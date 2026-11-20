"""Shared HTTP boundaries for setup and normal management."""
import asyncio
import fcntl
import re
import socket
import struct
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, StringConstraints
from typing import Annotated
from starlette.responses import JSONResponse

UUIDString = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
Revision = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def local_hosts():
    """Current local addresses, including addresses assigned after service start."""
    hosts = {"localhost", "127.0.0.1", socket.gethostname(), socket.gethostname() + ".local"}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        for _, name in socket.if_nameindex():
            try:
                result = fcntl.ioctl(connection.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))
                hosts.add(socket.inet_ntoa(result[20:24]))
            except OSError:
                pass
    return sorted(hosts)


def check_action(request):
    if request.method in ("GET", "HEAD"):
        return
    if request.headers.get("x-satip-action") != "1":
        raise HTTPException(403, "Missing action header")
    origin = request.headers.get("origin")
    if origin:
        try:
            parts = urlsplit(origin)
            valid = parts.scheme == request.url.scheme and parts.netloc == request.headers.get("host")
        except ValueError:
            valid = False
        if not valid:
            raise HTTPException(403, "Cross-origin operation rejected")


class LocalHostMiddleware:
    def __init__(self, app, provider=local_hosts):
        self.app, self.provider = app, provider

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            try:
                hosts = [value for name, value in scope["headers"] if name == b"host"]
                host = hosts[0].decode("ascii") if len(hosts) == 1 else ""
                # This appliance accepts IPv4 and DNS names, not URL syntax.
                # urlsplit().hostname alone also accepts paths, userinfo and
                # queries, which can poison Starlette's reconstructed URL.
                match = re.fullmatch(r"([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?", host)
                valid = bool(match and (match[2] is None or 0 < int(match[2]) <= 65535)
                             and match[1].lower() in self.provider())
            except (ValueError, UnicodeError, OSError):
                valid = False
            if not valid:
                await JSONResponse({"detail": "Invalid appliance host"}, status_code=400)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class RequestLimits:
    """Bound bytes before JSON parsing, including chunked/misdeclared bodies."""
    def __init__(self, app, limit=32768, timeout=10, backup_upload=False):
        self.app, self.limit, self.timeout = app, limit, timeout
        self.backup_upload = backup_upload

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        # This one appliance route authenticates before streaming to a fixed
        # private file and enforces its own byte/deadline/concurrency limits.
        import re
        if self.backup_upload and re.fullmatch(r"/api/device/(?:backup|channels)/upload/[0-9a-f]{32}", scope["path"]):
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        length = headers.get(b"content-length", b"0")
        async def reject(status):
            await JSONResponse({"detail": "Request body too large or incomplete"}, status_code=status,
                               headers={"Cache-Control": "no-store"})(scope, receive, send)
        if not length.isdigit() or len(length) > 10 or int(length) > self.limit:
            return await reject(413)
        body = bytearray()
        try:
            async with asyncio.timeout(self.timeout):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > self.limit:
                        return await reject(413)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await reject(408)
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()
        await self.app(scope, replay, send)
