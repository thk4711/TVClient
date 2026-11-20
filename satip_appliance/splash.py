"""Frühe, rein beobachtende Startanzeige und bestätigte DRM-Übergabe."""
import errno
import fcntl
import ipaddress
import os
from pathlib import Path
import re
import socket
import struct
import stat
import sys
import time

from satip_controller.state import read_json
from satip_controller.settings import load_settings, DEFAULT_LANGUAGE

DIRECTORY = Path("/run/satip-splash")
FAILURES = {"data-mount": 13, "config-read": 12, "data-init": 14,
            "data-prepare": 15, "backend-init": 16, "recovery": 17,
            "devices": 25, "service-tvheadend": 9, "service-web": 18,
            "service-player": 20}
CONFIG_REASONS = "format|unknown|duplicate|characters|value|wifi|password-conflict|size|file"


def command(value, directory=DIRECTORY):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(.3)
        client.connect(str(directory / "control"))
        client.sendall(value.encode("ascii") + b"\n")
        reply = b""
        while b"\n" not in reply and len(reply) < 16:
            chunk = client.recv(16-len(reply))
            if not chunk:
                break
            reply += chunk
        if reply != b"OK\n":
            raise OSError("Startbildschirm hat nicht bestätigt")


def hold_display(directory):
    """Keep a non-master DRM client open so last-close cannot restore fbcon."""
    fd = None
    try:
        if (directory / "finished").exists():
            return None
        device = (directory / "device").read_text().strip()
        if not re.fullmatch(r"/dev/dri/card[0-9]{1,2}", device):
            return None
        fd = os.open(device, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        if not stat.S_ISCHR(os.fstat(fd).st_mode):
            raise OSError("Not a DRM device")
        # Opening while the renderer owns DRM normally yields a non-master fd.
        # Drop our own master if a renderer-exit race made us master instead.
        try:
            fcntl.ioctl(fd, 0x641f, 0)  # DRM_IOCTL_DROP_MASTER
        except OSError as error:
            if error.errno != errno.EINVAL:  # Already non-master.
                raise
        return fd
    except (OSError, ValueError):
        if fd is not None:
            os.close(fd)
        return None


def release(directory=DIRECTORY, timeout=3, keep_open=False):
    """Root-Supervisor: späten Neustart sperren, vollständige Freigabe abwarten."""
    directory.mkdir(mode=0o755, exist_ok=True)
    held = hold_display(directory) if keep_open else None
    try:
        if held is not None:
            (directory / "retain").touch(mode=0o644)
        (directory / "finished").touch(mode=0o644)
        # Lock confirmation still covers releasing master and all renderer fds.
        with (directory / "lock").open("a") as lock:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return held
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("HDMI-Startbildschirm gibt die Anzeige nicht frei")
                try:
                    command("HANDOFF" if held is not None else "STOP", directory)
                except OSError:
                    pass
                time.sleep(.02)
    except BaseException:
        if held is not None:
            os.close(held)
        raise


def address():
    """Lokale IPv4-Adresse ohne DNS, Pakete oder Wartezeit auf das Netzwerk."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        for interface in sorted(Path("/sys/class/net").iterdir()):
            if interface.name == "lo":
                continue
            try:
                request = struct.pack("256s", interface.name.encode()[:15])
                flags = struct.unpack_from("H", fcntl.ioctl(client, 0x8913, request), 16)[0]
                if not flags & 1:
                    continue
                value = socket.inet_ntoa(fcntl.ioctl(client, 0x8915, request)[20:24])
                ip = ipaddress.ip_address(value)
                if not ip.is_loopback and not ip.is_link_local and not ip.is_unspecified:
                    return value
            except OSError:
                continue
    return None


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=.1):
            return True
    except OSError:
        return False


def failure(code):
    if re.fullmatch(r"config-(?:" + CONFIG_REASONS + r"):[0-9]{1,5}", code):
        return 12, code.removeprefix("config-")
    return FAILURES.get(code, 6), "supervisor" if code in (
        "service-tvheadend", "service-web", "service-player") else ""


def token(path):
    # Never copy file contents to the display/log. Callers use fixed allowlists.
    if not path.exists():
        return ""
    with path.open() as source:
        return source.read(96).strip()


def snapshot(root, elapsed, ip, backend, web):
    try:
        return timed_state(current_state(root, ip, backend, web), elapsed)
    except (OSError, ValueError, TypeError, AttributeError):
        return 23, ""


def current_state(root, ip, backend, web):
    """Nur vorhandene Zustände lesen; kein Tuning und keine Einrichtung auslösen."""
    run = root / "run"
    code = token(run / "satip-boot-error")
    if code:
        return failure(code)
    if (run / "satip-data-failed").exists():
        return 6, ""
    if not (run / "satip-data-ready").exists():
        return 1, ""
    installation = read_json(root / "data/store/installation.json").get("state")
    if installation == "imported-unqualified":
        return 8, ""
    if installation not in ("unconfigured", "configured", "qualified"):
        return 15, ""
    if installation == "unconfigured":
        if not (root / "data/store/factory-ready.json").exists():
            return 7, ""
        if not ip:
            return network_state(root), ""
        if web:
            return 5, f"http://{ip}:8080/"
        # A running backend does not mean the setup web service is running.
        return (26 if backend else 2), ""
    if not ip:
        return network_state(root), ""
    if not backend:
        return 2, ""
    reception = token(run / "satip-reception-stage")
    if reception == "ready":
        return 35, ""
    # A listening backend can precede the supervisor's first reception probe.
    # Do not claim player readiness while the stage is still absent/network.
    return (29 if reception == "tuner" else 27), ""


def network_state(root):
    # Link carrier distinguishes association/cable from DHCP; no credentials
    # or SSID need to be read, and no packets are sent.
    for name in ("eth0", "wlan0"):
        try:
            if (root / f"sys/class/net/{name}/carrier").read_text().strip() == "1":
                return 33
        except OSError:
            pass
    return 32 if (root / "run/satip-wpa.pid").exists() else 4


def timed_state(state, elapsed):
    # Timeout means no readiness, not a claim that a process has crashed.
    limits = {1: (60, 21), 2: (30, 9), 3: (60, 20), 4: (60, 19),
              7: (60, 22), 26: (30, 18), 27: (60, 28), 29: (60, 30),
              32: (60, 34), 33: (60, 19), 35: (60, 20)}
    limit, failed = limits.get(state[0], (float("inf"), state[0]))
    return (failed, "") if elapsed > limit else state


class StartupStatus:
    def __init__(self):
        self.phase = None
        self.began = 0

    def update(self, root, now, ip, backend, web):
        state = snapshot(root, 0, ip, backend, web)
        try:
            stage = token(root / "run/satip-boot-stage")
        except (OSError, ValueError):
            stage = ""
        # A slow network must not consume the next service's startup deadline.
        phase = (state, stage if stage in FAILURES else "")
        if phase != self.phase:
            self.phase, self.began = phase, now
        return timed_state(state, now - self.began)


def display_settings(root):
    # Nach Datenvorbereitung ist die tatsächliche Player-Konfiguration maßgeblich.
    path = root / "data/store/config/player/config.json"
    if (root / "run/satip-data-ready").exists() and path.is_file():
        values = read_json(path)
    else:
        # FAT enthält gegebenenfalls private Felder: nur diese zwei Werte auswerten.
        path = root / "boot/satip-config.txt"
        values = {}
        if path.is_file() and path.stat().st_size <= 16384:
            for line in path.read_text().splitlines():
                key, separator, value = line.partition("=")
                if separator and key.strip() in ("drm_connector", "drm_mode"):
                    values[key.strip()] = value.strip()
    connector = values.get("drm_connector", "HDMI-A-2")
    mode = values.get("drm_mode", "1920x1080@50")
    if connector not in ("HDMI-A-1", "HDMI-A-2") or mode not in ("1920x1080@50", "1920x1080@60", "1280x720@50"):
        return "DISPLAY HDMI-A-2 1920x1080@50"
    return f"DISPLAY {connector} {mode}"


def display_language(root):
    # Read canonical persistent data once mounted; early frames use English.
    try:
        return load_settings(root / "data/store/management")["osd_language"]
    except (OSError, ValueError, TypeError):
        return DEFAULT_LANGUAGE


def monitor(root=Path("/"), directory=DIRECTORY):
    began = time.monotonic()
    connected = False
    status = StartupStatus()
    while not (directory / "finished").exists():
        try:
            command("LANGUAGE " + display_language(root), directory)
            # A malformed config must not suppress the error-state command.
            try:
                settings = display_settings(root)
            except (OSError, ValueError, TypeError, AttributeError):
                settings = "DISPLAY HDMI-A-2 1920x1080@50"
            command(settings, directory)
            try:
                state, detail = status.update(root, time.monotonic(), address(),
                                              listening(9981), listening(8080))
            except (OSError, ValueError, TypeError, AttributeError):
                state, detail = 23, ""
            page = None
            if state in (4, 5, 19, 32, 33, 34):
                from .onboarding import SCREEN
                from satip_controller.state import boot_id
                try:
                    node = read_json(SCREEN)
                    if (node.get("boot_id") == boot_id() and time.monotonic() - node["time"] < 5
                            and read_json(root / "data/store/installation.json").get("state") == "unconfigured"):
                        page = node["page"]
                except (OSError, ValueError, KeyError):
                    pass
            command("PAGE " + page.encode().hex() if page else
                    f"STATE {state}" + (" " + detail if detail else ""), directory)
            connected = True
        except (OSError, ValueError, TypeError, AttributeError):
            # Keine Werte aus geschützten Dateien in Protokolle übernehmen.
            if not (directory / "control").exists() and (connected or time.monotonic()-began > 10):
                return
        time.sleep(.2)


if __name__ == "__main__":
    if sys.argv[1:] == ["stop"]:
        release()
    else:
        monitor()
