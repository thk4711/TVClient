"""Authenticated device controls; not installed into standalone Debian management."""
import asyncio
import os
from pathlib import Path
import secrets

from fastapi import Request
from fastapi.responses import FileResponse
from .backup import MAX_ARCHIVE, TOKEN
from satip_controller.http import check_action

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from pydantic import Field, SecretStr

from satip_controller.config import credentials
from satip_controller.http import StrictRequest
from .device_client import request


class DeviceAction(StrictRequest):
    action: str = Field(pattern=r"^(device|wifi|wifi_enabled|scan_wifi|wifi_confirm|wifi_revert|web_password|root_password|ssh|restart_player|reboot|shutdown|storage_status|storage_format|storage_mount|storage_limit|storage_eject|storage_forget|backup_create|backup_validate|backup_restore|backup_status|backup_channels_create|backup_channels_validate|backup_channels_restore|source_select|satip_tuner)$")
    args: dict = Field(default_factory=dict)
    current_password: SecretStr | None = None


def install(app, login_path, helper=request, backup_root=Path("/run"), require_auth=lambda: True,
            *, setup_only=False):
    security = HTTPBasic(auto_error=False)
    backup_root = Path(backup_root)
    uploads = asyncio.Lock()

    @app.middleware("http")
    async def maintenance(request, call_next):
        if (request.method not in ("GET", "HEAD")
                and (backup_root / "satip-backup/maintenance.json").exists()
                and request.url.path != "/api/device"):
            return JSONResponse({"detail": "Configuration maintenance is running"}, status_code=409)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        if request.url.path == "/api/device":
            return JSONResponse({"detail": "Invalid device settings request"}, status_code=422)
        return await request_validation_exception_handler(request, error)

    async def authenticate(supplied: HTTPBasicCredentials = Depends(security)):
        login = credentials(login_path)
        if not require_auth():
            return login
        if not (supplied and secrets.compare_digest(supplied.username.encode(), login["username"].encode())
                and secrets.compare_digest(supplied.password.encode(), login["password"].encode())):
            raise HTTPException(401, "Device settings require login", headers={"WWW-Authenticate": "Basic"})
        return login

    async def call(action, args=None):
        if setup_only and action not in ("backup_channels_validate", "backup_channels_restore", "backup_status"):
            raise HTTPException(409, "Only SAT-IP channel restore is available during initial setup")
        try:
            return await helper(action, args)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        except (OSError, TimeoutError):
            raise HTTPException(503, "Device settings are unavailable. Retry shortly.") from None

    @app.get("/api/device", dependencies=[Depends(authenticate)])
    async def status():
        return {**await call("status"), "require_auth": require_auth()}

    @app.get("/api/device/storage", dependencies=[Depends(authenticate)])
    async def storage_status():
        return await call("storage_status")

    @app.post("/api/device/channels/upload/{identifier}")
    @app.post("/api/device/backup/upload/{identifier}")
    async def upload(identifier: str, incoming: Request, login=Depends(authenticate)):
        check_action(incoming)
        if not TOKEN.fullmatch(identifier):
            raise HTTPException(422, "Invalid backup identifier")
        if uploads.locked():
            raise HTTPException(409, "Another backup upload is running")
        if incoming.headers.get("content-type") != "application/octet-stream":
            raise HTTPException(415, "Upload a configuration backup file")
        length = incoming.headers.get("content-length")
        if length is not None and (not length.isdigit() or len(length) > 10 or int(length) > MAX_ARCHIVE):
            raise HTTPException(413, "Backup exceeds 32 MiB")
        target = backup_root / "satip-web" / ("backup-upload-" + identifier)
        async with uploads:
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except OSError:
                raise HTTPException(409, "Backup upload unavailable; retry with a new selection") from None
            try:
                size = 0
                with os.fdopen(fd, "wb") as out:
                    try:
                        async with asyncio.timeout(60):
                            async for chunk in incoming.stream():
                                size += len(chunk)
                                if size > MAX_ARCHIVE:
                                    raise HTTPException(413, "Backup exceeds 32 MiB")
                                out.write(chunk)
                            out.flush()
                            os.fsync(out.fileno())
                    except TimeoutError:
                        raise HTTPException(408, "Backup upload timed out") from None
                return await call("backup_channels_validate" if incoming.url.path.startswith("/api/device/channels/") else "backup_validate", {"id": identifier})
            finally:
                target.unlink(missing_ok=True)

    @app.get("/api/device/channels/download/{identifier}", dependencies=[Depends(authenticate)])
    @app.get("/api/device/backup/download/{identifier}", dependencies=[Depends(authenticate)])
    async def download(identifier: str, incoming: Request):
        if not TOKEN.fullmatch(identifier):
            raise HTTPException(422, "Invalid backup identifier")
        status = await call("backup_status", {"id": identifier})
        channels = incoming.url.path.startswith("/api/device/channels/")
        if status.get("state") != "ready" or status.get("kind", "configuration") != ("channels" if channels else "configuration"):
            raise HTTPException(409, "Backup is not ready or has expired")
        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return FileResponse(backup_root / "satip-backup" / ("download-" + identifier + ".tar"),
            media_type="application/octet-stream", filename=("satip-channels-" if channels else "satip-backup-") + stamp + ".tar",
            headers={"Cache-Control": "no-store"})

    @app.post("/api/device")
    async def change(payload: DeviceAction, login=Depends(authenticate)):
        # Password changes rely on the authenticated web login, without re-entry.
        if require_auth() and payload.action in ("ssh", "restart_player", "reboot", "shutdown", "storage_eject", "storage_forget", "backup_restore", "backup_channels_restore", "source_select", "satip_tuner"):
            if not (payload.current_password and secrets.compare_digest(
                    payload.current_password.get_secret_value().encode(), login["password"].encode())):
                raise HTTPException(403, "Incorrect web password")
        return await call(payload.action, payload.args)
