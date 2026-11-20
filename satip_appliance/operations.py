"""Root-owned, bounded recovery facts. No URLs, credentials or raw errors."""
import argparse
import fcntl
import math
import os
from pathlib import Path
import subprocess
import time

from satip_controller.state import atomic_json, boot_id
from .persistent import validate_mount

REASONS = ("boot", "shutdown", "process_exit", "heartbeat_timeout", "local_api_timeout",
           "watchdog_armed", "watchdog_stopped", "supervisor_exit",
           "supervisor_timeout", "guardian_timeout")
SERVICES = ("appliance", "player", "web", "tvheadend", "watchdog")
DIRECTORY = Path("/data/store/operations")


def record(service, reason, directory=DIRECTORY):
    if service not in SERVICES or reason not in REASONS:
        raise ValueError("Unknown recovery event")
    if directory == DIRECTORY:
        validate_mount(Path("/data"), probe=False)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "recovery.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / "recovery.json"
        try:
            # The summary is bounded to 16 KiB, unlike the 4 KiB health records.
            if path.stat().st_size > 16384 or path.is_symlink():
                raise ValueError("Invalid recovery summary")
            from satip_controller.state import read_json
            old = read_json(path)
        except (OSError, ValueError):
            old = {}
        if not isinstance(old, dict):
            old = {}
        current_boot = boot_id()
        if reason == "boot" and old.get("boot_id") == current_boot:
            if old.get("clean_shutdown") is True:
                old["clean_shutdown"] = False
                atomic_json(path, old)
            return
        counters = {}
        for key, value in old.get("counters", {}).items() if isinstance(old.get("counters"), dict) else ():
            if key in {s + ":" + r for s in SERVICES for r in REASONS} and type(value) is int:
                counters[key] = min(1000000000, max(0, value))
        key = service + ":" + reason
        counters[key] = min(1000000000, counters.get(key, 0) + 1)
        events = []
        for event in old.get("events", []) if isinstance(old.get("events"), list) else []:
            if isinstance(event, dict) and event.get("service") in SERVICES and event.get("reason") in REASONS:
                clean = {"service": event["service"], "reason": event["reason"]}
                stamp = event.get("monotonic")
                if type(stamp) in (int, float) and math.isfinite(stamp) and 0 <= stamp <= 1e12:
                    clean["monotonic"] = stamp
                identifier = event.get("boot_id")
                if isinstance(identifier, str) and len(identifier) <= 64 and all(c in "0123456789abcdef-" for c in identifier):
                    clean["boot_id"] = identifier
                events.append(clean)
        events.append({"service": service, "reason": reason, "boot_id": current_boot,
                       "monotonic": round(time.monotonic(), 3)})
        previous = old.get("previous_shutdown", "unknown")
        if previous not in ("unknown", "clean", "unclean"):
            previous = "unknown"
        if reason == "boot":
            previous = "clean" if old.get("clean_shutdown") is True else "unclean" if old.get("boot_id") else "unknown"
        clean_shutdown = reason == "shutdown" or (reason != "boot" and
            old.get("boot_id") == current_boot and old.get("clean_shutdown") is True)
        summary = {"version": 1, "boot_id": current_boot, "previous_shutdown": previous,
                   "clean_shutdown": clean_shutdown, "counters": counters, "events": events[-32:]}
        atomic_json(path, summary)


def best_effort(service, reason):
    # SD I/O must not block a supervisor. A separate root-owned writer has a
    # short deadline, no inherited media descriptors, and a nonblocking lock.
    child = None
    try:
        child = subprocess.Popen(["/usr/bin/python3", "-m", "satip_appliance.operations", service, reason],
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "/opt/satip"}, start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        if child is not None:
            try:
                child.kill()
            except ProcessLookupError:
                pass
            # Do not wait indefinitely for a writer stuck in kernel I/O.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=SERVICES)
    parser.add_argument("reason", choices=REASONS)
    args = parser.parse_args()
    record(args.service, args.reason)


if __name__ == "__main__":
    main()
