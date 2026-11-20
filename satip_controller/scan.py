"""Single-tuner scan with playback lease, persisted rollback and explicit cancellation."""
import asyncio
import time
from dataclasses import asdict
from pathlib import Path

from .backend import BackendError, candidate
from .config import library, Channel
from .lifecycle import stop_task
from .coordination import wait_quiet
from .state import atomic_json, read_json, runtime_path, lease_data, retain_snapshots, JSONWriter, library_revision


class ConcurrentEdit(ValueError):
    pass


class Manager:
    def __init__(self, backend, config, *, setup=False):
        self.backend, self.config = backend, config
        from .enigma2 import profile_networks
        self.validation_networks = profile_networks(config["private_dir"], config["network_uuid"])
        self.setup = setup
        self.directory = Path(config["private_dir"])
        self.shared = Path(config["management_dir"])
        self.library_path = Path(config["library"])
        self.lock = asyncio.Lock()
        self.writer = JSONWriter()
        self.task = None
        self.status = {"state": "idle", "completed": 0, "total": 0, "found": 0}
        self.catalogue_revision = 0
        self.last_favourites_save_id = None
        self.scanned_muxes = []
        self.journal = self.directory / "mux-rollback.json"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.shared.mkdir(parents=True, exist_ok=True, mode=0o750)
        try:
            previous = read_json(self.directory / "last-scan.json")
            if isinstance(previous, dict) and previous.get("state") in ("complete", "cancelled", "error"):
                self.status = previous
        except (OSError, ValueError):
            pass

    def current_library(self):
        # Only the explicit fresh-setup caller may start without a catalogue.
        # Existing installations retain strict nonempty-library validation.
        if self.setup and not self.library_path.exists():
            return {}, []
        return library(read_json(self.library_path))

    def remember_status(self):
        atomic_json(self.directory / "last-scan.json", self.status)

    def lease(self, seconds):
        pending = self.journal.exists()
        recovery = self.shared / "recovery.json"
        try:
            old = read_json(recovery)
        except (OSError, ValueError):
            old = None
        if old != {"pending": pending}:
            atomic_json(recovery, {"pending": pending}, 0o640)
        atomic_json(runtime_path(self.shared, "scan-lease.json"),
                    lease_data(seconds, recovery_pending=pending), 0o640, durable=False)

    async def recover(self):
        source_journals = ("tv-source-transaction.json", "enigma2-import/mux-rollback.json",
                           "enigma2-import/network-rollback.json", "enigma2-import/publish-rollback.json")
        if any((self.directory / name).exists() for name in source_journals):
            raise BackendError("Recover the interrupted TV source operation using the source tool")
        if self.journal.exists():
            self.lease(86400)
            node = read_json(self.journal)
            await self.backend.save(**node)
            self.journal.unlink()
        self.lease(0)
        self.status["recovery_pending"] = False

    async def heartbeat(self):
        while True:
            self.lease(30)
            await asyncio.sleep(1)

    async def quiet(self):
        await wait_quiet(self.backend,
                         runtime_path(Path(self.config["player_state_dir"]), "status.json"),
                         lambda: self.lease(30))

    def selected_muxes(self):
        path = self.directory / "tv-source-enigma2.json"
        if path.exists():
            from satip_appliance.tv_source import validate_profile
            node = validate_profile(read_json(path))
            if node["network_uuid"] == self.config["network_uuid"]:
                return node.get("enigma2", {}).get("selected_muxes")
        return None

    async def candidates(self, muxes=None, service_ids=None, networks=None):
        selected = self.selected_muxes() if networks is None else None
        networks = networks if networks is not None else {self.config["network_uuid"]}
        allowed = {m["uuid"] for m in await self.backend.grid("mpegts/mux/grid")
                   if m.get("network_uuid") in networks}
        if muxes is not None:
            allowed &= set(muxes)
        if selected is not None:
            allowed &= set(selected)
        found = []
        for service in await self.backend.grid("mpegts/service/grid"):
            if service_ids is not None and service["uuid"] not in service_ids:
                continue
            if service.get("multiplex_uuid") not in allowed or service.get("encrypted") is not False:
                continue
            streams = (await self.backend.request("service/streams", uuid=service["uuid"])).get("streams", [])
            item = candidate(service, streams)
            if item:
                found.append(item)
        return sorted(found, key=lambda c: c["name"].casefold())

    def publish_candidates(self, found, replace=False):
        # Keep known channels (and favourites) visible while discoveries arrive.
        known = {}
        if not replace:
            try:
                known.update({c["service_uuid"]: c for c in read_json(self.directory / "candidates.json")["channels"]})
            except (OSError, ValueError, KeyError):
                pass
            channels, _ = self.current_library()
            for channel in channels.values():
                known.setdefault(channel.service_uuid, asdict(channel))
        known.update({c["service_uuid"]: c for c in found})
        atomic_json(self.directory / "candidates.json",
                    {"channels": sorted(known.values(), key=lambda c: c["name"].casefold())})
        self.catalogue_revision += 1

    async def scan(self, enabled_only=False, mux_order=None, mux_ids=None):
        async with self.lock:
            if mux_ids is None:
                mux_ids = self.selected_muxes()
            self.status = {"state": "starting", "completed": 0, "total": 0, "found": 0}
            await self.recover()
            self.status.update(state="pausing")
            pulse = asyncio.create_task(self.heartbeat())
            succeeded = []
            self.scanned_muxes = succeeded
            partial = []
            failed = []
            discovered = {}
            try:
                await self.quiet()
                # Snapshot before supplementing the network or tuning. Full
                # scans repair partial upstream scanfile imports on old devices.
                muxes = [m for m in await self.backend.grid("mpegts/mux/grid")
                         if m.get("network_uuid") == self.config["network_uuid"]]
                await self.writer.write(self.directory / f"scan-backup-{time.time_ns()}.json", {"muxes": muxes})
                retain_snapshots(self.directory)
                if not enabled_only:
                    from .astra import supplement
                    self.status["added_muxes"] = await supplement(self.backend, self.config["network_uuid"])
                    muxes = [m for m in await self.backend.grid("mpegts/mux/grid")
                             if m.get("network_uuid") == self.config["network_uuid"]]
                else:
                    muxes = [m for m in muxes if m.get("enabled") == 1]
                if mux_ids is not None:
                    muxes = [m for m in muxes if m["uuid"] in set(mux_ids)]
                if not muxes:
                    raise BackendError("No multiplexes configured")
                if mux_order:
                    muxes.sort(key=mux_order)
                self.status.update(state="running", total=len(muxes))
                for mux in muxes:
                    self.status["multiplex"] = mux.get("name", mux["uuid"])
                    self.status["mux_elapsed"] = 0
                    restore = {"uuid": mux["uuid"], "enabled": mux["enabled"], "epg": mux["epg"], "scan_state": 0}
                    await self.writer.write(self.journal, restore)
                    self.lease(30)
                    try:
                        started = int(time.time())
                        await self.backend.save(uuid=mux["uuid"], enabled=1, epg=0, scan_state=1)
                        mux_started = time.monotonic()
                        end = mux_started + self.config.get("mux_timeout", 90)
                        while True:
                            await asyncio.sleep(1)
                            self.status["mux_elapsed"] = int(time.monotonic() - mux_started)
                            current = next(m for m in await self.backend.grid("mpegts/mux/grid") if m["uuid"] == mux["uuid"])
                            if current["scan_state"] == 0 and current.get("scan_last", 0) >= started:
                                # Tvheadend: 1 = OK, 3 = OK (partial). Partial
                                # table collection still yields usable services.
                                result = current.get("scan_result")
                                (succeeded if result in (1, 3) else failed).append(mux["uuid"])
                                if result == 3:
                                    partial.append(mux["uuid"])
                                break
                            if time.monotonic() > end:
                                failed.append(mux["uuid"])
                                break
                    finally:
                        await self.backend.save(**restore)
                        self.journal.unlink()
                    self.status["completed"] += 1
                    if mux["uuid"] in succeeded:
                        found = await self.candidates([mux["uuid"]])
                        discovered.update({c["service_uuid"]: c for c in found})
                        self.publish_candidates(found)
                    self.status.update(found=len(discovered), failed_muxes=len(failed),
                                       partial_muxes=len(partial))
                self.status["state"] = "complete"
            except asyncio.CancelledError:
                self.status["state"] = "cancelled"
                raise
            except (BackendError, OSError, ValueError, StopIteration):
                self.status.update(state="error", error="Scan failed; check backend and tuner. Retry recovery if pending.")
            finally:
                pulse.cancel()
                await asyncio.gather(pulse, return_exceptions=True)
                self.lease(86400 if self.journal.exists() else 0)
                self.status["recovery_pending"] = self.journal.exists()
                self.remember_status()

    async def save_favourites(self, services, expected_revision=None):
        async with self.lock:
            if expected_revision is not None and expected_revision != library_revision(read_json(self.library_path)):
                raise ConcurrentEdit("Favourites changed in another browser. Reload saved favourites before saving again.")
            if self.journal.exists():
                raise BackendError("Scan recovery required before editing favourites")
            if not services or len(set(services)) != len(services):
                raise ValueError("Select a non-empty unique list of supported free-to-air channels")
            channels, _ = self.current_library()
            # Preserve UUIDs/names/order of existing backend channels, and never
            # delete deselected channels or change tuner/network/anonymous access.
            all_channels = await self.backend.grid("channel/grid")
            muxes = {m["uuid"]: m for m in await self.backend.grid("mpegts/mux/grid")}
            acl = next((a for a in await self.backend.grid("access/entry/grid")
                        if a["uuid"] == self.config["playback_acl_uuid"]), None)
            if not acl or acl.get("admin") or acl.get("webui") or acl.get("conn_limit") != 1:
                raise BackendError("Restricted playback ACL does not match expected baseline")
            highest = max([int(c.get("number", 0)) for c in all_channels] + [3000000])
            new_count = sum(not any(c.get("enabled") and int(c.get("number", 0)) >= acl["channel_min"]
                                    and c.get("services") == [service] for c in all_channels)
                            for service in services)
            widening = highest + new_count*1000000 > acl["channel_max"]
            validate = set(services)
            if widening:
                for mapped in all_channels:
                    if int(mapped.get("number", 0)) >= acl["channel_min"]:
                        validate.update(mapped.get("services", []))
            # Revalidate the selected services, not hundreds of unrelated PMTs.
            # When widening access, also validate every potentially exposed mapping.
            scope = ({"networks": self.validation_networks}
                     if widening and self.validation_networks != {self.config["network_uuid"]} else {})
            fresh = {c["service_uuid"]: c for c in await self.candidates(service_ids=validate, **scope)}
            if any(s not in fresh for s in services):
                raise ValueError("Select supported free-to-air channels")
            for service in services:
                Channel.parse({k: v for k, v in {**fresh[service], "uuid": service}.items() if k != "encrypted"})
            # Persistent API snapshot is intentionally secret-free (no password records).
            await self.writer.write(self.directory / f"favourites-backup-{time.time_ns()}.json",
                        {"channels": all_channels, "muxes": list(muxes.values()), "playback_acl": acl,
                         "library": read_json(self.library_path) if self.library_path.exists() else None})
            retain_snapshots(self.directory)
            chosen = []
            # Widening a number range must not accidentally authorize an unrelated
            # encrypted/unknown mapping added through the Tvheadend UI.
            if widening:
                for mapped in all_channels:
                    if int(mapped.get("number", 0)) >= acl["channel_min"]:
                        mapped_services = mapped.get("services", [])
                        if len(mapped_services) != 1 or mapped_services[0] not in fresh:
                            raise BackendError("Review unrelated backend channel mappings before widening playback range")
            for service in services:
                item = fresh[service]
                mapped = next((c for c in all_channels if c.get("enabled") and int(c.get("number", 0)) >= acl["channel_min"]
                               and c.get("services") == [service]), None)
                if mapped is None:
                    highest += 1000000
                    result = await self.backend.request("channel/create", write=True,
                        conf={"name": item["name"], "autoname": False, "number": highest,
                              "enabled": True, "services": [service], "epgauto": True})
                    mapped = {"uuid": result["uuid"], "number": highest}
                    all_channels.append({**mapped, "services": [service], "enabled": True})
                elif not mapped.get("epgauto"):
                    await self.backend.save(uuid=mapped["uuid"], epgauto=True)
                mux = muxes[item["mux_uuid"]]
                if mux["enabled"] != 1 or mux["epg"] == 0:
                    await self.backend.save(uuid=mux["uuid"], enabled=1, epg=1)
                    mux.update(enabled=1, epg=1)
                previous = channels.get(mapped["uuid"])
                channel = Channel.parse({k: v for k, v in {**item, "uuid": mapped["uuid"],
                    "name": previous.name if previous else item["name"]}.items() if k != "encrypted"})
                channels[channel.uuid] = channel
                chosen.append(channel.uuid)
                highest = max(highest, int(mapped["number"]))
            if highest > acl["channel_max"]:
                # Only channel range changes; playback never gains API/admin rights.
                await self.backend.save(uuid=acl["uuid"], channel_max=highest)
            data = {"version": 1, "channels": [asdict(c) for c in channels.values()], "favourites": chosen}
            library(data)
            await self.writer.write(self.library_path, data, 0o640)
            return data

    async def close(self):
        await stop_task(self.task)
