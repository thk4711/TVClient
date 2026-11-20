"""Bounded Wi-Fi startup timing and successful-frequency learning."""
import fcntl
import os
from pathlib import Path
import socket
import signal
import time

from satip_controller.state import atomic_json, boot_id, read_json

STATES = {"DISCONNECTED", "INTERFACE_DISABLED", "INACTIVE", "SCANNING",
          "AUTHENTICATING", "ASSOCIATING", "ASSOCIATED", "4WAY_HANDSHAKE",
          "GROUP_HANDSHAKE", "COMPLETED"}


def parse_status(reply):
    fields = dict(line.split("=", 1) for line in reply.decode("utf-8", errors="replace").splitlines()
                  if "=" in line)
    state = fields.get("wpa_state")
    if state not in STATES:
        return {"state": "unavailable"}
    result = {"state": state}
    frequency = fields.get("freq", "")
    if frequency.isdecimal() and 2000 <= int(frequency) <= 7100:
        result["frequency_mhz"] = int(frequency)
    return result


def query_control(directory, control, sequence, command):
    # A fresh reply address prevents a late response to a timed-out query from
    # being mistaken for a newer status sample.
    local = directory / f"wifi-startup-{os.getpid()}-{sequence}.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
        client.settimeout(.2)
        client.bind(str(local))
        try:
            client.sendto(command, control)
            return client.recv(8192)
        finally:
            local.unlink(missing_ok=True)


def query_status(directory, control, sequence):
    return parse_status(query_control(directory, control, sequence, b"STATUS"))


def observe(directory=Path("/run/satip-network"), duration=90,
            control="/run/wpa_supplicant/wlan0", hints=None, recovery=None):
    directory.mkdir(mode=0o755, exist_ok=True)
    with (directory / "wifi-startup.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        began = time.monotonic()
        node = {"boot_id": boot_id(), "started": round(began, 3), "events": []}
        path = directory / "wifi-startup.json"
        previous = None
        sequence = 0
        while time.monotonic() - began < duration:
            sequence += 1
            try:
                sample = query_status(directory, control, sequence)
            except OSError:
                sample = {"state": "unavailable"}
            now = time.monotonic()
            if recovery and recovery.retry(sample, now, lambda command:
                    query_control(directory, control, f"retry-{sequence}", command)):
                node["normal_scan_requested"] = round(time.monotonic(), 3)
                atomic_json(path, node, 0o644, durable=False)
            if sample != previous:
                node["events"] = (node["events"] + [{**sample, "uptime": round(now, 3)}])[-64:]
                atomic_json(path, node, 0o644, durable=False)
                previous = sample
            try:
                dhcp = read_json(directory / "wlan0.json")
                stamp = dhcp.get("history", [])[-1]["uptime"]
                confirmed = (dhcp.get("boot_id") == node["boot_id"] and
                             dhcp.get("state") == "confirmed" and
                             type(stamp) in (int, float) and began <= stamp <= now)
            except (OSError, ValueError, AttributeError, IndexError, KeyError, TypeError):
                confirmed = False
            if sample["state"] == "COMPLETED" and confirmed:
                node["dhcp_confirmed"] = stamp
                node["finished"] = round(now, 3)
                if hints:
                    hints.remember(sample.get("frequency_mhz"))
                break
            time.sleep(.25)
        else:
            node["timed_out"] = True
        atomic_json(path, node, 0o644, durable=False)


def main():
    if os.geteuid() != 0:
        raise SystemExit("Root required")
    def stop(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    try:
        from .wifi_scan import ScanHints, ScanRecovery
        observe(hints=ScanHints(), recovery=ScanRecovery())
    finally:
        pidfile = Path("/run/satip-wifi-startup.pid")
        try:
            if pidfile.read_text().strip() == str(os.getpid()):
                pidfile.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
