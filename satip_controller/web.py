"""Standalone management/preview; no evdev/DRM or access to player-private files."""
import argparse
import asyncio
import fcntl
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import Field

from .http import StrictRequest, UUIDString, Revision, RequestLimits, LocalHostMiddleware, check_action
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .backend import Backend, BackendError
from .config import credentials, library, uuid
from .epg import EPGCache, channel_events
from .scan import Manager, ConcurrentEdit
from .preview import Preview, PreviewError
from .state import read_json, runtime_path, library_revision
from .telemetry import Telemetry
from .settings import load_settings, settings_path
from .youtube import preferences as youtube_preferences, preferences_path as youtube_preferences_path, public_status as youtube_status
from .state import JSONWriter


class OSDSettings(StrictRequest):
    osd_timeout_ms: int = Field(ge=500, le=30000)
    osd_language: Literal["en", "de"] = "en"


class YouTubeRequest(StrictRequest):
    action: str = Field(pattern=r"^(pair|disable|unlink)$")


class Favourites(StrictRequest):
    services: list[UUIDString] = Field(min_length=1, max_length=500)
    base_revision: Revision | None = None
    save_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class ScanRequest(StrictRequest):
    enabled_only: bool = False
    confirm_pause: bool = False


class PreviewRequest(StrictRequest):
    service: UUIDString
    confirm_pause: bool = False


class PreviewSession(StrictRequest):
    session: str = Field(pattern=r"^[0-9a-f]{32}$")


def create_app(config, backend=None, *, hosts_provider=None, appliance=False, auth_provider=None):
    required = {"version", "library", "management_dir", "player_state_dir", "private_dir",
                "admin_credentials", "web_credentials", "network_uuid", "playback_acl_uuid", "mux_timeout"}
    optional = {"listen_host", "listen_port", "allowed_hosts", "require_auth", "preview_credentials", "preview_runtime_dir"}
    if not isinstance(config, dict):
        raise ValueError("Management config must be an object")
    if type(config.get("version")) is not int or config["version"] != 1:
        raise ValueError("Invalid management config version")
    if not required <= set(config) or set(config) - required - optional:
        raise ValueError("Missing or unknown management config fields")
    listen_host = config.get("listen_host", "127.0.0.1")
    listen_port = config.get("listen_port", 8080)
    allowed_hosts = config.get("allowed_hosts", ["localhost", "127.0.0.1", "testserver"])
    require_auth = config.get("require_auth", True)
    if not isinstance(listen_host, str) or not listen_host:
        raise ValueError("Invalid management listen address")
    if type(listen_port) is not int or not 1 <= listen_port <= 65535:
        raise ValueError("Invalid management listen port")
    if not isinstance(allowed_hosts, list) or not allowed_hosts or any(not isinstance(host, str) or not host for host in allowed_hosts):
        raise ValueError("Invalid management allowed hosts")
    if type(require_auth) is not bool:
        raise ValueError("Invalid management authentication setting")
    for field in required - {"version", "network_uuid", "playback_acl_uuid", "mux_timeout"}:
        if not isinstance(config[field], str) or not Path(config[field]).is_absolute():
            raise ValueError("Management paths must be absolute")
    uuid(config["network_uuid"])
    uuid(config["playback_acl_uuid"])
    if type(config["mux_timeout"]) not in (int, float) or not 0 < config["mux_timeout"] <= 180:
        raise ValueError("Invalid scan timeout")
    login = credentials(Path(config["web_credentials"])) if require_auth else None
    manager = Manager(backend or Backend(credentials(Path(config["admin_credentials"]))), config)
    for field in ("preview_credentials", "preview_runtime_dir"):
        if field in config and (not isinstance(config[field], str) or not Path(config[field]).is_absolute()):
            raise ValueError("Preview paths must be absolute")
    preview = Preview(manager, config.get("preview_credentials"), config.get("preview_runtime_dir"))
    telemetry = Telemetry(manager.backend, manager.directory)
    settings_writer = JSONWriter()
    settings_lock = asyncio.Lock()
    security = HTTPBasic(auto_error=False)
    def auth_enabled():
        return auth_provider() if auth_provider else require_auth

    async def authenticate(request: Request, supplied: HTTPBasicCredentials = Depends(security)):
        if auth_enabled():
            login = credentials(Path(config["web_credentials"]))
            valid_user = supplied is not None and secrets.compare_digest(supplied.username.encode(), login["username"].encode())
            valid_password = supplied is not None and secrets.compare_digest(supplied.password.encode(), login["password"].encode())
            if not (valid_user and valid_password):
                raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Basic"})
        check_action(request)

    @asynccontextmanager
    async def lifespan(app):
        with (manager.directory / "web.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            await preview.recover()
            try:
                await manager.recover()
            except (BackendError, OSError, ValueError):
                manager.status.update(state="error", recovery_pending=manager.journal.exists(),
                                      error="Backend unavailable or scan recovery pending")
            try:
                epg_task = asyncio.create_task(EPGCache(manager.backend, config).run())
                yield
            finally:
                epg_task.cancel()
                await asyncio.gather(epg_task, return_exceptions=True)
                await preview.stop()
                if appliance:
                    await source_importer.close()
                    await asyncio.gather(*app.state.source_tasks.values(), return_exceptions=True)
                await manager.close()

    app = FastAPI(title="SAT-IP receiver", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None, dependencies=[Depends(authenticate)])
    app.state.manager = manager
    app.state.preview = preview
    app.state.telemetry = telemetry
    app.add_middleware(RequestLimits, backup_upload=appliance)
    if hosts_provider is None:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    else:
        app.add_middleware(LocalHostMiddleware, provider=hosts_provider)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; media-src 'self' blob:; worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'"
        return response

    from .recovery import LocalHealth
    app.add_middleware(LocalHealth)

    from .manual import install as install_manual
    install_manual(app)

    # Explicit static allowlist: route path -> (filename, media type). index.html
    # keeps its default media type; everything else is served from this table only.
    static_root = Path(__file__).parent / "static"
    static_assets = {
        "/": ("index.html", None),
        "/request.js": ("request.js", "text/javascript"),
        "/ui.js": ("ui.js", "text/javascript"),
        "/hls.min.js": ("hls.min.js", "text/javascript"),
        "/app.css": ("app.css", "text/css"),
        "/app.js": ("app.js", "text/javascript"),
        "/youtube.js": ("youtube.js", "text/javascript"),
        "/qrcode.js": ("qrcode.js", "text/javascript"),
        "/device.js": ("device.js", "text/javascript"),
        "/sources.js": ("sources.js", "text/javascript"),
    }
    def static_handler(filename, media_type):
        # Handler arguments become query parameters in FastAPI. Capture these
        # fixed values outside the zero-argument handler instead.
        def static_file():
            return FileResponse(static_root / filename, media_type=media_type)
        return static_file

    for route, (filename, media_type) in static_assets.items():
        app.get(route)(static_handler(filename, media_type))

    @app.get("/api/telemetry")
    async def receiver_telemetry():
        return await telemetry.snapshot()

    @app.get("/api/settings")
    async def receiver_settings():
        try:
            data = load_settings(manager.shared)
            return {**data, "device_settings": True} if appliance else data
        except (OSError, ValueError):
            raise HTTPException(503, "Cannot read receiver settings") from None

    @app.put("/api/settings")
    async def save_receiver_settings(payload: OSDSettings):
        try:
            # Serialize read/merge/write so older timeout-only clients preserve
            # the selected language, even alongside another settings save.
            async with settings_lock:
                language = (payload.osd_language if "osd_language" in payload.model_fields_set
                            else load_settings(manager.shared)["osd_language"])
                data = {"version": 1, "osd_timeout_ms": payload.osd_timeout_ms,
                        "osd_language": language}
                await settings_writer.write(settings_path(manager.shared), data, 0o640)
        except (OSError, ValueError):
            raise HTTPException(503, "Cannot save receiver settings") from None
        return data

    if appliance:
        from satip_appliance.device_api import install
        install(app, Path(config["web_credentials"]), require_auth=auth_enabled)
        from satip_appliance.source_api import install as install_sources
        source_importer = install_sources(app, manager.backend, config, manager.directory.resolve().parent, manager, preview)

    @app.get("/api/network")
    async def receiver_network_status():
        interfaces = {}
        for interface in ("eth0", "wlan0"):
            try:
                node = read_json(Path("/run/satip-network") / (interface + ".json"))
                interfaces[interface] = {key: node[key] for key in ("state", "reason", "ip") if key in node}
            except (OSError, ValueError, TypeError):
                interfaces[interface] = {"state": "unknown"}
        return {"mode": "dhcp", "interfaces": interfaces}

    @app.get("/api/youtube")
    async def youtube_receiver():
        try:
            return {**youtube_status(config["player_state_dir"]),
                    "enabled": youtube_preferences(manager.shared)["enabled"]}
        except (OSError, ValueError):
            raise HTTPException(503, "Cannot read YouTube settings") from None

    @app.post("/api/youtube", status_code=202)
    async def youtube_action(payload: YouTubeRequest):
        async with manager.lock:
            try:
                data = youtube_preferences(manager.shared)
                if payload.action == "pair":
                    data["enabled"] = True
                    data["pair_request"] = secrets.token_hex(16)
                else:
                    data["enabled"] = False
                    if payload.action == "unlink":
                        data["unlink_request"] = secrets.token_hex(16)
                await settings_writer.write(youtube_preferences_path(manager.shared), data, 0o640)
            except (OSError, ValueError):
                raise HTTPException(503, "Cannot save YouTube settings") from None
        return {"accepted": True, "enabled": data["enabled"]}

    @app.post("/api/preview", status_code=202)
    async def start_preview(payload: PreviewRequest):
        if not payload.confirm_pause:
            raise HTTPException(400, "Confirm that preview pauses television playback")
        try:
            uuid(payload.service)
            return await preview.start(payload.service)
        except ValueError:
            raise HTTPException(400, "Invalid or unmapped channel; save favourites first") from None
        except PreviewError as error:
            raise HTTPException(409, str(error)) from None
        except OSError:
            raise HTTPException(503, "Cannot start preview") from None

    @app.post("/api/preview/heartbeat")
    async def preview_heartbeat(payload: PreviewSession):
        try:
            preview.touch(payload.session)
            return preview.status
        except PreviewError:
            raise HTTPException(410, "Preview stopped or expired") from None

    @app.post("/api/preview/stop")
    async def stop_preview(payload: PreviewSession):
        try:
            await preview.stop(payload.session)
            return preview.status
        except PreviewError:
            raise HTTPException(409, "Not the active preview session") from None

    @app.get("/api/preview/{session}/{name}")
    async def preview_media(session: str, name: str):
        try:
            path = preview.media(session, name)
            return FileResponse(path, media_type="application/vnd.apple.mpegurl" if name == "index.m3u8" else "video/mp2t")
        except PreviewError:
            raise HTTPException(410, "Preview stopped or expired") from None
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "Preview media not available") from None

    @app.get("/api/setup/status")
    async def setup_transition():
        return {"state": "configured"}

    @app.get("/api/status")
    async def status():
        try:
            player = read_json(runtime_path(Path(config["player_state_dir"]), "status.json"))
            pid = player.get("owner_pid")
            if type(pid) is int and not Path(f"/proc/{pid}").exists():
                player = {"error": "Player not running; persisted status is stale"}
        except (OSError, ValueError, AttributeError):
            player = {"error": "Player not running or status unavailable"}
        channels, _ = library(read_json(manager.library_path))
        channel = channels.get(player.get("playing"))
        player["channel_name"] = channel.name if channel else None
        now = time.time()
        def programmes(selected):
            return [{key: event[key] for key in ("title", "start", "stop")}
                    for event in channel_events(selected, manager.shared, now) if event["stop"] > now][:2] if selected else []
        player["programmes"] = programmes(channel)
        stream = {**preview.status, "programmes": programmes(channels.get(preview.status.get("channel_uuid")))}
        return {"scan": source_importer.public_status() if appliance and source_importer.task
                    and not source_importer.task.done() else manager.status, "player": player, "preview": stream,
                "catalogue_revision": manager.catalogue_revision,
                "library_revision": library_revision(read_json(manager.library_path)),
                "last_favourites_save_id": manager.last_favourites_save_id}

    @app.get("/api/channels")
    async def channels():
        data = read_json(manager.library_path)
        parsed, favourites = library(data)
        try:
            items = read_json(manager.directory / "candidates.json")["channels"]
        except (OSError, ValueError, KeyError):
            items = [{"service_uuid": c.service_uuid, "name": c.name} for c in parsed.values()]
        # The saved library is authoritative for mapped channel names. A stale
        # or partial scan catalogue must not rename or hide saved favourites.
        saved = {c.service_uuid: c.name for c in parsed.values()}
        items = [{**item, "name": saved.get(item["service_uuid"], item["name"])} for item in items]
        known = {item["service_uuid"] for item in items}
        items.extend({"service_uuid": service, "name": name}
                     for service, name in saved.items() if service not in known)
        return {"library_revision": library_revision(data), "channels": items, "favourites": [parsed[c].service_uuid for c in favourites],
                "previewable": [c.service_uuid for c in parsed.values()]}

    @app.post("/api/refresh")
    async def refresh():
        if manager.lock.locked():
            raise HTTPException(409, "Scan/edit already running")
        try:
            async with manager.lock:
                found = await manager.candidates()
                manager.publish_candidates(found, replace=True)
            return {"found": len(found)}
        except BackendError:
            raise HTTPException(503, "Cannot connect to Tvheadend") from None

    @app.put("/api/favourites")
    async def favourites(payload: Favourites):
        if manager.lock.locked():
            raise HTTPException(409, "Scan/edit already running")
        try:
            for service in payload.services:
                uuid(service)
            if payload.base_revision is None:
                raise HTTPException(428, "Reload channels and submit their library_revision as base_revision")
            saved = await manager.save_favourites(payload.services, expected_revision=payload.base_revision)
            # Publish only after the library has been committed. Status polling
            # can confirm this exact save if its HTTP response is lost.
            manager.last_favourites_save_id = payload.save_id
            manager.catalogue_revision += 1
            return {"saved": len(payload.services), "save_id": payload.save_id,
                    "library_revision": library_revision(saved)}
        except ConcurrentEdit as error:
            raise HTTPException(409, str(error)) from None
        except ValueError:
            raise HTTPException(400, "Invalid or unsupported free-to-air favourites") from None
        except (BackendError, OSError):
            raise HTTPException(503, "Favourites not saved; check backend. Prior favourites retained.") from None

    @app.post("/api/scan", status_code=202)
    async def scan(payload: ScanRequest):
        if not payload.confirm_pause:
            raise HTTPException(400, "Confirm that scanning pauses television playback")
        if manager.lock.locked() or (manager.task and not manager.task.done()):
            raise HTTPException(409, "Scan/edit already running")
        manager.status = {"state": "starting", "completed": 0, "total": 0, "found": 0}
        async def guarded_scan():
            try:
                await manager.scan(payload.enabled_only)
            except asyncio.CancelledError:
                manager.status.update(state="cancelled", recovery_pending=manager.journal.exists())
                manager.remember_status()
                raise
            except (BackendError, OSError, ValueError, KeyError):
                manager.status.update(state="error", recovery_pending=manager.journal.exists(),
                                      error="Cannot start scan; check backend configuration and recovery")
                manager.remember_status()
        manager.task = asyncio.create_task(guarded_scan())
        return {"started": True}

    @app.post("/api/scan/cancel")
    async def cancel():
        active = manager.task and not manager.task.done()
        if active:
            manager.status["state"] = "stopping"
        await manager.close()
        if active:
            manager.status.update(state="cancelled", recovery_pending=manager.journal.exists())
            manager.remember_status()
        return manager.status

    @app.post("/api/scan/recover")
    async def recover():
        if manager.lock.locked():
            raise HTTPException(409, "Scan/edit already running")
        try:
            async with manager.lock:
                await manager.recover()
            manager.status = {"state": "idle", "recovery_pending": False}
            manager.remember_status()
            return manager.status
        except (BackendError, OSError, ValueError):
            raise HTTPException(503, "Backend recovery failed; playback remains paused") from None

    return app


def main():
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/satip-web/config.json")
    parser.add_argument("--host", help="Override configured listening address")
    parser.add_argument("--port", type=int, help="Override configured listening port")
    args = parser.parse_args()
    try:
        config = read_json(Path(args.config))
        if args.host is not None:
            config["listen_host"] = args.host
        if args.port is not None:
            config["listen_port"] = args.port
        app = create_app(config)
    except (OSError, ValueError, KeyError):
        raise SystemExit("Management configuration or protected credentials invalid") from None
    uvicorn.run(app, proxy_headers=False, host=config.get("listen_host", "127.0.0.1"),
                port=config.get("listen_port", 8080), access_log=False)


if __name__ == "__main__":
    main()
