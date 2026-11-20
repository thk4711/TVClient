"""Read-only source discovery and leased, staged OpenWebif bouquet imports."""
import asyncio
import concurrent.futures
import os
import socket
from pathlib import Path

from satip_controller.backend import BackendError
from satip_controller.config import library, uuid
from satip_controller.enigma2 import Enigma2
from satip_controller.scan import Manager
from satip_controller.state import atomic_json, read_json
from . import discovery
from .tv_source import profile_path, validate_profile, source_networks


def recover_publication(store):
    store = Path(store)
    journal = store / "web/enigma2-import/publish-rollback.json"
    if not journal.exists():
        return
    node = read_json(journal)
    if set(node) != {"profile", "library", "candidates"}:
        raise ValueError("Invalid import publication journal")
    validate_profile(node["profile"])
    library(node["library"])
    from .tv_source import owned_write
    for path, value, gid in ((profile_path(store, "enigma2"), node["profile"], 422),
                             (store / "management/library.json", node["library"], 423),
                             (store / "web/candidates.json", node["candidates"], 422)):
        if os.geteuid() == 0:
            owned_write(path, value, uid=422, gid=gid)
        else:
            atomic_json(path, value, 0o640)
    journal.unlink()


def search_enigma2():
    available = discovery.interfaces()
    def probe(host):
        try:
            with socket.create_connection((host, 80), timeout=.3):
                pass
            return Enigma2(host, timeout=2).about()
        except (OSError, ValueError, BackendError):
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        servers = [s for s in pool.map(probe, discovery.local_targets(available)) if s]
    return {"servers": servers[:16], "interfaces": [n for n, _ in available],
            "warnings": ["Discovery result limited to 16 receivers"] if len(servers) > 16 else []}


class Importer:
    def __init__(self, backend, config, store, owner=None, prepare=None):
        self.backend, self.config, self.store, self.owner = backend, config, Path(store), owner
        self.prepare = prepare
        self.manager = None
        self.task = None
        self.status = {"state": "idle"}

    def start(self, receiver, bouquets, timeout=20):
        if self.task and not self.task.done():
            raise BackendError("A bouquet import is already running")
        if self.owner and (self.owner.lock.locked() or (self.owner.task and not self.owner.task.done())):
            raise BackendError("Stop channel search or finish editing first")
        self.status = {"state": "starting", "found": 0, "completed": 0, "total": 0}
        self.task = asyncio.create_task(self.run(receiver, bouquets, timeout))
        if self.owner:
            self.owner.task = self.task

    async def run(self, receiver, bouquets, timeout):
        self.manager = None
        lock = self.owner.lock if self.owner else asyncio.Lock()
        async with lock:
            pulse = None
            network = None
            journal = self.store / "web/enigma2-import/network-rollback.json"
            try:
                # Publish a lease before any slow metadata/backend preparation.
                if self.owner:
                    await self.owner.recover()
                    self.owner.lease(30)
                    pulse = asyncio.create_task(self.owner.heartbeat())
                    await self.owner.quiet()
                receiver_info = await asyncio.to_thread(receiver.about)
                services = await asyncio.to_thread(receiver.services, bouquets)
                builders = await self.backend.request("mpegts/network/builders")
                if not any(n.get("class") == "iptv_network" for n in builders.get("entries", [])):
                    raise BackendError("The backend needs IPTV support for Enigma2")
                path = profile_path(self.store, "enigma2")
                previous = validate_profile(read_json(path)) if path.exists() else None
                if self.owner and self.config["network_uuid"] != "0" * 32 and Path(self.config["library"]).exists():
                    active = "enigma2" if previous and previous["network_uuid"] == self.config["network_uuid"] else "satip"
                    # Capture current browser edits, not a stale source-switch snapshot.
                    node = {"version": 1, "network_uuid": self.config["network_uuid"],
                            "library": read_json(Path(self.config["library"])),
                            "candidates": read_json(Path(self.config["private_dir"]) / "candidates.json")}
                    if active == "enigma2":
                        node["enigma2"] = previous["enigma2"]
                    validate_profile(node)
                    atomic_json(profile_path(self.store, active), node, 0o640)
                    if active == "enigma2":
                        previous = node
                if previous and any(previous["enigma2"][k] != v for k, v in
                        (("address", receiver.host), ("web_port", receiver.web_port), ("stream_port", receiver.stream_port))):
                    raise BackendError("Only one Enigma2 receiver profile is supported; use the configured receiver")
                networks = await self.backend.grid("mpegts/network/grid")
                name = "Enigma2 " + receiver.host
                matching = [n for n in networks if n.get("networkname") == name]
                if len(matching) > 1:
                    raise BackendError("Ambiguous Enigma2 network")
                if previous:
                    network = previous["network_uuid"]
                    if not any(n["uuid"] == network for n in matching):
                        raise BackendError("Saved Enigma2 network is missing")
                elif matching:
                    network = uuid(matching[0]["uuid"])
                else:
                    result = await self.backend.request("mpegts/network/create", write=True,
                        **{"class": "iptv_network", "conf": {"networkname": name, "enabled": False,
                           "skipinitscan": True, "idlescan": False, "autodiscovery": 0,
                           "max_streams": 1, "scan_create": False, "remove_scrambled": False}})
                    network = uuid(result["uuid"])
                if self.prepare:
                    self.prepare(network, receiver, bouquets)
                original_enabled = next((n["enabled"] for n in networks if n["uuid"] == network), False)
                work = journal.parent
                work.mkdir(mode=0o700, exist_ok=True)
                if journal.exists() or (work / "mux-rollback.json").exists():
                    raise BackendError("Recover the interrupted import first")
                staged = work / "library.json"
                if previous:
                    atomic_json(staged, previous["library"], 0o640)
                else:
                    staged.unlink(missing_ok=True)
                # Do not reuse an earlier failed/partial import's candidate catalogue.
                atomic_json(work / "candidates.json", {"channels": []})
                manager = Manager(self.backend, {**self.config, "network_uuid": network,
                    "private_dir": str(work), "library": str(staged), "mux_timeout": timeout}, setup=True)
                self.manager = manager
                if not pulse:
                    manager.lease(30)
                    pulse = asyncio.create_task(manager.heartbeat())
                    await manager.quiet()
                muxes = [m for m in await self.backend.grid("mpegts/mux/grid") if m.get("network_uuid") == network]
                known = {m.get("iptv_url"): m for m in muxes}
                for service in services:
                    url = receiver.stream_url(service["reference"])
                    if url not in known:
                        await self.backend.request("mpegts/network/mux_create", write=True, uuid=network,
                            conf={"iptv_url": url, "iptv_muxname": service["name"], "iptv_sname": service["name"],
                                  "enabled": 0, "epg": 0, "scan_state": 0})
                muxes = [m for m in await self.backend.grid("mpegts/mux/grid") if m.get("network_uuid") == network]
                known = {m.get("iptv_url"): m for m in muxes}
                refs = dict(previous["enigma2"]["services"]) if previous else {}
                requested = []
                for service in services:
                    mux = known[receiver.stream_url(service["reference"])]
                    refs[mux["uuid"]] = service["reference"]
                    requested.append(mux["uuid"])
                atomic_json(journal, {"uuid": network, "enabled": original_enabled})
                await self.backend.save(uuid=network, enabled=True)
                await manager.scan(mux_ids=requested)
                manager.lease(30)
                self.status = manager.status
                if manager.status["state"] != "complete" or manager.journal.exists():
                    raise BackendError("Import did not complete; previous lineup retained")
                found = await manager.candidates(muxes=set(requested) & set(manager.scanned_muxes))
                if not found:
                    raise BackendError("No supported free-to-air German HD/SD channels found; previous lineup retained")
                # Retain exact service identities/order, never match favourites by name.
                ids = {c["service_uuid"] for c in found}
                old, favourites = library(previous["library"]) if previous else ({}, [])
                chosen = [old[c].service_uuid for c in favourites if old[c].service_uuid in ids]
                manager.validation_networks = source_networks(self.store / "web", self.config["network_uuid"]) | {network}
                data = await manager.save_favourites(chosen or [found[0]["service_uuid"]])
                data = {**data, "channels": [c for c in data["channels"] if c["service_uuid"] in ids]}
                library(data)
                atomic_json(staged, data, 0o640)
                node = {"version": 1, "network_uuid": network, "library": data,
                    "candidates": {"channels": found}, "enigma2": {"address": receiver.host,
                    "web_port": receiver.web_port, "stream_port": receiver.stream_port,
                    "bouquets": bouquets, "services": refs, "selected_muxes": requested}}
                validate_profile(node)
                publication = work / "publish-rollback.json"
                if self.config["network_uuid"] == network:
                    atomic_json(publication, {"profile": previous,
                        "library": read_json(Path(self.config["library"])),
                        "candidates": read_json(Path(self.config["private_dir"]) / "candidates.json")})
                atomic_json(path, node, 0o640)
                if self.config["network_uuid"] == network:
                    atomic_json(Path(self.config["library"]), data, 0o640)
                    atomic_json(Path(self.config["private_dir"]) / "candidates.json", node["candidates"])
                    if self.owner:
                        self.owner.catalogue_revision += 1
                    publication.unlink()
                self.status.update(state="complete", found=len(found), receiver=receiver_info,
                                   message="Bouquets imported. Select this source in Settings if it is not active.")
            except asyncio.CancelledError:
                self.status = {**(self.manager.status if self.manager else self.status),
                               "state": "cancelled", "error": "Import stopped; previous lineup retained"}
                raise
            except (BackendError, OSError, ValueError, KeyError) as error:
                self.status = {**(self.manager.status if self.manager else self.status),
                               "state": "error", "error": str(error)}
            finally:
                try:
                    recover_publication(self.store)
                except (OSError, ValueError):
                    self.status.update(state="error", error="Import publication recovery required")
                if network and journal.exists():
                    try:
                        restore = read_json(journal)
                        await self.backend.save(**restore)
                        journal.unlink()
                    except (BackendError, OSError, ValueError):
                        self.status.update(state="error", error="Network restoration failed; use source recovery")
                if pulse:
                    pulse.cancel()
                    await asyncio.gather(pulse, return_exceptions=True)
                if self.manager:
                    pending = self.manager.journal.exists() or journal.exists() or (journal.parent / "publish-rollback.json").exists()
                    self.manager.lease(86400 if pending else 0)
                    if pending:
                        from satip_controller.state import lease_data, runtime_path
                        atomic_json(self.manager.shared / "recovery.json", {"pending": True}, 0o640)
                        atomic_json(runtime_path(self.manager.shared, "scan-lease.json"),
                                    lease_data(86400, recovery_pending=True), 0o640, durable=False)
                    self.status["recovery_pending"] = pending
                elif self.owner:
                    self.owner.lease(0)
                if self.owner:
                    self.owner.status = self.status

    def public_status(self):
        if self.manager and self.task and not self.task.done():
            status = self.manager.status
            return {**status, "state": "running"} if status.get("state") == "complete" else status
        return self.status

    async def close(self):
        from satip_controller.lifecycle import stop_task
        await stop_task(self.task)
