"""Source browser API shared by first setup and authenticated management."""
import asyncio
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import Field
from satip_controller.backend import BackendError
from satip_controller.enigma2 import Enigma2
from satip_controller.http import StrictRequest
from satip_controller.state import read_json
from . import discovery
from .source_browser import Importer, search_enigma2
from .tv_source import public_status, profile_path, validate_profile


class SourceSearch(StrictRequest):
    source: Literal["satip", "enigma2"]


class Receiver(StrictRequest):
    address: str = Field(min_length=7, max_length=15)
    web_port: int = Field(default=80, ge=1, le=65535)
    stream_port: int = Field(default=8001, ge=1, le=65535)

    def receiver(self):
        return Enigma2(self.address, self.web_port, self.stream_port)


class BouquetImport(Receiver):
    bouquets: list[str] = Field(min_length=1, max_length=16)
    timeout: int = Field(default=20, ge=5, le=90)
    confirm_pause: bool = False


def install(app, backend, config, store, owner=None, preview=None, setup=None):
    store = Path(store)
    importer = Importer(backend, config, store, owner, prepare=setup.prepare_enigma2 if setup else None)
    searches = {s: {"state": "idle", "servers": [], "warnings": [], "interfaces": []}
                for s in ("satip", "enigma2")}
    tasks = {}
    app.state.source_importer = importer
    app.state.source_tasks = tasks

    def quiet_changes(*, recovery=False):
        if any((Path("/run") / p).exists() for p in
               ("satip-source-maintenance.json", "satip-backup/maintenance.json")):
            raise HTTPException(409, "Device maintenance is running")
        if not recovery and (store / "web/satip-tuner-transaction.json").exists():
            raise HTTPException(409, "Recover the interrupted SAT-IP tuner change first")
        if (store / "network/wifi-trial.json").exists():
            raise HTTPException(409, "Finish the Wi-Fi trial first")
        if preview and preview.status.get("state") in ("starting", "ready"):
            raise HTTPException(409, "Stop browser viewing first")
        if setup and (setup.manager or setup.registration_task and not setup.registration_task.done()):
            if not (setup.manager and setup.state.get("source") == "enigma2"):
                raise HTTPException(409, "Source selection is committed; finish the current setup")
        if setup and (store / "web/setup-complete.json").exists():
            raise HTTPException(409, "Setup is finishing")

    async def search(source):
        try:
            result = await asyncio.to_thread(search_enigma2 if source == "enigma2" else discovery.search_servers)
            searches[source] = {**result, "state": "complete"}
        except (OSError, ValueError, BackendError):
            searches[source].update(state="error", error="Discovery failed; retry or enter the receiver address manually")

    @app.get("/api/sources")
    async def status():
        data = public_status(store)
        path = profile_path(store, "enigma2")
        if path.exists():
            info = validate_profile(read_json(path))["enigma2"]
            data["receiver"] = {k: info[k] for k in ("address", "web_port", "stream_port", "bouquets")}
        elif setup and setup.manager and setup.state.get("receiver"):
            data["receiver"] = setup.state["receiver"]
        result = importer.public_status()
        pending = any((store / "web/enigma2-import" / name).exists() for name in
                      ("mux-rollback.json", "network-rollback.json", "publish-rollback.json"))
        if pending and not (importer.task and not importer.task.done()):
            result = {**result, "state": "error", "recovery_pending": True,
                      "error": "An interrupted bouquet import needs recovery before retrying"}
        if setup and importer.task and importer.task.done() and result.get("state") == "complete":
            setup.accept_enigma2()
        from .satip_tuner import inventory
        try:
            data["satip"] = await inventory(backend, store)
        except (OSError, ValueError, BackendError):
            data["satip"] = {"error": "SAT-IP status unavailable; saved configuration is not a live connection",
                             "connected": False, "tuners": [], "receiving": []}
        return {**data, "discovery": searches, "import": result}

    @app.post("/api/sources/discover", status_code=202)
    async def discover(payload: SourceSearch):
        source = payload.source
        if source not in tasks or tasks[source].done():
            searches[source].update(state="searching", error=None)
            tasks[source] = asyncio.create_task(search(source))
        return {"state": "searching"}

    @app.post("/api/sources/bouquets")
    async def bouquets(payload: Receiver):
        try:
            receiver = payload.receiver()
            info = await asyncio.to_thread(receiver.about)
            items = await asyncio.to_thread(receiver.bouquets)
            return {"receiver": info, "bouquets": items}
        except (ValueError, BackendError) as error:
            raise HTTPException(400, str(error)) from None

    @app.post("/api/sources/import", status_code=202)
    async def import_bouquets(payload: BouquetImport):
        quiet_changes()
        if any((store / "web/enigma2-import" / name).exists() for name in
               ("mux-rollback.json", "network-rollback.json", "publish-rollback.json")):
            raise HTTPException(409, "Recover the interrupted bouquet import first")
        if not payload.confirm_pause:
            raise HTTPException(400, "Confirm that importing pauses television playback")
        if len(set(payload.bouquets)) != len(payload.bouquets) or any(
                not b or len(b) > 150 or any(ord(c) < 32 for c in b) for b in payload.bouquets):
            raise HTTPException(400, "Select distinct TV bouquets")
        try:
            if setup:
                # Retry only after the previous source scan has fully stopped.
                if setup.task and not setup.task.done():
                    raise BackendError("Setup scan is active")
                committed = setup.state.get("receiver") if setup.manager else None
                if committed and any(committed[k] != payload.model_dump()[k] for k in ("address", "web_port", "stream_port")):
                    raise BackendError("Receiver identity is committed; retry the same Enigma2 receiver")
                importer.owner = setup.manager
            importer.start(payload.receiver(), payload.bouquets, payload.timeout)
            if setup:
                setup.task = importer.task
            return {"state": "starting"}
        except (ValueError, BackendError) as error:
            raise HTTPException(409, str(error)) from None

    @app.post("/api/sources/cancel")
    async def cancel(payload: StrictRequest):
        await importer.close()
        return importer.public_status()

    @app.post("/api/sources/recover")
    async def recover(payload: StrictRequest):
        quiet_changes(recovery=True)
        if importer.task and not importer.task.done():
            raise HTTPException(409, "Stop the import first")
        manager = owner or (setup.manager if setup else None)
        if manager and (manager.lock.locked() or manager.task and not manager.task.done()):
            raise HTTPException(409, "Stop channel search or finish editing first")
        try:
            from .tv_source import recover as recover_source
            await recover_source(backend, store)
            if importer.manager:
                await importer.manager.recover()
            if owner:
                await owner.recover()
            elif setup and setup.manager:
                await setup.manager.recover()
            importer.status = {"state": "idle", "recovery_pending": False}
            return importer.status
        except (ValueError, OSError, BackendError):
            raise HTTPException(409, "Source recovery failed; check backend and retry") from None

    return importer
