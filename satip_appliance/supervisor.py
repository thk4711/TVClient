"""Linux subreaper: one non-root service generation at a time, exact PGID cleanup."""
import argparse
import ctypes
import fcntl
import os
from pathlib import Path
import pwd
import select
import secrets
import signal
import socket
import subprocess
import termios
import time

from .persistent import validate_mount, schema_valid
from satip_controller.state import read_json, atomic_json
from satip_controller.backend import BackendError

SERVICES = {
    "player": ("satip-player", ["/usr/bin/python3", "-m", "satip_controller", "--config", "/etc/satip-player/config.json"]),
    "web": ("satip-web", ["/usr/bin/python3", "-m", "satip_appliance.web"]),
    "tvheadend": ("tvheadend", ["/usr/bin/tvheadend", "-c", "/data/store/tvheadend", "--bindaddr", "127.0.0.1", "--nosyslog"]),
}


def reap(leader):
    # Keep the direct leader unreaped until cleanup is complete. Its zombie
    # reserves the numeric PID/PGID, unlike merely retaining a pidfd reference.
    while True:
        children = Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()
        reaped = 0
        for child in children:
            if int(child) == leader:
                continue
            try:
                reaped += bool(os.waitpid(int(child), os.WNOHANG)[0])
            except ChildProcessError:
                pass
        if not reaped:
            return


def group_exists(pgid):
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid and fields[0] != "Z":
                return True
        except (FileNotFoundError, ProcessLookupError):
            pass
    return False


def cleanup(pgid, grace=10, final=3):
    """Never restart until every member of the owned generation has gone."""
    for sig, duration in ((signal.SIGTERM, grace), (signal.SIGKILL, final)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        end = time.monotonic() + duration
        while time.monotonic() < end:
            reap(pgid)
            if not group_exists(pgid):
                reap(pgid)
                return
            time.sleep(.05)
    raise RuntimeError("Owned service group did not stop; restart refused")


def child_setup(account, tty=False):
    os.setsid()
    if tty:
        fd = os.open("/dev/tty1", os.O_RDWR)
        fcntl.ioctl(fd, termios.TIOCSCTTY, 0)
        os.tcsetpgrp(fd, os.getpgrp())
        os.dup2(fd, 0)
        os.close(fd)
    os.initgroups(account.pw_name, account.pw_gid)
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)
    os.umask(0o077)
    libc = ctypes.CDLL(None, use_errno=True)
    # PR_SET_NO_NEW_PRIVS; the owned child cannot regain privilege through exec.
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "no_new_privs")


def ready(service):
    if not schema_valid(read_json(Path("/run/satip-data-ready"))):
        return False
    validate_mount(Path("/data"), probe=False)
    from .provision import activate
    if service == "player":
        activate()
    state = read_json(Path("/data/store/installation.json")).get("state")
    if state not in ("qualified", "configured", "unconfigured"):
        return False
    if state == "unconfigured" and (service == "player" or not Path("/data/store/factory-ready.json").is_file()):
        return False
    if service == "tvheadend":
        return any(Path("/data/store/tvheadend/accesscontrol").glob("*"))
    # Local TCP readiness only; the application performs authenticated API calls.
    try:
        with socket.create_connection(("127.0.0.1", 9981), timeout=.5):
            return True
    except OSError:
        return False


def supervise(service, directory=Path("/run/satip-supervisor")):
    os.umask(0o077)
    directory.mkdir(mode=0o700, exist_ok=True)
    with (directory / (service + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # PR_SET_CHILD_SUBREAPER: orphaned mpv/FFmpeg descendants are reaped here.
        if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) != 0:
            raise RuntimeError("Subreaper unavailable")
        stopped = False
        def stop(*_):
            nonlocal stopped
            stopped = True
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, stop)
        account_name, command = SERVICES[service]
        command = list(command)
        from .registration import Registration
        registration = Registration() if service == "tvheadend" else None
        from .startup import StartupGate
        startup = StartupGate()
        from .config_restore import ConfigRestore
        config_restore = ConfigRestore() if service == "player" else None
        account = pwd.getpwnam(account_name)
        from .epg_persistence import EPGPersistence
        epg_persistence = EPGPersistence() if service == "tvheadend" else None
        home = "/run/satip-" + service if service != "tvheadend" else "/run/tvheadend"
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8",
               "PYTHONPATH": "/opt/satip", "SATIP_RUNTIME_ROOT": "/run", "HOME": home,
               "XDG_CACHE_HOME": home + "/cache", "XDG_RUNTIME_DIR": home,
               "TMPDIR": home}
        if service == "player":
            env["SATIP_BOOT_SPLASH"] = "1"
            feedback = Path("/run/satip-tuning")
            feedback.mkdir(mode=0o755, exist_ok=True)
            os.chmod(feedback, 0o755)
            env["SATIP_TUNE_STATUS"] = str(feedback / "status.json")
        from .guardian import publish
        progress = directory / (service + ".progress")
        previous_phase, next_pulse = None, 0
        def pulse(phase):
            nonlocal previous_phase, next_pulse
            now = time.monotonic()
            if phase != previous_phase or now >= next_pulse:
                publish(progress, phase, now)
                previous_phase, next_pulse = phase, now + 2
        backoff = 1
        generation = directory / (service + ".generation")
        if generation.exists():
            raise RuntimeError("Unclean supervisor exit; inspect owned group before restarting")
        (directory / (service + ".ready")).touch(mode=0o600)
        print(f"Supervisor {service} ready ({time.monotonic():.3f} s since Linux start)", flush=True)
        while not stopped:
            pulse("waiting")
            display_fd = None
            try:
                if config_restore:
                    config_restore.refresh()
                available = ready(service)
                if (available and service == "tvheadend" and
                        read_json(Path("/data/store/installation.json")).get("state") != "unconfigured"):
                    available = startup.backend(registration.url)
                if available and service == "player":
                    available = startup.player(Registration().url)
                if available and epg_persistence:
                    epg_persistence.configure(account.pw_uid, account.pw_gid)
            except (OSError, ValueError, KeyError, BackendError, subprocess.SubprocessError):
                available = False
            if not available:
                # Bounded probe/retry; no network-online wait in the init chain.
                time.sleep(.5)
                continue
            if service == "player":
                from .splash import release
                display_fd = release(keep_open=True)
                subprocess.run(["/usr/bin/chvt", "1"], check=True, timeout=3)
                os.chown("/dev/tty1", account.pw_uid, account.pw_gid)
                os.chmod("/dev/tty1", 0o600)
            began = time.monotonic()
            env["SATIP_GENERATION"] = secrets.token_hex(16)
            if service == "player":
                env["SATIP_PROGRESS_FILE"] = "/run/satip-player/progress.json"
            server_url = registration.url if registration else None
            launch_command = command + (["--satip_xml", server_url] if server_url else [])
            # Never execute service-writable code as root. preexec drops privilege
            # before the fixed executable and immutable PYTHONPATH are used.
            with (directory / (service + ".log")).open("wb") as log:
                # Mark before spawn: an interrupted launch never permits another
                # generation until an administrator inspects it or reboots.
                pulse("launching")
                atomic_json(generation, {"launching": True}, durable=False)
                child = subprocess.Popen(launch_command, env=env, cwd="/opt/satip", stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    preexec_fn=lambda: child_setup(account, service == "player"))
                print(f"Service {service} launched ({time.monotonic():.3f} s since Linux start)", flush=True)
                restart_reason = None
                try:
                    atomic_json(generation, {"pgid": child.pid, "start": process_identity(child.pid),
                        "generation": env["SATIP_GENERATION"]}, durable=False)
                    from .health import Health
                    health = Health(service, child.pid, env["SATIP_GENERATION"], began)
                    from .tuning import TuneMonitor
                    tuning = TuneMonitor(child.pid, env["SATIP_GENERATION"], began) if service == "player" else None
                    restart_reason = "process_exit"
                    next_data_check = 0
                    while not stopped and os.waitid(os.P_PID, child.pid,
                            os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                        pulse("running")
                        if display_fd is not None and time.monotonic()-began >= 10:
                            os.close(display_fd)
                            display_fd = None
                        if time.monotonic() >= next_data_check:
                            try:
                                validate_mount(Path("/data"), probe=False)
                                if epg_persistence:
                                    epg_persistence.flush()
                            except (OSError, ValueError, subprocess.SubprocessError):
                                Path("/run/satip-data-ready").unlink(missing_ok=True)
                                restart_reason = None
                                break
                            next_data_check = time.monotonic() + 2
                            if registration and registration.refresh() != server_url:
                                restart_reason = None
                                break  # Cleanup this generation before launching the selected server.
                        failure = health.check()
                        if failure:
                            restart_reason = failure
                            print(f"Recovery {service}: {failure}; cleaning owned generation", flush=True)
                            break
                        if tuning:
                            tuning.check()
                        if select.select([child.stdout], [], [], .1)[0]:
                            chunk = os.read(child.stdout.fileno(), 65536)
                            if log.tell() + len(chunk) > 1024 * 1024:
                                log.seek(0)
                                log.truncate()
                            log.write(chunk)
                            log.flush()
                finally:
                    pulse("cleanup")
                    cleanup(child.pid, grace=40 if service == "web" else 10)
                    child.wait()
                    child.stdout.close()
                    if display_fd is not None:
                        os.close(display_fd)
                    generation.unlink()
                    if not stopped and restart_reason:
                        from .operations import best_effort
                        best_effort(service, restart_reason)
                    if epg_persistence:
                        try:
                            epg_persistence.flush()
                        except (OSError, ValueError):
                            Path("/run/satip-data-ready").unlink(missing_ok=True)
                            print("EPG database flush failed; data readiness revoked", flush=True)
            if time.monotonic() - began > 60:
                backoff = 1
            end = time.monotonic() + backoff
            while not stopped and time.monotonic() < end:
                pulse("backoff")
                time.sleep(.1)
            backoff = min(backoff * 2, 30)


def process_identity(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return fields[19] if fields[0] != "Z" else None


def direct_control(service, action, directory=Path("/run/satip-supervisor")):
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / (service + ".pid.json")
    with (directory / (service + ".control")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        node = read_json(path) if path.exists() else None
        alive = False
        if node:
            try:
                alive = node["start"] is not None and process_identity(node["pid"]) == node["start"]
            except FileNotFoundError:
                pass
        if action == "stop":
            if alive:
                fd = os.pidfd_open(node["pid"])
                try:
                    if process_identity(node["pid"]) != node["start"]:
                        raise RuntimeError("Supervisor identity changed")
                    signal.pidfd_send_signal(fd, signal.SIGTERM)
                    end = time.monotonic() + 45
                    while time.monotonic() < end:
                        try:
                            if process_identity(node["pid"]) != node["start"]:
                                break
                        except FileNotFoundError:
                            break
                        time.sleep(.05)
                    else:
                        raise RuntimeError("Supervisor stop timed out; no restart")
                finally:
                    os.close(fd)
            path.unlink(missing_ok=True)
        elif not alive:
            if (directory / (service + ".generation")).exists():
                raise RuntimeError("Unclean generation; restart refused")
            ready_path = directory / (service + ".ready")
            ready_path.unlink(missing_ok=True)
            with (directory / (service + ".supervisor.log")).open("wb") as log:
                os.chmod(log.name, 0o600)
                child = subprocess.Popen(["/usr/bin/python3", "-m", "satip_appliance.supervisor", service],
                    env=dict(os.environ, PYTHONPATH="/opt/satip"), start_new_session=True,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            atomic_json(path, {"pid": child.pid, "start": process_identity(child.pid)})
            end = time.monotonic() + 3
            while not ready_path.exists() and time.monotonic() < end:
                if child.poll() is not None:
                    raise RuntimeError("Supervisor failed to start")
                time.sleep(.05)
            if not ready_path.exists():
                raise RuntimeError("Supervisor startup timed out")


def control(service, action, directory=Path("/run/satip-supervisor")):
    from .guardian import control as guarded_control
    guarded_control(service, action, directory)


def control_all(action, root=Path("/")):
    # Reuse one interpreter for ordered control calls, retaining each daemon
    # acknowledgement and the same stop/cleanup contract.
    from .operations import best_effort
    services = ("tvheadend", "web", "player")
    if action == "start":
        best_effort("appliance", "boot")
    for service in services if action == "start" else reversed(services):
        if action == "start":
            (root / "run/satip-boot-stage").write_text(f"service-{service}\n")
        try:
            control(service, action, root / "run/satip-supervisor")
            if action == "start" and service == "tvheadend":
                store = root / "data/store"
                journals = ("web/tv-source-transaction.json", "web/satip-tuner-transaction.json", "web/enigma2-import/mux-rollback.json",
                            "web/enigma2-import/network-rollback.json")
                if any((store / name).exists() for name in journals):
                    from .tv_source import recover
                    from satip_controller.backend import Backend, BackendError
                    from satip_controller.config import credentials
                    import asyncio
                    async def restore_source():
                        backend = Backend(credentials(store / "config/web/tvheadend-admin.json"))
                        async with asyncio.timeout(20):
                            while True:
                                try:
                                    await backend.grid("status/subscriptions")
                                    break
                                except BackendError:
                                    await asyncio.sleep(.25)
                            await recover(backend, store)
                    asyncio.run(restore_source())
        except Exception:
            if action == "start":
                path = root / "run/satip-boot-error"
                if not path.exists() or not path.stat().st_size:
                    path.write_text(f"service-{service}\n")
            raise
    if action == "stop":
        best_effort("appliance", "shutdown")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=(*SERVICES, "all"))
    parser.add_argument("action", choices=("start", "stop"), nargs="?")
    args = parser.parse_args()
    if args.service == "all":
        if not args.action:
            parser.error("all requires start or stop")
        control_all(args.action)
    elif args.action:
        control(args.service, args.action)
    else:
        supervise(args.service)


if __name__ == "__main__":
    main()
