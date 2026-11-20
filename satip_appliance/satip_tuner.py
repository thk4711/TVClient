"""Read-only SAT-IP inventory and fixed-path recoverable tuner reassignment."""
from pathlib import Path

from satip_controller.backend import BackendError
from satip_controller.config import uuid
from satip_controller.state import atomic_json, read_json
from .setup import flatten

# Copy reception policy, not frontend identity, names or master/slave wiring.
POLICY = ("priority", "spriority", "ota_epg", "initscan", "idlescan", "free_weight",
          "remove_scrambled", "idle_grace", "transport_mode", "udp_rtp_port", "tdelay",
          "play2", "grace_period", "teardown_delay", "pass_weight", "specinv", "delsys")
JOURNAL = "web/satip-tuner-transaction.json"


def network(store):
    from .tv_source import public_status, profile_path, validate_profile
    if public_status(store)["active"] == "satip":
        return read_json(store / "config/web/config.json")["network_uuid"]
    path = profile_path(store, "satip")
    return validate_profile(read_json(path))["network_uuid"] if path.exists() else None


async def inventory(backend, store):
    """Use backend identities; discovery is not a connection or configuration."""
    store = Path(store)
    saved = store / "web/server-selection.json"
    info = read_json(saved) if saved.exists() else {}
    accepted = store / "identity/satip-server.json"
    if accepted.exists():
        info = {**info, **read_json(accepted)}
    result = {"name": info.get("model") or info.get("name", "SAT-IP server"),
              "address": info.get("address", ""), "url": info.get("url", ""),
              "connected": False, "tuners": [], "configured_frontend": None,
              "receiving": [], "tuning": []}
    if not result["address"] and result["url"]:
        from urllib.parse import urlsplit
        result["address"] = urlsplit(result["url"]).hostname
    roots = await backend.request("hardware/tree", uuid="root")
    selected = next((s for s in roots if s.get("class") == "satip_client"
                     and flatten(s).get("device_uuid") == info.get("udn", "")[5:]), None)
    if not selected:
        return result
    fields = flatten(selected)
    result.update(name=fields.get("friendly") or result["name"],
                  address=fields.get("addr") or result["address"],
                  connected=fields.get("active") is True)
    current_network = network(store)
    for child in await backend.request("hardware/tree", uuid=selected["uuid"]):
        if child.get("class") != "satip_frontend_dvbs":
            continue
        tuner = flatten(child)
        positions = [flatten(p) for p in await backend.request("hardware/tree", uuid=child["uuid"])]
        assigned = any(p.get("enabled") and current_network in p.get("networks", []) for p in positions)
        configured = tuner.get("enabled") is True and assigned
        result["tuners"].append({"uuid": child["uuid"], "number": tuner.get("fe_number"),
                                 "configured": configured})
        if configured:
            result["configured_frontend"] = child["uuid"]
    result["tuners"].sort(key=lambda t: t["number"] or 0)
    # Input labels are backend-reported receiving status, not frontend availability.
    inputs = await backend.grid("status/inputs")
    relevant = [i for i in inputs if result["address"] and result["address"] in i.get("input", "")]
    result["receiving"] = [i.get("input", "") for i in relevant if i.get("bps", 0) > 0]
    result["tuning"] = [i.get("input", "") for i in relevant if i.get("bps", 0) <= 0]
    return result


async def recover(backend, store):
    store = Path(store)
    path = store / JOURNAL
    if not path.exists():
        return
    node = read_json(path)
    if set(node) != {"frontends", "positions", "selection"}:
        raise ValueError("Invalid SAT-IP tuner recovery journal")
    for item in node["frontends"]:
        if set(item) - {"uuid", "enabled", *POLICY}:
            raise ValueError("Invalid SAT-IP frontend recovery")
        uuid(item["uuid"])
    for item in node["positions"]:
        if set(item) != {"uuid", "enabled", "networks"} or not isinstance(item["networks"], list):
            raise ValueError("Invalid SAT-IP position recovery")
        uuid(item["uuid"])
        for entry in item["networks"]:
            uuid(entry)
    # Disable first so a failed rollback cannot leave two enabled frontends.
    for item in node["frontends"]:
        await backend.save(uuid=item["uuid"], enabled=False)
    for item in node["positions"]:
        await backend.save(**item)
    for item in node["frontends"]:
        await backend.save(**item)
    if node["selection"] is not None:
        from .tv_source import owned_write
        owned_write(store / "web/setup-state.json", node["selection"])
    path.unlink()


async def select(backend, frontend, store):
    store = Path(store)
    uuid(frontend)
    await recover(backend, store)
    from .tv_source import public_status, owned_write
    if public_status(store)["active"] != "satip":
        raise ValueError("Activate SAT-IP before changing its tuner")
    info = await inventory(backend, store)
    if not info["connected"] or frontend not in {t["uuid"] for t in info["tuners"]}:
        raise ValueError("Select an available tuner on the configured SAT-IP server")
    old = info["configured_frontend"]
    if not old:
        raise ValueError("The configured SAT-IP tuner could not be identified")
    if old == frontend:
        return
    if await backend.grid("status/subscriptions"):
        raise BackendError("Another client owns the tuner")
    nodes = await backend.request("idnode/load", **{"class": "satip_frontend"})
    frontends = [flatten(n) for n in nodes.get("entries", [])]
    if [t["uuid"] for t in frontends if t.get("enabled")] != [old]:
        raise ValueError("Expected exactly one configured SAT-IP tuner")
    previous = next(t for t in frontends if t["uuid"] == old)
    target = next(t for t in frontends if t["uuid"] == frontend)
    if previous.get("fe_master") or target.get("fe_master") or previous.get("linked") or target.get("linked"):
        raise ValueError("Linked or master/slave tuner wiring needs advanced configuration")
    source_positions = [flatten(p) for p in await backend.request("hardware/tree", uuid=old)]
    target_positions = [flatten(p) for p in await backend.request("hardware/tree", uuid=frontend)]
    net = network(store)
    position = next((p.get("position") for p in source_positions if p.get("enabled") and net in p.get("networks", [])), None)
    chosen = next((p for p in target_positions if p.get("position") == position), None)
    if position is None or chosen is None:
        raise ValueError("The selected tuner does not support the configured satellite position")
    selection_path = store / "web/setup-state.json"
    selection = read_json(selection_path) if selection_path.exists() else None
    journal = {"frontends": [{"uuid": t["uuid"], "enabled": t.get("enabled", False),
                              **{k: t[k] for k in POLICY if k in t}} for t in (previous, target)],
               "positions": [{"uuid": p["uuid"], "enabled": p.get("enabled", False),
                              "networks": p.get("networks", [])} for p in source_positions + target_positions],
               "selection": selection}
    atomic_json(store / JOURNAL, journal, 0o600)
    try:
        await backend.save(uuid=old, enabled=False)
        for p in source_positions:
            await backend.save(uuid=p["uuid"], enabled=False, networks=[])
        for p in target_positions:
            await backend.save(uuid=p["uuid"], enabled=p["uuid"] == chosen["uuid"],
                               networks=[net] if p["uuid"] == chosen["uuid"] else [])
        await backend.save(uuid=frontend, enabled=True, **{k: previous[k] for k in POLICY if k in previous})
        check = await inventory(backend, store)
        enabled = await backend.request("idnode/load", **{"class": "satip_frontend"})
        if check["configured_frontend"] != frontend or [flatten(n)["uuid"] for n in enabled.get("entries", [])
                                                       if flatten(n).get("enabled")] != [frontend]:
            raise BackendError("Single-tuner configuration verification failed")
        if selection is not None:
            owned_write(selection_path, {**selection, "frontend_uuid": frontend})
        (store / JOURNAL).unlink()
    except BaseException:
        await recover(backend, store)
        raise
