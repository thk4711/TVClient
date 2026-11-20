"""Read-only startup gates; never tune, scan, or expose admin credentials."""
import ipaddress
from pathlib import Path
import socket
import time
from urllib.parse import urlsplit

from satip_controller.backend import Backend
from satip_controller.state import read_json
from .splash import address


def route_available(url):
    # Registration accepts numeric LAN addresses. UDP connect checks routing
    # without sending a packet or waiting for the SAT-IP device.
    host = urlsplit(url).hostname
    ip = ipaddress.ip_address(host)
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as client:
            client.connect((str(ip), 554))
        return True
    except OSError:
        return False


class StartupGate:
    def __init__(self, root=Path("/")):
        self.root = root
        self.previous = None

    def publish(self, stage):
        if stage != self.previous:
            path = self.root / "run/satip-reception-stage"
            temporary = path.with_suffix(".next")
            temporary.write_text(stage + "\n")
            temporary.chmod(0o644)
            temporary.replace(path)
            print(f"Startup {stage} ({time.monotonic():.3f} s since Linux start)", flush=True)
            self.previous = stage

    def enigma2(self):
        store = self.root / "data/store"
        profile = store / "web/tv-source-enigma2.json"
        config = store / "config/web/config.json"
        if not profile.exists() or not config.exists():
            return None
        from .tv_source import validate_profile
        node = validate_profile(read_json(profile))
        return node if node["network_uuid"] == read_json(config)["network_uuid"] else None

    def backend(self, url):
        source = self.enigma2()
        if source:
            info = source["enigma2"]
            url = f"http://{info['address']}:{info['web_port']}"
        if url and not route_available(url):
            self.publish("network")
            return False
        return True

    def player(self, url):
        source = self.enigma2()
        if source:
            info = source["enigma2"]
            url = f"http://{info['address']}:{info['web_port']}"
        if not address() or (url and not route_available(url)):
            self.publish("network")
            return False
        if url:
            target = urlsplit(url)
            try:
                with socket.create_connection((target.hostname, target.port or 80), timeout=.5):
                    pass
            except OSError:
                self.publish("server")
                return False
        self.publish("tuner")
        credentials = read_json(self.root / "data/store/config/web/tvheadend-admin.json")
        backend = Backend(credentials, timeout=.5)
        if source:
            networks = backend._request("mpegts/network/grid", {"limit": 10000}, False).get("entries", [])
            if any(n["uuid"] == source["network_uuid"] and n.get("enabled") is True for n in networks):
                self.publish("ready")
                return True
            return False
        servers = backend._request("hardware/tree", {"uuid": "root"}, False)
        for server in servers:
            if server.get("class") != "satip_client":
                continue
            fields = {p["id"]: p.get("value") for p in server.get("params", [])}
            if url and fields.get("addr") != urlsplit(url).hostname:
                continue
            if not fields.get("active"):
                continue
            children = backend._request("hardware/tree", {"uuid": server["uuid"]}, False)
            for frontend in children:
                fields = {p["id"]: p.get("value") for p in frontend.get("params", [])}
                if frontend.get("class") == "satip_frontend_dvbs" and fields.get("enabled") is True:
                    self.publish("ready")
                    return True
        return False
