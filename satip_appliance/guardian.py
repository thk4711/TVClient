"""Root subreaper above one supervisor; adopted children are exact ownership."""
import argparse
import ctypes
import fcntl
import math
import os
from pathlib import Path
import select
import signal
import subprocess
import time

from satip_controller.state import atomic_json, boot_id
from .health import small_json
from .supervisor import SERVICES, process_identity

DIRECTORY = Path("/run/satip-supervisor")
PROGRESS_TIMEOUT = 45
CLEANUP_TIMEOUT = 100


def identity(pid):
    # Includes zombies: retaining an unreaped child reserves its PID.
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def publish(path, phase, now=None):
    atomic_json(path, {"pid": os.getpid(), "start": identity(os.getpid()),
        "boot_id": boot_id(), "monotonic": time.monotonic() if now is None else now,
        "phase": phase}, durable=False)


def fresh(path, pid, start, now=None, timeout=PROGRESS_TIMEOUT):
    try:
        node = small_json(path)
        # Sample after the read: a writer can publish between the caller's
        # clock sample and our read. That valid record is not from the future.
        now = time.monotonic() if now is None else now
        stamp = node.get("monotonic")
        return (node.get("pid") == pid and node.get("start") == start
                and node.get("boot_id") == boot_id()
                and type(stamp) in (int, float) and math.isfinite(stamp)
                and 0 <= now - stamp <= timeout)
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def children():
    return [int(p) for p in Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()]


def exited(pid):
    return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def terminate(pid, grace):
    # This PID is a direct unreaped child, not a number taken from a stale file.
    if exited(pid):
        return
    fd = os.pidfd_open(pid)
    try:
        try:
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            signal.pidfd_send_signal(fd, signal.SIGCONT)
        except ProcessLookupError:
            if exited(pid):
                return
            raise
        if not select.select([fd], [], [], grace)[0]:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
            if not select.select([fd], [], [], 3)[0]:
                raise RuntimeError("Owned child cannot be killed")
    except ProcessLookupError:
        if not exited(pid):
            raise
    finally:
        os.close(fd)


def drain(supervisor, grace):
    """Stop supervisor, then all adopted descendants, including escaped sessions.

    Do not reap any child until every live descendant has exited. Dead leaders
    reserve PID/PGID values throughout cleanup. No process-name or UID matching.
    """
    terminate(supervisor, grace)
    end = time.monotonic() + grace + 6
    while True:
        live = [pid for pid in children() if not exited(pid)]
        if not live:
            break
        for pid in live:
            terminate(pid, max(0, min(grace, end - time.monotonic())))
        if time.monotonic() > end:
            raise RuntimeError("Adopted descendants did not stop")
    for pid in children():
        os.waitpid(pid, 0)


def run(service, directory=DIRECTORY):
    os.umask(0o077)
    directory.mkdir(mode=0o700, exist_ok=True)
    with (directory / (service + ".guard.lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) != 0:
            raise RuntimeError("Guardian subreaper unavailable")
        stopped = False
        def stop(*_):
            nonlocal stopped
            stopped = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        progress = directory / (service + ".guard.progress")
        failures = []
        while not stopped:
            publish(progress, "starting")
            from .supervisor import direct_control
            direct_control(service, "start", directory)
            node = small_json(directory / (service + ".pid.json"))
            pid, start = node["pid"], node["start"]
            began = time.monotonic()
            reason = None
            while not stopped:
                now = time.monotonic()
                publish(progress, "monitoring")
                if exited(pid):
                    reason = "supervisor_exit"
                    break
                # Cleanup/backoff phases are bounded, not heartbeat threads.
                timeout = CLEANUP_TIMEOUT if supervisor_phase(directory, service) == "cleanup" else PROGRESS_TIMEOUT
                if now - began > timeout and not fresh(directory / (service + ".progress"), pid, start, timeout=timeout):
                    reason = "supervisor_timeout"
                    break
                time.sleep(.5)
            publish(progress, "cleanup")
            drain(pid, 40 if service == "web" else 10)
            # Only this continuously owning subreaper may clear an unclean marker.
            for suffix in (".generation", ".pid.json", ".ready", ".progress"):
                (directory / (service + suffix)).unlink(missing_ok=True)
            if reason:
                from .operations import best_effort
                best_effort(service, reason)
                now = time.monotonic()
                failures = [stamp for stamp in failures if now - stamp < 300] + [now]
                if len(failures) >= 5:
                    raise RuntimeError("Repeated supervisor failures; watchdog escalation")
            end = time.monotonic() + min(30, 2 ** len(failures))
            while not stopped and time.monotonic() < end:
                publish(progress, "backoff")
                time.sleep(.5)
        publish(progress, "stopped")


def supervisor_phase(directory, service):
    try:
        return small_json(directory / (service + ".progress")).get("phase")
    except (OSError, ValueError, AttributeError):
        return None


def control(service, action, directory=DIRECTORY):
    os.umask(0o077)
    directory.mkdir(mode=0o700, exist_ok=True)
    expected = directory / (service + ".guard.expected")
    path = directory / (service + ".guard.pid.json")
    with (directory / (service + ".guard.control")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            node = small_json(path)
            alive = node["start"] is not None and process_identity(node["pid"]) == node["start"]
        except (OSError, ValueError, KeyError, TypeError):
            node, alive = None, False
        if action == "stop":
            if expected.exists() and not alive:
                raise RuntimeError("Lost guardian; cannot prove cleanup, watchdog remains armed")
            if alive:
                fd = os.pidfd_open(node["pid"])
                try:
                    if process_identity(node["pid"]) != node["start"]:
                        raise RuntimeError("Guardian identity changed")
                    signal.pidfd_send_signal(fd, signal.SIGTERM)
                    if not select.select([fd], [], [], CLEANUP_TIMEOUT)[0]:
                        raise RuntimeError("Guardian stop timed out; watchdog remains armed")
                finally:
                    os.close(fd)
            if alive and (not fresh(directory / (service + ".guard.progress"), node["pid"], node["start"])
                          or small_json(directory / (service + ".guard.progress")).get("phase") != "stopped"):
                raise RuntimeError("Guardian did not acknowledge verified cleanup")
            if (directory / (service + ".generation")).exists():
                raise RuntimeError("Unowned generation; stop refused")
            expected.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
        elif not alive:
            if expected.exists() or (directory / (service + ".generation")).exists():
                raise RuntimeError("Lost guardian ownership; restart refused")
            # Expectation survives a crash; only verified orderly stop removes it.
            atomic_json(expected, {"began": time.monotonic()}, durable=False)
            with (directory / (service + ".guard.log")).open("wb") as log:
                child = subprocess.Popen(["/usr/bin/python3", "-m", "satip_appliance.guardian", service],
                    env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONPATH": "/opt/satip"},
                    start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            atomic_json(path, {"pid": child.pid, "start": process_identity(child.pid)}, durable=False)
            end = time.monotonic() + 5
            while time.monotonic() < end:
                if child.poll() is not None:
                    raise RuntimeError("Guardian startup failed; inspect guardian log")
                if fresh(directory / (service + ".guard.progress"), child.pid,
                         process_identity(child.pid), time.monotonic()):
                    return
                time.sleep(.05)
            raise RuntimeError("Guardian startup timed out")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=SERVICES)
    run(parser.parse_args().service)


if __name__ == "__main__":
    main()
