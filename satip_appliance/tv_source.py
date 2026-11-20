"""Root-only, recoverable Enigma2 import and single-source selection.

Run on the receiver, not on the Enigma2 box. No remote zap or filesystem changes
on the source. The usual web channel/favourites UI remains the channel manager.
"""
import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import subprocess

from satip_controller.backend import Backend, BackendError
from satip_controller.config import credentials, library, uuid
from satip_controller.enigma2 import Enigma2, service_reference
from satip_controller.scan import Manager
from satip_controller.state import atomic_json, read_json, runtime_path
from . import persistent

STORE = Path("/data/store")
SOURCES = ("satip", "enigma2")


def profile_path(store, source):
    if source not in SOURCES:
        raise ValueError("Unknown TV source")
    return store / ("web/tv-source-" + source + ".json")


def validate_profile(node):
    fields = {"version", "network_uuid", "library", "candidates"}
    if (not isinstance(node, dict) or set(node) not in (fields, fields | {"enigma2"})
            or type(node.get("version")) is not int or node["version"] != 1):
        raise ValueError("Invalid TV source profile")
    uuid(node["network_uuid"])
    library(node["library"])
    if not isinstance(node.get("candidates"), dict) or not isinstance(node["candidates"].get("channels"), list):
        raise ValueError("Invalid TV source catalogue")
    if "enigma2" in node:
        info = node["enigma2"]
        required = {"address", "web_port", "stream_port", "bouquets", "services"}
        if (not isinstance(info, dict) or set(info) not in (required, required | {"selected_muxes"})
                or not isinstance(info["bouquets"], list) or not 1 <= len(info["bouquets"]) <= 16
                or any(not isinstance(n, str) or not n or len(n) > 150 for n in info["bouquets"])):
            raise ValueError("Invalid Enigma2 source settings")
        Enigma2(info["address"], info["web_port"], info["stream_port"])
        if not isinstance(info["services"], dict) or len(info["services"]) > 500:
            raise ValueError("Invalid Enigma2 service mapping")
        for mux, ref in info["services"].items():
            uuid(mux)
            service_reference(ref)
        if "selected_muxes" in info:
            selected = info["selected_muxes"]
            if (not isinstance(selected, list) or not 1 <= len(selected) <= 500
                    or any(not isinstance(mux, str) for mux in selected)
                    or len(set(selected)) != len(selected) or not set(selected) <= set(info["services"])):
                raise ValueError("Invalid selected Enigma2 mux scope")
    return node


def validate_iptv_record(name, record, store):
    """Backups never turn IPTV support into arbitrary URL/pipe execution."""
    import re
    match = re.fullmatch(r"tvheadend/input/iptv/networks/([0-9a-f]{32})/(config|muxes/([0-9a-f]{32}))", name)
    profile = validate_profile(read_json(profile_path(store, "enigma2")))
    if not match or match[1] != profile["network_uuid"]:
        raise ValueError("Unmanaged IPTV network refused")
    info = profile["enigma2"]
    receiver = Enigma2(info["address"], info["web_port"], info["stream_port"])
    if match[2] == "config":
        if (record.get("class", "iptv_network") != "iptv_network" or record.get("url") or record.get("remove_scrambled")
                or record.get("max_streams") != 1 or record.get("idlescan")
                or record.get("skipinitscan") is not True):
            raise ValueError("Only managed Enigma2 IPTV networks may be restored")
    else:
        config = record.get("config", {})
        reference = info["services"].get(match[3])
        if (not reference or config.get("iptv_url") != receiver.stream_url(reference)
                or any(config.get(k) for k in ("iptv_env", "iptv_hdr", "iptv_respawn", "iptv_icon",
                                             "iptv_ret_url", "iptv_substitute"))):
            raise ValueError("Unmanaged IPTV URL or external hooks refused")


def public_status(store=STORE):
    config_path = store / "config/web/config.json"
    if not config_path.exists():
        return {"active": "satip", "available": ["satip"]}
    config = read_json(config_path)
    path = profile_path(store, "enigma2")
    profile = validate_profile(read_json(path)) if path.exists() else None
    return {"active": "enigma2" if profile and profile["network_uuid"] == config["network_uuid"] else "satip",
            "available": (["satip"] if config["network_uuid"] != (profile or {}).get("network_uuid")
                          or profile_path(store, "satip").exists() else []) + (["enigma2"] if profile else [])}


def source_networks(directory, default):
    result = {default}
    for source in SOURCES:
        path = Path(directory) / ("tv-source-" + source + ".json")
        if path.exists():
            result.add(validate_profile(read_json(path))["network_uuid"])
    return result


def owned_write(path, node, uid=422, gid=420, mode=0o640):
    if path.exists():
        info = path.stat()
        uid, gid, mode = info.st_uid, info.st_gid, info.st_mode & 0o777
    atomic_json(path, node, mode)
    os.chown(path, uid, gid)


def snapshot(store, config):
    path = profile_path(store, "enigma2")
    previous = validate_profile(read_json(path)) if path.exists() else None
    source = "enigma2" if previous and previous["network_uuid"] == config["network_uuid"] else "satip"
    node = {"version": 1, "network_uuid": config["network_uuid"],
            "library": read_json(store / "management/library.json"),
            "candidates": (read_json(store / "web/candidates.json") if (store / "web/candidates.json").exists() else {"channels": []})}
    if source == "enigma2":
        node["enigma2"] = previous["enigma2"]
    validate_profile(node)
    owned_write(profile_path(store, source), node, uid=0, gid=422)
    return source


def command(*args):
    subprocess.run(args, check=True, timeout=60,
                   env={**os.environ, "PYTHONPATH": "/opt/satip", "SATIP_RUNTIME_ROOT": "/run"})


@contextmanager
def maintenance(store=STORE):
    """Serialize root changes, stop owned player/web, retain backend for API work."""
    if os.geteuid() != 0 or store != STORE:
        raise ValueError("Run this operation as root on the configured receiver")
    persistent.reject_links(store)
    if read_json(store / "installation.json").get("state") not in ("configured", "qualified"):
        raise ValueError("Complete initial setup before using the command-line source importer")
    from .device_helper import Device
    device = Device()
    device.backups.quiet(source_recovery=True)
    if device.backups.pending or device.backups.gate.exists() or device.trial():
        raise ValueError("Finish configuration maintenance or Wi-Fi trial first")
    with Path("/run/satip-source.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        gate = Path("/run/satip-source-maintenance.json")
        from satip_controller.coordination import process_identity
        atomic_json(gate, {"owner": process_identity(os.getpid())}, 0o644, durable=False)
        stopped = False
        try:
            command("/etc/init.d/S89satip-watchdog", "stop")
            stopped = True
            for service in ("player", "web"):
                command("/usr/bin/python3", "-m", "satip_appliance.supervisor", service, "stop")
            yield
        finally:
            failures = []
            if stopped:
                actions = [("/usr/bin/python3", "-m", "satip_appliance.supervisor", service, "start")
                           for service in ("web", "player")]
                actions.append(("/etc/init.d/S89satip-watchdog", "start"))
                for action in actions:
                    try:
                        command(*action)
                    except (OSError, subprocess.SubprocessError):
                        failures.append(action[-2])
            gate.unlink(missing_ok=True)
            if failures:
                raise BackendError("TV source cleanup could not restart all services; use source recovery")


async def recover(backend, store=STORE):
    from .source_browser import recover_publication
    from .satip_tuner import recover as recover_tuner
    await recover_tuner(backend, store)
    recover_publication(store)
    path = store / "web/tv-source-transaction.json"
    if path.exists():
        node = read_json(path)
        # Fixed paths only; journal data never chooses filesystem paths or commands.
        library(node["library"])
        uuid(node["config"]["network_uuid"])
        factory_path = store / "identity/factory.json"
        if factory_path.exists():
            from .provision import web_config
            factory = read_json(factory_path)
            config = node["config"]
            if config != web_config(config["network_uuid"], config["playback_acl_uuid"],
                                    factory["settings"], config["allowed_hosts"]):
                raise ValueError("Invalid generated configuration in source journal")
        for network in node["networks"]:
            await backend.save(uuid=uuid(network["uuid"]), enabled=network["enabled"])
        owned_write(store / "config/web/config.json", node["config"])
        owned_write(store / "management/library.json", node["library"])
        owned_write(store / "web/candidates.json", node["candidates"])
        path.unlink()
    journal = store / "web/enigma2-import/mux-rollback.json"
    if journal.exists():
        node = read_json(journal)
        if set(node) != {"uuid", "enabled", "epg", "scan_state"}:
            raise ValueError("Invalid Enigma2 scan journal")
        await backend.save(**node)
        journal.unlink()
        from satip_controller.state import lease_data
        owned_write(store / "management/recovery.json", {"pending": False})
        owned_write(runtime_path(store / "management", "scan-lease.json"), lease_data(0))
    network_journal = store / "web/enigma2-import/network-rollback.json"
    if network_journal.exists():
        node = read_json(network_journal)
        if set(node) != {"uuid", "enabled"}:
            raise ValueError("Invalid Enigma2 network journal")
        await backend.save(uuid=uuid(node["uuid"]), enabled=node["enabled"])
        network_journal.unlink()


async def select(backend, source, store=STORE):
    await recover(backend, store)
    config = read_json(store / "config/web/config.json")
    snapshot(store, config)
    node = validate_profile(read_json(profile_path(store, source)))
    networks = await backend.grid("mpegts/network/grid")
    relevant = source_networks(store / "web", config["network_uuid"])
    if not any(n["uuid"] == node["network_uuid"] for n in networks):
        raise ValueError("The selected source's backend network is missing")
    # Verify stored references against current backend metadata before publication.
    check_config = {**config, "network_uuid": node["network_uuid"]}
    manager = Manager(backend, check_config)
    channels, _ = library(node["library"])
    fresh = {c["service_uuid"]: c for c in await manager.candidates(service_ids={c.service_uuid for c in channels.values()})}
    if any(c.service_uuid not in fresh for c in channels.values()):
        raise ValueError("Source channel metadata changed; import/scan again before selecting it")
    journal = store / "web/tv-source-transaction.json"
    atomic_json(journal, {"config": config, "library": read_json(store / "management/library.json"),
                         "candidates": (read_json(store / "web/candidates.json") if (store / "web/candidates.json").exists() else {"channels": []}),
                         "networks": [{"uuid": n["uuid"], "enabled": n["enabled"]} for n in networks if n["uuid"] in relevant]})
    try:
        for network in networks:
            if network["uuid"] in relevant:
                await backend.save(uuid=network["uuid"], enabled=network["uuid"] == node["network_uuid"])
        owned_write(store / "config/web/config.json", check_config)
        owned_write(store / "web/candidates.json", node["candidates"])
        owned_write(store / "management/library.json", node["library"])
        journal.unlink()
    except BaseException:
        await recover(backend, store)
        raise
    print("Selected TV source:", source, flush=True)


def import_selection(previous, found, channel_names, current_library):
    """Targeted imports append favourites/catalogue entries instead of replacing them."""
    old_channels, old_favourites = library(previous["library"] if previous else current_library)
    by_name = {c["name"]: c["service_uuid"] for c in found}
    if channel_names:
        missing = [name for name in channel_names if name not in by_name]
        if missing:
            raise BackendError("Requested channels did not pass stream validation: " + ", ".join(missing)
                               + ". Existing lineup retained.")
        chosen = [old_channels[c].service_uuid for c in old_favourites] if previous else []
        chosen.extend(by_name[name] for name in channel_names)
        catalogue = {c["service_uuid"]: c for c in previous["candidates"]["channels"]} if previous else {}
        catalogue.update({c["service_uuid"]: c for c in found})
        found = sorted(catalogue.values(), key=lambda c: c["name"].casefold())
    else:
        chosen = [by_name[old_channels[c].name] for c in old_favourites if old_channels[c].name in by_name]
    return list(dict.fromkeys(chosen)) or [found[0]["service_uuid"]], found


async def import_channels(backend, receiver, bouquets, channel_names, timeout, store=STORE):
    info = receiver.about()
    services = receiver.services(bouquets)
    if channel_names:
        services = [s for s in services if s["name"] in channel_names]
        if {s["name"] for s in services} != set(channel_names):
            raise ValueError("A requested channel is not present in the selected bouquets")
    print("Receiver:", info["model"], "· candidate references:", len(services), flush=True)
    builders = await backend.request("mpegts/network/builders")
    if not any(n.get("class") == "iptv_network" for n in builders.get("entries", [])):
        raise BackendError("Install the matching IPTV-enabled Tvheadend binary first")
    config = read_json(store / "config/web/config.json")
    current = snapshot(store, config)
    path = profile_path(store, "enigma2")
    previous = validate_profile(read_json(path)) if path.exists() else None
    if previous and any(previous["enigma2"][k] != v for k, v in
                        (("address", receiver.host), ("web_port", receiver.web_port), ("stream_port", receiver.stream_port))):
        raise ValueError("This importer supports one Enigma2 receiver; retain its identity")
    networks = await backend.grid("mpegts/network/grid")
    name = "Enigma2 " + receiver.host
    matching = [n for n in networks if n.get("networkname") == name]
    if len(matching) > 1:
        raise BackendError("Ambiguous Enigma2 network; refusing duplicates")
    if previous:
        network = previous["network_uuid"]
        if not any(n["uuid"] == network for n in matching):
            raise BackendError("Saved Enigma2 network is missing")
    elif matching:
        network = uuid(matching[0]["uuid"])
    else:
        result = await backend.request("mpegts/network/create", write=True,
            **{"class": "iptv_network", "conf": {"networkname": name, "enabled": False,
               "skipinitscan": True, "idlescan": False, "autodiscovery": 0, "max_streams": 1,
               "scan_create": False, "remove_scrambled": False}})
        network = uuid(result["uuid"])
    original_enabled = next((n["enabled"] for n in networks if n["uuid"] == network), False)
    work = store / "web/enigma2-import"
    work.mkdir(mode=0o700, exist_ok=True)
    staged_library = work / "library.json"
    if previous:
        atomic_json(staged_library, previous["library"])
    else:
        staged_library.unlink(missing_ok=True)
    manager = Manager(backend, {**config, "private_dir": str(work), "library": str(staged_library),
                               "network_uuid": network, "mux_timeout": timeout}, setup=True)
    references = dict(previous["enigma2"]["services"]) if previous else {}
    try:
        await manager.recover()
        manager.lease(30)
        await manager.quiet()
        muxes = [m for m in await backend.grid("mpegts/mux/grid") if m.get("network_uuid") == network]
        known = {m.get("iptv_url"): m for m in muxes}
        for service in services:
            url = receiver.stream_url(service["reference"])
            if url not in known:
                await backend.request("mpegts/network/mux_create", write=True, uuid=network,
                    conf={"iptv_url": url, "iptv_muxname": service["name"], "iptv_sname": service["name"],
                          "enabled": 0, "epg": 0, "scan_state": 0})
        muxes = [m for m in await backend.grid("mpegts/mux/grid") if m.get("network_uuid") == network]
        by_url = {m.get("iptv_url"): m for m in muxes}
        requested = []
        for service in services:
            mux = by_url.get(receiver.stream_url(service["reference"]))
            if not mux:
                raise BackendError("Incomplete Enigma2 mux import")
            references[mux["uuid"]] = service["reference"]
            requested.append(mux["uuid"])
        # Scan only this explicit import selection, not stale muxes from other bouquets.
        atomic_json(work / "network-rollback.json", {"uuid": network, "enabled": original_enabled})
        await backend.save(uuid=network, enabled=True)
        await manager.scan(mux_ids=requested)
        if manager.status["state"] != "complete" or manager.status.get("recovery_pending"):
            raise BackendError("Enigma2 scan failed; inspect the import status and recover")
        found = await manager.candidates(muxes=set(requested) & set(manager.scanned_muxes))
        if not found:
            raise BackendError("No supported free-to-air German HD/SD services were found")
        # Keep existing favourites where possible; a new source starts with the
        # old source's matching channel names, otherwise the first supported one.
        chosen, catalogue = import_selection(previous, found, channel_names,
                                             read_json(store / "management/library.json"))
        # Existing snapshots authorize metadata revalidation across both sources,
        # never bypassing the candidate()/ACL fail-closed checks.
        manager.validation_networks = source_networks(store / "web", config["network_uuid"]) | {network}
        data = await manager.save_favourites(chosen)
        node = {"version": 1, "network_uuid": network, "library": data,
                "candidates": {"channels": catalogue}, "enigma2": {"address": receiver.host,
                "web_port": receiver.web_port, "stream_port": receiver.stream_port,
                "bouquets": list(dict.fromkeys(previous["enigma2"]["bouquets"] + bouquets))
                    if previous and channel_names else bouquets, "services": references,
                "selected_muxes": list(dict.fromkeys(
                    (previous["enigma2"].get("selected_muxes", list(previous["enigma2"]["services"]))
                     if previous and channel_names else []) + requested))}}
        validate_profile(node)
        owned_write(path, node, uid=0, gid=422)
        print("Import complete:", len(found), "supported channels;", len(chosen), "favourites; source unchanged:", current, flush=True)
    finally:
        await backend.save(uuid=network, enabled=original_enabled)
        (work / "network-rollback.json").unlink(missing_ok=True)
        manager.lease(86400 if manager.journal.exists() else 0)
        # Root's scan writes must remain readable by the player/web roles.
        for lease in (runtime_path(manager.shared, "scan-lease.json"), manager.shared / "recovery.json"):
            if lease.exists():
                os.chown(lease, 422, 420)
    if current == "enigma2":
        # Publish refreshed metadata only when this was already the active source.
        node = validate_profile(read_json(path))
        owned_write(store / "management/library.json", node["library"])
        owned_write(store / "web/candidates.json", node["candidates"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("discover", "import"):
        p = sub.add_parser(action)
        p.add_argument("--address", required=True)
        p.add_argument("--web-port", type=int, default=80)
        p.add_argument("--stream-port", type=int, default=8001)
        if action == "import":
            p.add_argument("--bouquet", action="append", default=[])
            p.add_argument("--channel", action="append", default=[])
            p.add_argument("--timeout", type=int, default=20, choices=range(5, 91), metavar="5–90")
            p.add_argument("--apply", action="store_true")
    p = sub.add_parser("select")
    p.add_argument("source", choices=SOURCES)
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("tuner")
    p.add_argument("frontend", type=uuid)
    p.add_argument("--apply", action="store_true")
    sub.add_parser("recover")
    args = parser.parse_args()
    os.environ["SATIP_RUNTIME_ROOT"] = "/run"
    receiver = Enigma2(args.address, args.web_port, args.stream_port) if args.action in ("discover", "import") else None
    if args.action == "discover":
        print(receiver.about())
        for bouquet in receiver.bouquets():
            print(bouquet["name"])
        return
    if args.action != "recover" and not args.apply:
        print("Dry run: no changes. Add --apply to interrupt HDMI/web playback.")
        if args.action == "import":
            for service in receiver.services(args.bouquet or ["German Free HD", "German Free SD"]):
                print(service["name"])
        return
    backend = Backend(credentials(STORE / "config/web/tvheadend-admin.json"))
    with maintenance():
        if args.action == "import":
            asyncio.run(recover(backend))
            asyncio.run(import_channels(backend, receiver, args.bouquet or ["German Free HD", "German Free SD"], args.channel, args.timeout))
        elif args.action == "tuner":
            from .satip_tuner import select as select_tuner
            asyncio.run(recover(backend))
            asyncio.run(select_tuner(backend, args.frontend, STORE))
        elif args.action == "select":
            asyncio.run(select(backend, args.source))
        else:
            asyncio.run(recover(backend))


if __name__ == "__main__":
    try:
        main()
    except (BackendError, ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from None
