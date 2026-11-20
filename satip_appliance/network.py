"""Root-only DHCP address hints; only the stock DHCP script configures networking."""
import fcntl
import ipaddress
import os
from pathlib import Path
import sys
import time
from contextlib import contextmanager

from satip_controller.state import atomic_json, read_json, boot_id

STORE = Path("/data/store/network")
RUN = Path("/run/satip-network")


def read(path):
    try:
        if path.stat().st_size > 16384:
            return {}
        return read_json(path)
    except (OSError, ValueError):
        return {}


@contextmanager
def locked(interface):
    RUN.mkdir(mode=0o755, exist_ok=True)
    with (RUN / (interface + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def status(interface, state, reason="", **fields):
    previous = read(RUN / (interface + ".json"))
    history = previous.get("history", []) if isinstance(previous, dict) and previous.get("boot_id") == boot_id() else []
    history = history[-7:] + [{"state": state, "reason": reason, "uptime": round(time.monotonic(), 3)}]
    atomic_json(RUN / (interface + ".json"),
                {"state": state, "reason": reason, "boot_id": boot_id(), "history": history, **fields},
                0o644, durable=False)


def validate_cache(data):
    # Version 1 was a full lease cache. Only its IP hint remains relevant;
    # the next binding replaces it with the minimal version 2 representation.
    if (not isinstance(data, dict) or type(data.get("version")) is not int
            or data["version"] not in (1, 2) or not isinstance(data.get("ip"), str)):
        raise ValueError("Invalid DHCP address hint")
    ip = ipaddress.IPv4Address(data["ip"])
    if ip.is_unspecified or ip.is_multicast or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        raise ValueError("Unsuitable DHCP address hint")
    if data["version"] == 2 and set(data) != {"version", "ip"}:
        raise ValueError("Invalid DHCP address hint fields")
    return {"version": 2, "ip": str(ip)}


def save_cache(interface):
    try:
        cache = validate_cache({"version": 2, "ip": os.environ.get("ip")})
        atomic_json(STORE / (interface + ".json"), cache)
    except (ValueError, OSError):
        (STORE / (interface + ".json")).unlink(missing_ok=True)


def cache_event(interface, event):
    # Called after stock DHCP configuration. Never add/remove an address or route.
    with locked(interface):
        if event in ("bound", "renew"):
            status(interface, "confirmed", ip=os.environ.get("ip", ""))
            save_cache(interface)
        elif event in ("deconfig", "nak"):
            (STORE / (interface + ".json")).unlink(missing_ok=True)
            status(interface, "dhcp", "DHCP " + event)


def start(interface):
    cache = None
    try:
        cache = validate_cache(read(STORE / (interface + ".json")))
    except ValueError:
        pass
    status(interface, "dhcp", "Safe DHCP")
    args = ["/sbin/udhcpc", "-f", "-T", "1", "-t", "10" if interface == "wlan0" else "3",
            "-A", "2" if interface == "wlan0" else "5", "-i", interface,
            "-p", f"/run/satip-dhcp-{interface}.pid"]
    if interface == "wlan0":
        args.append("-S")
    if cache:
        args.extend(["-r", cache["ip"]])
    os.execv(args[0], args)


def main():
    if os.geteuid() != 0 or len(sys.argv) < 3 or sys.argv[2] not in ("eth0", "wlan0"):
        raise SystemExit("Root and a supported interface required")
    action, interface = sys.argv[1:3]
    if action == "start" and len(sys.argv) == 3:
        start(interface)
    elif action == "cache" and len(sys.argv) == 4:
        cache_event(interface, sys.argv[3])
    else:
        raise SystemExit("Invalid network action")


if __name__ == "__main__":
    main()
