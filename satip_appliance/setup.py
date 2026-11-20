"""Fresh backend setup over its pinned loopback API; no root execution here."""
import asyncio
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

from . import discovery
from .provision import web_config
from .registration import read_request
from satip_controller.backend import BackendError
from satip_controller.config import uuid
from satip_controller.scan import Manager
from satip_controller.state import atomic_json, read_json
from satip_controller.lifecycle import complete

NETWORK_NAME = "SAT-IP setup Astra 19.2E"


def flatten(node):
    return {"uuid": node["uuid"], **{p["id"]: p.get("value") for p in node.get("params", [])}}


async def nodes(backend, classname):
    result = await backend.request("idnode/load", **{"class": classname})
    return [flatten(node) for node in result.get("entries", [])]


async def verify_access(backend, acl_uuid):
    entries = await backend.grid("access/entry/grid")
    if len(entries) != 2 or {a.get("username") for a in entries} != {"satip-admin", "satip-playback"} or any(a.get("enabled") is not True or a.get("prefix") != "127.0.0.1/32"
                              or a.get("username") not in ("satip-admin", "satip-playback") for a in entries):
        raise BackendError("Unexpected backend access policy")
    acl = next((a for a in entries if a["uuid"] == acl_uuid), None)
    if (not acl or acl.get("username") != "satip-playback" or acl.get("admin") or acl.get("webui")
            or acl.get("dvr") or acl.get("streaming") != ["basic"] or acl.get("conn_limit") != 1):
        raise BackendError("Unsafe playback account")
    profiles = await backend.grid("profile/list")
    allowed = acl.get("profile", [])
    if len(allowed) != 1 or not any(p.get("uuid", p.get("key")) == allowed[0]
                                   and p.get("name", p.get("val")) == "pass" for p in profiles):
        raise BackendError("Playback must be restricted to pass")
    return acl


class Setup:
    def __init__(self, backend, store, preferences, hosts):
        self.backend, self.store = backend, Path(store)
        self.preferences, self.hosts = preferences, hosts
        self.state_path = self.store / "web/setup-state.json"
        self.lock = asyncio.Lock()
        self.manager = None
        self.task = None
        self.discovery_task = None
        self.registration_task = None
        self.discovery = {"state": "idle", "servers": [], "interfaces": [], "warnings": []}
        self.registration = {"state": "idle"}
        self.available_tuners = []
        self.available_scanfiles = []
        selection = self.store / "web/server-selection.json"
        self.selected = read_json(selection) if selection.exists() else None
        request_path = self.store / "web/server-request.json"
        if request_path.exists():
            request = read_request(request_path)
            if not self.selected or any(self.selected[k] != request[k] for k in ("url", "udn")):
                # Recover a crash between request and display-metadata publication.
                host = urlsplit(request["url"]).hostname
                self.selected = {"url": request["url"], "udn": request["udn"],
                                 "name": host, "address": host, "tuners": 0}
        if self.selected:
            self.discovery["servers"] = [self.selected]
        if self.state_path.exists():
            self.attach(read_json(self.state_path))

    def attach(self, state):
        uuid(state["network_uuid"])
        self.state = state
        config = web_config(state["network_uuid"], self.preferences["playback_acl_uuid"],
                            self.preferences["settings"], self.hosts)
        config.update(private_dir=str(self.store / "web"), management_dir=str(self.store / "management"),
                      library=str(self.store / "management/library.json"), player_state_dir=str(self.store / "player"))
        self.manager = Manager(self.backend, config, setup=True)

    def prepare_enigma2(self, network, receiver, bouquets):
        if not self.manager:
            self.attach({"source": "enigma2", "network_uuid": network, "prepared": False,
                         "receiver": {"address": receiver.host, "web_port": receiver.web_port,
                                      "stream_port": receiver.stream_port, "bouquets": bouquets}})
            self.available_tuners = []
            self.available_scanfiles = []
            atomic_json(self.state_path, self.state)

    def accept_enigma2(self):
        importer = self.source_importer
        if getattr(self, "accepted_import", None) is importer.task:
            return
        from .tv_source import validate_profile, profile_path
        node = validate_profile(read_json(profile_path(self.store, "enigma2")))
        self.attach({"source": "enigma2", "network_uuid": node["network_uuid"], "prepared": True})
        atomic_json(self.state_path, self.state)
        atomic_json(self.store / "management/library.json", node["library"], 0o640)
        atomic_json(self.store / "web/candidates.json", node["candidates"])
        self.manager.status = importer.status
        self.accepted_import = importer.task

    async def discover(self):
        servers = await self.backend.request("hardware/tree", uuid="root")
        result = []
        for server in servers:
            if server.get("class") != "satip_client":
                continue
            fields = flatten(server)
            if self.selected and fields.get("device_uuid") != self.selected["udn"][5:]:
                continue
            children = await self.backend.request("hardware/tree", uuid=server["uuid"])
            for frontend in children:
                if frontend.get("class") != "satip_frontend_dvbs":
                    continue
                tuner = flatten(frontend)
                result.append({"server_uuid": uuid(server["uuid"]), "frontend_uuid": uuid(frontend["uuid"]),
                               "server": fields.get("addr", ""), "description": server.get("text", server.get("caption", "SAT-IP")),
                               "tuner_number": tuner.get("fe_number")})
        return result

    def start_discovery(self):
        if self.discovery_task and not self.discovery_task.done():
            return
        self.discovery.update(state="searching", error=None)
        self.discovery_task = asyncio.create_task(self.search_servers())

    async def search_servers(self):
        try:
            result = await complete(asyncio.create_task(asyncio.to_thread(discovery.search_servers)))
            # Retain manually entered servers and the persisted selection.
            known = {server["udn"]: server for server in self.discovery["servers"] if server.get("manual")
                     or (self.selected and server["udn"] == self.selected["udn"])}
            known.update({server["udn"]: server for server in result.pop("servers")})
            initial_url = self.preferences["settings"].get("satip_xml")
            if initial_url and not known:
                try:
                    server = await complete(asyncio.create_task(asyncio.to_thread(discovery.describe, initial_url)))
                    known[server["udn"]] = server
                except ValueError:
                    result["warnings"].append("The provisioned server description is unreachable")
            self.discovery.update(result, servers=list(known.values()), state="complete")
        except (OSError, ValueError):
            self.discovery.update(state="error", error="LAN discovery failed. Retry or add a description URL.")

    async def add_server(self, url):
        try:
            server = await complete(asyncio.create_task(asyncio.to_thread(discovery.describe, url)))
        except ValueError as error:
            raise BackendError(str(error)) from None
        server["manual"] = True
        known = {item["udn"]: item for item in self.discovery["servers"]}
        known[server["udn"]] = server
        self.discovery["servers"] = list(known.values())
        return server

    async def select_server(self, url, udn):
        async with self.lock:
            if self.task and not self.task.done():
                raise BackendError("Stop the current source scan before selecting a server")
            if self.manager and self.state.get("source") == "enigma2":
                raise BackendError("Enigma2 source is committed; finish its setup")
            if self.registration_task and not self.registration_task.done():
                raise BackendError("Server registration is in progress")
            if self.manager and self.selected and self.selected["url"] == url and self.selected["udn"] == udn:
                self.resume_registration()
                return
            if self.manager or (self.store / "web/mux-rollback.json").exists():
                raise BackendError("Server selection is committed; resume the existing setup")
            chosen = next((s for s in self.discovery["servers"] if s["url"] == url and s["udn"] == udn), None)
            if not chosen:
                raise ValueError("Discover or add the server first")
            server = await self.add_server(url)
            if server["udn"] != udn:
                raise BackendError("Server identity changed; discover and select it again")
            if await self.backend.grid("status/subscriptions"):
                raise BackendError("Another client owns the tuner")
            request = {"url": url, "udn": udn, "request_id": secrets.token_hex(16)}
            # Commit the pending request before metadata; recovery reuses its ID.
            atomic_json(self.store / "web/server-request.json", request)
            atomic_json(self.store / "web/server-selection.json", server)
            self.selected = server
            self.available_tuners = []
            self.resume_registration()

    def resume_registration(self):
        if self.selected:
            self.registration = {"state": "connecting"}
            self.registration_task = asyncio.create_task(self.wait_for_server())

    async def wait_for_server(self):
        end = time.monotonic() + 30
        request = read_json(self.store / "web/server-request.json")
        while time.monotonic() < end:
            result_path = self.store / "web/server-result.json"
            result = read_json(result_path) if result_path.exists() else {}
            if result.get("request_id") == request["request_id"]:
                if result.get("state") == "rejected":
                    self.registration = {"state": "error", "error": result["error"]}
                    return
                try:
                    if result.get("state") != "accepted":
                        raise BackendError("Server registration has not been accepted")
                    tuners = await self.discover()
                    if tuners:
                        self.available_scanfiles = await self.scanfiles()
                        self.available_tuners = tuners
                        self.registration = {"state": "ready"}
                        return
                except BackendError:
                    pass  # Backend is restarting or fetching the selected description.
            await asyncio.sleep(.5)
        self.registration = {"state": "error", "error":
            "Server connection timed out. Check server connectivity and choose Retry connection."}

    async def close_discovery(self):
        for task in (self.discovery_task, self.registration_task):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.discovery_task, self.registration_task) if t), return_exceptions=True)

    async def scanfiles(self):
        entries = (await self.backend.request("dvb/scanfile/list", type="dvbs", satpos=192)).get("entries", [])
        return list({entry["key"]: entry for entry in entries}.values())

    async def configure(self, server, frontend, scanfile=None):
        async with self.lock:
            if self.selected and self.registration["state"] != "ready":
                raise BackendError("Wait for the selected server to register")
            if self.task and not self.task.done():
                raise BackendError("Setup scan is active")
            if (self.store / "web/mux-rollback.json").exists():
                raise BackendError("Recover the interrupted scan first")
            if (self.store / "web/setup-complete.json").exists():
                raise BackendError("Initial lineup is being saved; wait for management to start")
            if self.manager and self.state.get("source") == "enigma2":
                raise BackendError("Enigma2 source is committed; finish its setup")
            chosen = next((t for t in await self.discover() if t["server_uuid"] == server and t["frontend_uuid"] == frontend), None)
            settings = self.preferences["settings"]
            if not chosen:
                raise ValueError("Select an available DVB-S tuner")
            available = {s["key"] for s in await self.scanfiles()}
            if scanfile is None:
                # Preserve committed identities on retry. Prefer the full Astra
                # definition over a definition for an individual spacecraft.
                scanfile = self.state["scanfile"] if self.manager else next(iter(sorted(available)), None)
            if scanfile is None or scanfile not in available:
                raise ValueError("Select a backend-provided Astra 19.2E mux list")
            selection = {"server_uuid": server, "frontend_uuid": frontend, "scanfile": scanfile}
            if self.manager and any(self.state[k] != selection[k] for k in ("server_uuid", "scanfile")):
                raise BackendError("Server and satellite selection are already committed")
            if self.manager and self.state["frontend_uuid"] != frontend and self.manager.library_path.exists():
                raise BackendError("Initial lineup is already saved; tuner change is unavailable")
            await verify_access(self.backend, self.preferences["playback_acl_uuid"])
            if await self.backend.grid("status/subscriptions"):
                raise BackendError("Another client owns the tuner")
            if self.manager:
                # Any interrupted re-preparation must block publishing a lineup.
                self.state["prepared"] = False
                atomic_json(self.state_path, self.state)
            # Disable every frontend before creating muxes or assigning networks.
            for tuner in await nodes(self.backend, "satip_frontend"):
                await self.backend.save(uuid=tuner["uuid"], enabled=False, ota_epg=False, initscan=False, idlescan=False)
            networks = await self.backend.grid("mpegts/network/grid")
            matching = [n for n in networks if n.get("networkname") == NETWORK_NAME]
            if len(matching) > 1 or any(n.get("networkname") != NETWORK_NAME for n in networks):
                raise BackendError("Unexpected fresh-backend networks; no overwrite")
            if matching:
                network = uuid(matching[0]["uuid"])
            else:
                created = await self.backend.request("mpegts/network/create", write=True,
                    **{"class": "dvb_network_dvbs", "conf": {"networkname": NETWORK_NAME,
                       "orbital_pos": "19.2E", "skipinitscan": True, "idlescan": False, "autodiscovery": False}})
                network = uuid(created["uuid"])
            # Persist selection before remaining idempotent writes; restart can
            # recover the same actual network identity without duplicate creation.
            self.attach({**selection, "network_uuid": network})
            atomic_json(self.state_path, self.state)
            muxes = [m for m in await self.backend.grid("mpegts/mux/grid") if m.get("network_uuid") == network]
            # Reapplying the selected pinned scanfile deduplicates transponders
            # and fills in an interrupted creation without discarding identities.
            await self.backend.save(uuid=network, scanfile=scanfile)
            muxes = [m for m in await self.backend.grid("mpegts/mux/grid") if m.get("network_uuid") == network]
            if not 1 <= len(muxes) <= 256:
                raise BackendError("Missing or oversized initial mux list")
            for mux in muxes:
                await self.backend.save(uuid=mux["uuid"], enabled=0, epg=0, scan_state=0)
            # Source/position entries belong to the selected frontend, not to a
            # remembered database UUID. Disable all other source assignments.
            await self.backend.save(uuid=frontend, positions=int(settings["satip_source"]))
            positions = await self.backend.request("hardware/tree", uuid=frontend)
            found = False
            for entry in positions:
                position = flatten(entry)
                selected = position.get("position") == int(settings["satip_source"])
                await self.backend.save(uuid=position["uuid"], enabled=selected, networks=[network] if selected else [])
                found |= selected
            if not found:
                raise BackendError("Selected satellite source is unavailable")
            # Explicit scans need initial-scan permission; startup scanning is
            # disabled on the network, idle and dedicated EPG tuning stay off.
            await self.backend.save(uuid=frontend, enabled=True, ota_epg=False, initscan=True, idlescan=False)
            active = [t for t in await nodes(self.backend, "satip_frontend") if t.get("enabled")]
            if len(active) != 1 or active[0]["uuid"] != frontend:
                raise BackendError("Single frontend ownership readback failed")
            self.state["prepared"] = True
            atomic_json(self.state_path, self.state)
            self.task = asyncio.create_task(self.run_scan())
            self.manager.task = self.task

    async def run_scan(self):
        try:
            from .starter import mux_order
            await self.manager.scan(mux_order=mux_order)
        except (BackendError, OSError, ValueError):
            self.manager.status.update(state="error", error="Initial scan failed; inspect tuner or recover and retry")
            self.manager.remember_status()

    async def recover(self):
        async with self.lock:
            if self.task and not self.task.done():
                raise BackendError("Scan is active")
            if self.manager:
                await self.manager.recover()

    async def finish(self, services):
        async with self.lock:
            if not self.manager or not self.state.get("prepared") or (self.task and not self.task.done()):
                raise BackendError("Complete the initial scan first")
            await verify_access(self.backend, self.preferences["playback_acl_uuid"])
            # No active subscriptions while mapping; never probe another stream.
            await self.manager.quiet()
            try:
                data = await self.manager.save_favourites(services)
                if self.state.get("source") == "enigma2":
                    from .tv_source import profile_path, validate_profile
                    path = profile_path(self.store, "enigma2")
                    node = validate_profile(read_json(path))
                    atomic_json(path, {**node, "library": data}, 0o640)
                    await self.backend.save(uuid=self.state["network_uuid"], enabled=True)
                config = web_config(self.state["network_uuid"], self.preferences["playback_acl_uuid"],
                                    self.preferences["settings"], self.hosts)
                atomic_json(self.store / "config/web/config.json", config)
                # Root independently validates fixed-path outputs before changing
                # installation state. The non-root UI cannot mark it qualified.
                atomic_json(self.store / "web/setup-complete.json", {
                    "network_uuid": self.state["network_uuid"], "playback_acl_uuid": self.preferences["playback_acl_uuid"]})
            finally:
                self.manager.lease(86400 if self.manager.journal.exists() else 0)
