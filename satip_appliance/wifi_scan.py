"""Private frequency hints for wpa_supplicant's first-scan-only optimization."""
import hashlib
import fcntl
import os
from pathlib import Path
import re
import math
import subprocess
import sys
import time

from satip_controller.state import atomic_json, boot_id, read_json


def frequencies(value):
    if (not isinstance(value, list) or not 1 <= len(value) <= 3 or
            any(type(f) is not int or not (f == 2484 or
                2412 <= f <= 2472 and (f - 2412) % 5 == 0 or
                5000 <= f <= 5900 and f % 5 == 0) for f in value) or
            len(set(value)) != len(value)):
        raise ValueError("Invalid Wi-Fi frequencies")
    return value


def enabled_frequencies(text):
    result = set()
    for line in text.splitlines():
        match = re.search(r"\*\s+(\d+)(?:\.0)? MHz\s+\[\d+\](.*)", line)
        if match and "disabled" not in match[2].lower():
            result.add(int(match[1]))
    return result


class ScanHints:
    def __init__(self, root=Path("/")):
        self.root = Path(root)
        self.source = self.root / "etc/wpa_supplicant.conf"
        self.cache = self.root / "data/store/network/wifi-scan.json"
        self.directory = self.root / "run/satip-network"
        self.runtime = self.directory / "wpa-startup.conf"
        self.identity = None
        try:
            self.identity = hashlib.sha256(self.config()).hexdigest()
        except (OSError, ValueError):
            pass

    def config(self):
        fd = os.open(self.source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            data = source.read(16385)
        if not data or len(data) > 16384 or b"\0" in data:
            raise ValueError("Invalid Wi-Fi configuration")
        return data

    def saved(self, identity):
        try:
            if self.cache.is_symlink() or self.cache.stat().st_size > 1024:
                return []
            node = read_json(self.cache)
            if (not isinstance(node, dict) or set(node) != {"version", "config_sha256", "frequencies"} or
                    type(node["version"]) is not int or node["version"] != 1 or
                    node["config_sha256"] != identity):
                return []
            return frequencies(node["frequencies"])
        except (OSError, ValueError, TypeError):
            return []

    def available(self):
        phy = (self.root / "sys/class/net/wlan0/phy80211").resolve(strict=True).name
        if not re.fullmatch(r"phy[0-9]+", phy):
            return set()
        result = subprocess.run(["/usr/sbin/iw", "phy", phy, "info"],
                                capture_output=True, text=True, check=True, timeout=1)
        return enabled_frequencies(result.stdout)

    def observer_active(self):
        with (self.directory / "wifi-startup.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
        return False

    def prepare(self):
        # Only the returned path is used by the launcher. Failure always selects
        # the canonical configuration, never a leftover runtime copy.
        selected = self.source
        hint = []
        try:
            self.directory.mkdir(mode=0o755, exist_ok=True)
            self.runtime.unlink(missing_ok=True)
            data = self.config()
            identity = hashlib.sha256(data).hexdigest()
            hint = self.saved(identity)
            if re.search(rb"(?m)^\s*(initial_freq_list|freq_list|scan_freq)\s*=", data):
                hint = []  # Preserve explicit administrator scan policies.
            if hint and not self.observer_active():
                hint = []
            if hint:
                allowed = self.available()
                hint = [frequency for frequency in hint if frequency in allowed]
            if hint:
                content = ("initial_freq_list=" + " ".join(map(str, hint)) + "\n").encode() + data
                # /run is disposable; publish a private complete file atomically.
                temporary = self.runtime.with_suffix(".next")
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(fd, "wb") as output:
                        output.write(content)
                    temporary.replace(self.runtime)
                finally:
                    temporary.unlink(missing_ok=True)
                selected = self.runtime
        except (OSError, ValueError, subprocess.SubprocessError):
            selected = self.source
            hint = []
        try:
            atomic_json(self.directory / "wifi-scan.json",
                        {"boot_id": boot_id(), "prepared": round(time.monotonic(), 3),
                         "frequencies": hint, "mode": "initial" if hint else "full"},
                        0o644, durable=False)
        except OSError:
            selected = self.source
        return selected

    def remember(self, frequency):
        try:
            frequencies([frequency])
            identity = hashlib.sha256(self.config()).hexdigest()
            if not self.identity or identity != self.identity:
                return  # A concurrent settings change invalidates this observation.
            recent = [frequency] + [f for f in self.saved(identity) if f != frequency]
            node = {"version": 1, "config_sha256": identity, "frequencies": recent[:3]}
            if self.cache.is_symlink():
                return
            try:
                if read_json(self.cache) == node:
                    return
            except (OSError, ValueError):
                pass
            atomic_json(self.cache, node)
        except (OSError, ValueError, TypeError):
            pass


class ScanRecovery:
    """Nudge an idle failed initial scan without interrupting association."""
    def __init__(self, path=Path("/run/satip-network/wifi-scan.json")):
        self.path = path
        self.next_try = 0
        self.attempts = 0
        self.done = False

    def retry(self, sample, now, send):
        if self.done or self.attempts >= 3 or now < self.next_try or sample.get("state") != "DISCONNECTED":
            return False
        try:
            node = read_json(self.path)
            began = node.get("prepared")
            if (node.get("boot_id") != boot_id() or node.get("mode") != "initial" or
                    type(began) not in (int, float) or not math.isfinite(began) or
                    not began + 3 <= now <= began + 90):
                return False
            self.attempts += 1
            self.next_try = now + 1
            # A manual SCAN does not use initial_freq_list. Never reconfigure,
            # disconnect or restart an associating/authenticating connection.
            if send(b"SCAN").strip() == b"OK":
                self.done = True
                return True
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return False


def main():
    if os.geteuid() != 0 or sys.argv[1:] not in (["prepare"], ["remember"]):
        raise SystemExit("Root and prepare/remember required")
    hints = ScanHints()
    if sys.argv[1] == "prepare":
        print(hints.prepare())
    else:
        from .wifi_startup import query_status
        sample = query_status(hints.directory, "/run/wpa_supplicant/wlan0", "seed")
        dhcp = read_json(hints.directory / "wlan0.json")
        if (sample["state"] == "COMPLETED" and dhcp.get("boot_id") == boot_id() and
                dhcp.get("state") == "confirmed"):
            hints.remember(sample.get("frequency_mhz"))


if __name__ == "__main__":
    main()
