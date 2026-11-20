"""Read-only backend subscription failure feedback, bound to one player generation."""
import math
from pathlib import Path
import re
import time

from satip_controller.backend import Backend, BackendError
from satip_controller.config import credentials
from satip_controller.state import atomic_json
from .health import small_json


class TuneMonitor:
    def __init__(self, pid, generation, began, root=Path("/")):
        self.pid, self.generation, self.began = pid, generation, began
        self.request = root / "run/satip-player/tune-request.json"
        self.response = root / "run/satip-tuning/status.json"
        self.credentials = root / "etc/satip-web/tvheadend-admin.json"
        self.next_probe = 0
        self.reported = None

    def check(self, now=None):
        now = time.monotonic() if now is None else now
        if now < self.next_probe:
            return
        self.next_probe = now + .25
        try:
            request = small_json(self.request)
            if not isinstance(request, dict):
                return
            stamp, token = request.get("monotonic"), request.get("token")
            if (type(request.get("pid")) is not int or request["pid"] != self.pid or
                    request.get("generation") != self.generation or
                    not isinstance(token, str) or not re.fullmatch("[0-9a-f]{32}", token) or
                    token == self.reported or type(stamp) not in (int, float) or
                    not math.isfinite(stamp) or not self.began <= stamp <= now <= stamp + 60):
                return
            backend = Backend(credentials(self.credentials), timeout=.2)
            data = backend._request("status/subscriptions", {"limit": 1000, "lang": "eng"}, False)
            rows = data.get("entries")
            if not isinstance(rows, list) or data.get("totalCount") != len(rows):
                return
            matching = [row for row in rows if isinstance(row, dict) and
                        row.get("client") == "satip-player/" + token and
                        row.get("hostname") == "127.0.0.1" and
                        row.get("title") == "HTTP" and row.get("profile") == "pass"]
            if len(matching) != 1 or matching[0].get("state") != "Bad":
                return
            subscription = matching[0].get("id")
            if type(subscription) is not int or subscription < 0:
                return
            # The load may have ended while the backend query was in flight.
            if small_json(self.request) != request:
                return
            atomic_json(self.response, {"generation": self.generation, "token": token,
                        "reason": "load_failed", "subscription": subscription,
                        "monotonic": time.monotonic()}, 0o644, durable=False)
            self.reported = token
            print(f"Tune failed: subscription {subscription} ({time.monotonic():.3f} s since Linux start)", flush=True)
        except (OSError, ValueError, TypeError, AttributeError, BackendError):
            # No status, an unavailable API or malformed data is not a tune failure.
            pass
