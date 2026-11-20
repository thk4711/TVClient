#!/usr/bin/env python3
"""Disrupt one Buildroot service and verify its supervisor recovers it.

Run ON the Pi as root with PYTHONPATH=/opt/satip. Never target the hardware
watchdog feeder: doing so can hard-reset the board and lose recent SD writes.
"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import time

from satip_controller.backend import BackendError
from satip_appliance.health import http_json
from satip_appliance.supervisor import group_exists, process_identity
from satip_controller.state import lease_active, read_json


def generation(service):
    return read_json(Path("/run/satip-supervisor") / (service + ".generation")).get("pgid")


def check_idle():
    for name in ("scan-lease.json", "preview-lease.json", "recovery.json"):
        path = Path("/run/satip-management") / name
        node = read_json(path) if path.exists() else {}
        if lease_active(node) or node.get("pending") or node.get("recovery_pending"):
            raise RuntimeError("Scan/preview/recovery active; test refused")
    if Path("/data/store/web/mux-rollback.json").exists():
        raise RuntimeError("Scan rollback pending; test refused")


def useful(service, pid):
    try:
        if service == "player":
            status = read_json(Path("/run/satip-status/status.json"))
            progress = read_json(Path("/run/satip-player/progress.json"))
            return (status.get("owner_pid") == pid and status.get("playing") and not status.get("paused")
                    and progress.get("pid") == pid and time.monotonic() - progress["monotonic"] < 5)
        if service == "web":
            return http_json(8080, "/healthz").get("pid") == pid
        from satip_controller.backend import Backend
        from satip_controller.config import credentials
        backend = Backend(credentials(Path("/etc/satip-web/tvheadend-admin.json")), timeout=1)
        return bool(backend._request("serverinfo", {}, False).get("sw_version"))
    except (OSError, ValueError, KeyError, BackendError):
        return False


def preview_request(action, payload):
    from satip_controller.backend import Backend
    from satip_controller.config import credentials
    header = Backend(credentials(Path("/etc/satip-web/login.json"))).header
    connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=3)
    try:
        connection.request("POST", "/api/preview" + action, json.dumps(payload),
                           {"Authorization": header, "X-Satip-Action": "1", "Content-Type": "application/json"})
        response = connection.getresponse()
        data = response.read(4097)
        if response.status not in (200, 202) or len(data) > 4096:
            raise RuntimeError("Preview request failed")
        return json.loads(data)
    finally:
        connection.close()


def start_test_preview():
    from satip_controller.config import library
    channels, _ = library(read_json(Path("/data/store/management/library.json")))
    playing = read_json(Path("/run/satip-status/status.json"))["playing"]
    channel = channels[playing]
    session = preview_request("", {"service": channel.service_uuid, "confirm_pause": True})["session"]
    try:
        end = time.monotonic() + 60
        while time.monotonic() < end:
            preview_request("/heartbeat", {"session": session})
            media = Path("/run/satip-web/preview") / session
            if (media / "index.m3u8").exists() and any(media.glob("segment-*.ts")):
                return session
            time.sleep(.5)
        raise RuntimeError("Preview did not produce HLS")
    except BaseException:
        preview_request("/stop", {"session": session})
        raise


def configuration_hashes():
    # Compare stable user configuration only, not EPG/runtime/player checkpoints.
    store = Path("/data/store")
    paths = list((store / "config").rglob("*.json"))
    paths += [store / "management/library.json", store / "installation.json"]
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", choices=("player", "web", "tvheadend"), required=True)
    parser.add_argument("--target", choices=("service", "supervisor"), default="service")
    parser.add_argument("--failure", choices=("crash", "freeze"), required=True)
    parser.add_argument("--preview-before-failure", action="store_true",
                        help="Web supervisor only: produce HLS before fault; verify return to HDMI")
    parser.add_argument("--confirm-interrupt", action="store_true")
    args = parser.parse_args()
    if not args.confirm_interrupt or os.geteuid() != 0:
        parser.error("Requires root and --confirm-interrupt; this disrupts viewing")
    if args.preview_before_failure and (args.service != "web" or args.target != "supervisor"):
        parser.error("Preview exercise requires --service web --target supervisor")
    check_idle()
    old = generation(args.service)
    target = (read_json(Path("/run/satip-supervisor") / (args.service + ".pid.json"))["pid"]
              if args.target == "supervisor" else old)
    identity = process_identity(target)
    config = configuration_hashes()
    if not identity or not useful(args.service, old):
        raise RuntimeError("Service is not healthy before test")
    fd = os.pidfd_open(target)
    session = None
    try:
        if args.preview_before_failure:
            session = start_test_preview()
        if process_identity(target) != identity or generation(args.service) != old:
            raise RuntimeError("Generation changed; test refused")
        began = time.monotonic()
        signal.pidfd_send_signal(fd, signal.SIGKILL if args.failure == "crash" else signal.SIGSTOP)
        end = began + 180
        next_heartbeat = began
        while time.monotonic() < end:
            if session and time.monotonic() >= next_heartbeat:
                try:
                    preview_request("/heartbeat", {"session": session})
                except (OSError, RuntimeError, http.client.HTTPException):
                    pass  # Expected once the old web generation is stopped.
                next_heartbeat = time.monotonic() + 5
            try:
                new = generation(args.service)
                replacement = read_json(Path("/run/satip-supervisor") / (args.service + ".pid.json")).get("pid")
                if (new and new != old and not group_exists(old) and useful(args.service, new)
                        and (args.target != "supervisor" or replacement != target)
                        and (not session or useful("player", generation("player")))):
                    if configuration_hashes() != config:
                        raise RuntimeError("Persistent configuration changed during recovery")
                    print(json.dumps({"service": args.service, "failure": args.failure,
                        "target": args.target, "old_supervisor_pid": target if args.target == "supervisor" else None,
                        "new_supervisor_pid": replacement, "configuration_unchanged": True,
                        "preview_hls_then_hdmi_recovered": bool(session),
                        "old_pid": old, "new_pid": new, "recovered_seconds": round(time.monotonic() - began, 2),
                        "old_group_gone": True}))
                    return
            except (OSError, ValueError):
                pass
            time.sleep(.5)
        raise RuntimeError("Recovery timed out; inspect exact owned generation")
    finally:
        if session:
            try:
                preview_request("/stop", {"session": session})
            except (OSError, RuntimeError, http.client.HTTPException):
                pass
        # If a test fails, do not leave the original application deliberately
        # paused. pidfd remains tied to this process even if its PID is reused.
        if args.failure == "freeze":
            try:
                signal.pidfd_send_signal(fd, signal.SIGCONT)
            except ProcessLookupError:
                pass
        os.close(fd)


if __name__ == "__main__":
    main()
