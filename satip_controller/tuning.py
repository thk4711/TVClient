"""Per-load identity and narrowly scoped supervisor feedback for failed TV tunes."""
import math
import os
from pathlib import Path
import secrets
import time

from .state import atomic_json, read_json


class TuneFeedback:
    def __init__(self, runtime):
        self.request = Path(runtime) / "tune-request.json"
        self.response = os.environ.get("SATIP_TUNE_STATUS")
        self.generation = os.environ.get("SATIP_GENERATION")
        self.token = None
        self.began = 0

    def begin(self):
        self.token = None
        if not self.response or not self.generation:
            return {}
        token = secrets.token_hex(16)
        self.began = time.monotonic()
        try:
            atomic_json(self.request, {"pid": os.getpid(), "generation": self.generation,
                        "token": token, "monotonic": self.began}, durable=False)
        except OSError:
            # Optional feedback must never prevent an ordinary bounded load.
            return {}
        self.token = token
        return {"user-agent": "satip-player/" + token}

    def cancel(self):
        active = self.token is not None
        self.token = None
        if active:
            try:
                self.request.unlink(missing_ok=True)
            except OSError:
                pass

    def failed(self):
        if not self.token:
            return False
        try:
            path = Path(self.response)
            if path.stat().st_size > 4096:
                return False
            node = read_json(path)
            stamp = node.get("monotonic")
            return (node.get("generation") == self.generation and
                    node.get("token") == self.token and node.get("reason") == "load_failed" and
                    type(stamp) in (int, float) and math.isfinite(stamp) and
                    self.began <= stamp <= time.monotonic() <= stamp + 2)
        except (OSError, ValueError, TypeError, AttributeError):
            return False
