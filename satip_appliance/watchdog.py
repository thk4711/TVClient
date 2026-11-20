"""BCM2835 hardware watchdog backstop with verified timeout and exact ownership."""
import argparse
from array import array
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import time

from satip_controller.state import atomic_json, read_json
from .supervisor import process_identity

ROOT = Path("/run")
PID = ROOT / "satip-watchdog.pid.json"
READY = ROOT / "satip-watchdog.ready"
STATE = Path("/sys/class/watchdog/watchdog0")
SET_TIMEOUT = 0xc0045706
GET_TIMEOUT = 0x80045707


def configure(fd, requested=15):
    timeout = array("i", [requested])
    fcntl.ioctl(fd, SET_TIMEOUT, timeout, True)
    fcntl.ioctl(fd, GET_TIMEOUT, timeout, True)
    if timeout[0] < 3:
        raise ValueError("Watchdog timeout too short")
    return timeout[0], min(3, timeout[0] / 3)


def guardians_healthy(now=None, directory=Path("/run/satip-supervisor")):
    from .guardian import fresh, CLEANUP_TIMEOUT, PROGRESS_TIMEOUT
    from .health import small_json
    sampled_now = now
    now = time.monotonic() if now is None else now
    for service in ("tvheadend", "web", "player"):
        expected = directory / (service + ".guard.expected")
        if not expected.exists():
            continue  # Explicitly stopped services are not failures.
        try:
            began = small_json(expected)["began"]
            if type(began) in (int, float) and 0 <= now - began < 15:
                continue  # Bounded initial guardian handshake.
            node = small_json(directory / (service + ".guard.pid.json"))
            progress = small_json(directory / (service + ".guard.progress"))
            timeout = CLEANUP_TIMEOUT if progress.get("phase") == "cleanup" else PROGRESS_TIMEOUT
            # The stop controller has a short window to remove the expectation
            # after the guardian acknowledges cleanup and exits.
            if progress.get("phase") == "stopped" and fresh(
                    directory / (service + ".guard.progress"), node["pid"], node["start"], sampled_now, 5):
                continue
            if (alive(node) and fresh(directory / (service + ".guard.progress"),
                                     node["pid"], node["start"], sampled_now, timeout)):
                continue
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return False
    return True


def run():
    os.umask(0o077)
    with (ROOT / "satip-watchdog.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (STATE / "nowayout").read_text().strip() != "0":
            raise RuntimeError("Watchdog cannot safely disarm; nowayout must be 0")
        stopped = False

        def stop(*_):
            nonlocal stopped
            stopped = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, stop)
        fd = os.open("/dev/watchdog0", os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        armed = False
        try:
            timeout, interval = configure(fd)
            os.write(fd, b"\0")
            atomic_json(READY, {"pid": os.getpid(), "timeout": timeout,
                              "feed_interval": interval}, durable=False)
            armed = True
            print(f"Hardware watchdog active: {timeout}s timeout / {interval}s feed", flush=True)
            while not stopped:
                if not guardians_healthy():
                    from .operations import best_effort
                    best_effort("watchdog", "guardian_timeout")
                    raise RuntimeError("Guardian failure; leaving hardware watchdog armed")
                os.write(fd, b"\0")
                end = time.monotonic() + interval
                while not stopped and time.monotonic() < end:
                    time.sleep(.1)
        finally:
            # Only deliberate stop/configuration failure may disarm. An unexpected
            # feeder error, SIGKILL or system freeze must leave the timer armed.
            try:
                if stopped or not armed:
                    os.write(fd, b"V")
            finally:
                os.close(fd)
                READY.unlink(missing_ok=True)


def alive(node):
    try:
        return bool(node and node.get("start") is not None and
                    process_identity(node["pid"]) == node["start"])
    except (FileNotFoundError, ProcessLookupError):
        return False


def control(action):
    os.umask(0o077)
    with (ROOT / "satip-watchdog.control").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        node = read_json(PID) if PID.exists() else None
        if action == "stop":
            if alive(node):
                fd = os.pidfd_open(node["pid"])
                try:
                    if not alive(node):
                        raise RuntimeError("Watchdog identity changed")
                    signal.pidfd_send_signal(fd, signal.SIGTERM)
                    end = time.monotonic() + 5
                    while alive(node) and time.monotonic() < end:
                        time.sleep(.05)
                    if alive(node):
                        raise RuntimeError("Watchdog stop timed out; do not KILL its feeder")
                finally:
                    os.close(fd)
            if (STATE / "state").read_text().strip() != "inactive":
                raise RuntimeError("Watchdog is still armed")
            PID.unlink(missing_ok=True)
            from .operations import best_effort
            best_effort("watchdog", "watchdog_stopped")
        elif not alive(node):
            READY.unlink(missing_ok=True)
            with (ROOT / "satip-watchdog.log").open("wb") as log:
                child = subprocess.Popen(["/usr/bin/python3", "-m", "satip_appliance.watchdog", "run"],
                    env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "/opt/satip"}, start_new_session=True,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            atomic_json(PID, {"pid": child.pid, "start": process_identity(child.pid)}, durable=False)
            end = time.monotonic() + 3
            while time.monotonic() < end:
                if child.poll() is not None:
                    raise RuntimeError("Watchdog failed to start; inspect /run/satip-watchdog.log")
                if READY.exists() and read_json(READY).get("pid") == child.pid:
                    from .operations import best_effort
                    best_effort("watchdog", "watchdog_armed")
                    return
                time.sleep(.05)
            # Preserve PID identity for safe stop/inspection, never force KILL.
            raise RuntimeError("Watchdog startup timed out")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "run"))
    args = parser.parse_args()
    if args.action == "run":
        run()
    else:
        control(args.action)


if __name__ == "__main__":
    main()
