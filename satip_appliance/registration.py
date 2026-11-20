"""Fixed-path SAT-IP registration mailbox, consumed only by the backend supervisor."""
import json
import os
from pathlib import Path
import re
import stat

from .discovery import description_url, server_identity
from satip_controller.backend import Backend, BackendError
from satip_controller.state import atomic_json, read_json


def read_request(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("Registration must be a regular file")
        content = source.read(2049)
    if len(content) > 2048:
        raise ValueError("Oversized server registration")
    node = json.loads(content)
    if (not isinstance(node, dict) or set(node) != {"url", "udn", "request_id"}
            or not isinstance(node["request_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", node["request_id"])):
        raise ValueError("Invalid server registration")
    return {"url": description_url(node["url"]), "udn": server_identity(node["udn"]),
            "request_id": node["request_id"]}


class Registration:
    def __init__(self, store=Path("/data/store")):
        self.store = Path(store)
        self.accepted = self.store / "identity/satip-server.json"
        self.seen = None
        self.url = None
        if self.accepted.exists():
            self.url = read_request(self.accepted)["url"]
        elif (self.store / "identity/factory.json").exists():
            value = read_json(self.store / "identity/factory.json")["settings"].get("satip_xml")
            self.url = description_url(value) if value else None

    def refresh(self):
        try:
            node = read_request(self.store / "web/server-request.json")
        except FileNotFoundError:
            return self.url
        except (OSError, ValueError, TypeError):
            return self.url  # Malformed/untrusted requests never change launch arguments.
        if node == self.seen:
            return self.url
        result = {"request_id": node["request_id"], "state": "accepted"}
        try:
            if read_json(self.store / "installation.json").get("state") != "unconfigured":
                raise ValueError("Server selection is available only during first setup")
            if node["url"] != self.url:
                if any((self.store / ("web/" + name)).exists() for name in
                       ("setup-state.json", "mux-rollback.json", "setup-complete.json")):
                    raise ValueError("Server selection is committed; resume the existing setup")
                factory = read_json(self.store / "identity/factory.json")
                if Backend(factory["admin"])._request("status/subscriptions", {"limit": 10000}, False).get("entries"):
                    raise ValueError("Another client owns the tuner; stop it before selecting a server")
            atomic_json(self.accepted, node)
            self.url = node["url"]
        except BackendError:
            return self.url  # Retry once the backend becomes reachable.
        except ValueError as error:
            result.update(state="rejected", error=str(error))
        atomic_json(self.store / "web/server-result.json", result)
        os.chown(self.store / "web/server-result.json", 422, 422)
        self.seen = node
        return self.url
