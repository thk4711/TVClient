"""RAM-only progress and a loopback-only web liveness probe."""
import json
import os
from pathlib import Path
import time

from .state import atomic_json


class Progress:
    """Called by the supervised control loop, never by a background thread."""
    def __init__(self):
        self.path = os.environ.get("SATIP_PROGRESS_FILE")
        self.token = os.environ.get("SATIP_GENERATION")
        self.next_write = 0

    def pulse(self):
        now = time.monotonic()
        if self.path and self.token and now >= self.next_write:
            atomic_json(Path(self.path), {"pid": os.getpid(), "generation": self.token,
                        "monotonic": now}, durable=False)
            self.next_write = now + 2


class LocalHealth:
    """No credentials/status exposed; ordinary routes retain their authentication.

    Applied outside routing dependencies. Only a direct loopback client can use
    this endpoint; forwarded headers and Host never grant loopback access.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/healthz":
            return await self.app(scope, receive, send)
        client = scope.get("client")
        allowed = client and client[0] in ("127.0.0.1", "::1") and scope["method"] == "GET"
        body = json.dumps({"pid": os.getpid(), "generation": os.environ.get("SATIP_GENERATION")}).encode() if allowed else b"Not found"
        await send({"type": "http.response.start", "status": 200 if allowed else 404,
                    "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})
