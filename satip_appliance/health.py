"""Generation-bound service checks; external tuner availability is not health."""
import http.client
import json
import math
import os
from pathlib import Path
import time

from satip_controller.backend import Backend
from satip_controller.config import credentials

STARTUP_GRACE = 120
PROGRESS_TIMEOUT = 45
PROBE_INTERVAL = 5
PROBE_FAILURES = 6


def small_json(path):
    # Runtime directories belong to the service. Never follow its symlinks or
    # read an unbounded file as root.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        data = source.read(4097)
    if len(data) > 4096:
        raise ValueError("Health response too large")
    return json.loads(data)


def http_json(port, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        data = response.read(4097)
        if response.status != 200 or len(data) > 4096:
            raise ValueError("Local health request failed")
        return json.loads(data)
    finally:
        connection.close()


class Health:
    def __init__(self, service, pid, generation, began):
        self.service, self.pid, self.generation = service, pid, generation
        self.began = self.last_progress = began
        self.next_probe = began
        self.failures = 0
        self.armed = False

    def check(self, now=None):
        now = time.monotonic() if now is None else now
        if now < self.next_probe:
            return None
        self.next_probe = now + PROBE_INTERVAL
        healthy = False
        try:
            if self.service == "player":
                node = small_json("/run/satip-player/progress.json")
                if not isinstance(node, dict):
                    raise ValueError("Invalid progress record")
                stamp = node.get("monotonic")
                healthy = (node.get("pid") == self.pid and node.get("generation") == self.generation
                           and type(stamp) in (int, float) and math.isfinite(stamp)
                           and self.began <= stamp <= now and now - stamp <= PROGRESS_TIMEOUT)
                if healthy:
                    self.last_progress = stamp
            elif self.service == "web":
                node = http_json(8080, "/healthz")
                if not isinstance(node, dict):
                    raise ValueError("Invalid web health response")
                healthy = node.get("pid") == self.pid and node.get("generation") == self.generation
            else:
                header = Backend(credentials(Path("/etc/satip-web/tvheadend-admin.json"))).header
                node = http_json(9981, "/api/serverinfo", {"Authorization": header})
                healthy = isinstance(node, dict) and bool(node.get("sw_version"))
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
            pass
        if healthy:
            self.armed = True
            self.failures = 0
            return None
        self.failures += 1
        if not self.armed and now - self.began < STARTUP_GRACE:
            return None
        if self.service == "player":
            if now - self.last_progress > PROGRESS_TIMEOUT:
                return "heartbeat_timeout"
        elif self.failures >= PROBE_FAILURES:
            return "local_api_timeout"
        return None
