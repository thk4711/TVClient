"""Select password-free first setup or existing management, never both."""
import asyncio
from contextlib import asynccontextmanager
import fcntl
from pathlib import Path
import secrets

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field

from .setup import Setup
from satip_controller.backend import Backend, BackendError
from satip_controller.config import credentials
from satip_controller.state import atomic_json, read_json
from satip_controller.http import (local_hosts, LocalHostMiddleware, RequestLimits,
                                   StrictRequest, UUIDString, check_action)


def allowed_hosts():
    from .mdns import local_name
    return sorted(set(local_hosts()) | {local_name()})

class Selection(StrictRequest):
    server_uuid: str = Field(pattern=r"^[0-9a-f]{32}$")
    frontend_uuid: str = Field(pattern=r"^[0-9a-f]{32}$")
    # Legacy clients may still supply these; the appliance selects Astra by default.
    scanfile: str | None = Field(default=None, min_length=1, max_length=200)
    confirm_scan: bool = True


class Lineup(StrictRequest):
    services: list[UUIDString] = Field(min_length=1, max_length=500)


class ServerURL(StrictRequest):
    url: str = Field(min_length=1, max_length=512)


class ServerSelection(ServerURL):
    udn: str = Field(min_length=1, max_length=64)


def create_setup_app(store=Path("/data/store"), backend=None, hosts=None, transition=None, device_helper=None):
    store = Path(store)
    fixed_hosts = hosts
    hosts = hosts or allowed_hosts()
    setup = Setup(backend or Backend(credentials(store / "config/web/tvheadend-admin.json")),
                  store, read_json(store / "config/web/setup.json"), hosts)
    async def authenticate(request: Request):
        if read_json(store / "installation.json").get("state") != "unconfigured":
            raise HTTPException(409, "Setup finished. Reload to open your receiver.")
        check_action(request)

    async def watch():
        while True:
            await asyncio.sleep(1)
            if read_json(store / "installation.json").get("state") != "unconfigured":
                transition()
                return

    @asynccontextmanager
    async def lifespan(app):
        with (store / "web/web.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                await setup.recover()
            except (BackendError, OSError, ValueError):
                pass  # Durable journal blocks new mutations until explicit recovery.
            watcher = asyncio.create_task(watch()) if transition else None
            setup.resume_registration()
            try:
                yield
            finally:
                if watcher:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
                await source_importer.close()
                await asyncio.gather(*app.state.source_tasks.values(), return_exceptions=True)
                if setup.manager:
                    await setup.manager.close()
                await setup.close_discovery()

    app = FastAPI(title="SAT-IP first setup", lifespan=lifespan, docs_url=None,
                  redoc_url=None, openapi_url=None, dependencies=[Depends(authenticate)])
    app.state.setup = setup
    app.add_middleware(RequestLimits, backup_upload=True)
    from .device_api import install as install_device
    from .device_client import request as device_request
    install_device(app, store / "config/web/login.json", helper=device_helper or device_request,
                   backup_root=store.parent.parent / "run", require_auth=lambda: False, setup_only=True)
    app.add_middleware(LocalHostMiddleware, provider=lambda: fixed_hosts or allowed_hosts())
    from satip_controller.recovery import LocalHealth
    app.add_middleware(LocalHealth)
    from satip_controller.manual import install as install_manual
    install_manual(app)

    @app.get("/app.css")
    async def stylesheet():
        return FileResponse(Path(__file__).parent.parent / "satip_controller/static/app.css", media_type="text/css")

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(BackendError)
    async def backend_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(ValueError)
    async def validation_error(request, error):
        return JSONResponse({"detail": "Invalid setup selection or backend state"}, status_code=400)

    @app.get("/request.js")
    async def request_script():
        return FileResponse(Path(__file__).parents[1] / "satip_controller/static" / "request.js", media_type="text/javascript")

    @app.get("/")
    async def index():
        return FileResponse(Path(__file__).parents[1] / "satip_controller/static/setup.html")

    @app.get("/setup.js")
    async def script():
        return FileResponse(Path(__file__).parents[1] / "satip_controller/static/setup.js", media_type="text/javascript")

    from .source_api import install as install_sources
    from .provision import web_config
    source_config = web_config("0" * 32, setup.preferences["playback_acl_uuid"],
                               setup.preferences["settings"], hosts)
    source_config.update(private_dir=str(store / "web"), management_dir=str(store / "management"),
                         library=str(store / "management/library.json"), player_state_dir=str(store / "player"))
    source_importer = install_sources(app, setup.backend, source_config, store, setup=setup)
    setup.source_importer = source_importer

    @app.get("/sources.js")
    async def source_script():
        return FileResponse(Path(__file__).parents[1] / "satip_controller/static/sources.js", media_type="text/javascript")

    @app.get("/api/setup/status")
    async def status():
        if source_importer.task and source_importer.task.done() and source_importer.status.get("state") == "complete":
            setup.accept_enigma2()
        manager = setup.manager
        candidates = store / "web/candidates.json"
        channels = read_json(candidates).get("channels", []) if candidates.exists() else []
        from .starter import lineup
        return {"state": "unconfigured", "source": setup.state.get("source", "satip") if manager else None,
                "source_import": source_importer.public_status(),
                "prepared": bool(manager and setup.state.get("prepared")),
                "discovery": setup.discovery, "registration": setup.registration,
                "selected_server": setup.selected, "selection_locked": manager is not None or
                    bool(source_importer.task and not source_importer.task.done()),
                "frontend_uuid": setup.state.get("frontend_uuid") if manager else None,
                "preferred_tuner_number": int(setup.preferences["settings"]["tuner_number"])
                    if "tuner_number" in setup.preferences["settings"] else None,
                "tuners": setup.available_tuners, "scanfiles": setup.available_scanfiles,
                "scan": source_importer.public_status() if source_importer.task and not source_importer.task.done()
                    else manager.status if manager else {"state": "idle"},
                # A journal during a live scan is normal, not an interrupted scan.
                "recovery_pending": (store / "web/mux-rollback.json").exists() and
                    not (setup.task and not setup.task.done()),
                "candidates": channels, "starter": lineup(channels),
                "completion_pending": (store / "web/setup-complete.json").exists()}

    @app.post("/api/setup/discover")
    async def discover():
        setup.start_discovery()
        return {"state": "searching"}

    @app.get("/api/setup/inputs")
    async def inputs():
        from .device_client import request
        return await request("setup_inputs")

    @app.get("/api/setup/preferences")
    async def preferences():
        from .device_client import request
        data = await request("status")
        return {k: data[k] for k in ("hostname", "timezone", "timezones")}

    class Preferences(StrictRequest):
        hostname: str = Field(min_length=1, max_length=63)
        timezone: str = Field(min_length=1, max_length=100)

    @app.post("/api/setup/preferences")
    async def save_preferences(selection: Preferences):
        from .device_client import request
        return await request("device", selection.model_dump())

    class InputSelection(StrictRequest):
        id: str = Field(pattern=r"^(ir|[0-9a-f]{4}:[0-9a-f]{4})$")

    @app.post("/api/setup/input")
    async def select_input(selection: InputSelection):
        from .device_client import request
        async with setup.lock:
            if (store / "web/setup-complete.json").exists():
                raise HTTPException(409, "Setup is finishing")
            result = await request("setup_input", {"id": selection.id})
            setup.preferences = read_json(store / "config/web/setup.json")
            return result

    @app.post("/api/setup/config/restore")
    async def restore_config(selection: StrictRequest):
        async with setup.lock:
            identifier = secrets.token_hex(16)
            atomic_json(store / "web/config-restore-request.json", {"request_id": identifier})
            for _ in range(100):
                await asyncio.sleep(.1)
                path = store / "web/config-restore-result.json"
                result = read_json(path) if path.exists() else {}
                if result.get("request_id") == identifier:
                    if result.get("state") != "restored":
                        raise HTTPException(409, "Could not restore satip-config.txt. Check the boot FAT mount and image template, then retry.")
                    return {"state": "restored"}
            raise HTTPException(504, "Restore confirmation timed out. Check recovery access and retry.")

    @app.post("/api/setup/server/add")
    async def add_server(selection: ServerURL):
        return await setup.add_server(selection.url)

    @app.post("/api/setup/server/select", status_code=202)
    async def select_server(selection: ServerSelection):
        await setup.select_server(selection.url, selection.udn)
        return {"state": "connecting"}

    @app.post("/api/setup/scan", status_code=202)
    async def scan(selection: Selection):
        if not selection.confirm_scan:
            raise HTTPException(400, "Confirm exclusive tuner use for setup")
        await setup.configure(selection.server_uuid, selection.frontend_uuid, selection.scanfile)
        return {"state": "scanning"}

    @app.post("/api/setup/channels/prepare")
    async def prepare_channels(selection: Selection):
        if not selection.confirm_scan:
            raise HTTPException(400, "Confirm exclusive tuner use for setup")
        await setup.configure(selection.server_uuid, selection.frontend_uuid, selection.scanfile, start_scan=False)
        return {"state": "prepared"}

    @app.post("/api/setup/cancel")
    async def cancel():
        async with setup.lock:
            if setup.manager:
                await setup.manager.close()
        return {"state": "cancelled"}

    @app.post("/api/setup/recover")
    async def recover():
        await setup.recover()
        return {"state": "recovered"}

    @app.post("/api/setup/finish")
    async def finish(lineup: Lineup):
        setup.hosts = fixed_hosts or allowed_hosts()
        await setup.finish(lineup.services)
        return {"state": "completion-pending", "message": "Reload after management restarts"}

    return app


def main():
    import uvicorn
    store = Path("/data/store")
    state = read_json(store / "installation.json").get("state")
    if state == "unconfigured":
        app = create_setup_app(store, transition=lambda: setattr(server, "should_exit", True))
    elif state in ("configured", "qualified"):
        from satip_controller.web import create_app
        config = read_json(store / "config/web/config.json")
        if state == "configured":
            config["allowed_hosts"] = allowed_hosts()
        app = create_app(config, hosts_provider=allowed_hosts if state == "configured" else None, appliance=True,
                         auth_provider=lambda: read_json(store / "config/web/config.json").get("require_auth", True))
    else:
        raise SystemExit("Installation is not ready for management")
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8080, access_log=False, proxy_headers=False))
    server.run()


if __name__ == "__main__":
    main()
