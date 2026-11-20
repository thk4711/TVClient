"""Opt-in fault test: freeze only the owned prebuffer FFmpeg, never the player."""
import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import time

from satip_controller.audio import normal_german
from satip_controller.config import Config, library
from satip_controller.coordination import preview_held
from satip_controller.mpv_ipc import IPCError, MPVIPC
from satip_controller.player import url
from satip_controller.rolling import process_start
from satip_controller.state import lease_active, read_json, runtime_path


async def exercise(args):
    if os.geteuid() != 0 or not args.confirm_interrupt:
        raise ValueError("Requires root and --confirm-interrupt; playback will be interrupted")
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    config = Config.load(args.config)
    status_file = runtime_path(config.state_dir, "status.json")
    initial = read_json(status_file)
    if not initial.get("playing") or initial.get("paused") or initial.get("error"):
        raise ValueError("Player must be playing normally")
    for name in ("scan-lease.json", "preview-lease.json"):
        path = runtime_path(config.management_dir, name)
        lease = read_json(path) if path.exists() else {}
        if lease.get("recovery_pending") or (preview_held(lease) if name.startswith("preview") else lease_active(lease)):
            raise ValueError("Finish scan/preview first")
    if initial.get("timeshift", {}).get("mode") != "ram":
        raise ValueError("Requires live RAM prebuffer, not active USB playback")
    channels, _ = library(read_json(config.library))
    channel = channels[initial["playing"]]
    ipc = await MPVIPC.connect(config.runtime_dir / "mpv.sock")
    fd = None
    stopped = False
    try:
        media_path = await ipc.get("path")
        if not isinstance(media_path, str) or not media_path.endswith("/live.ts"):
            raise ValueError("Requires live relay")
        token = media_path.split("/")[-2]
        directory = config.runtime_dir / ("rolling-" + token)
        owner = read_json(directory / "owner.json")
        capture_pid = owner["pid"]
        command = (Path("/proc") / str(capture_pid) / "cmdline").read_bytes().split(bytes([0]))
        legacy = b"satip_controller.rolling" in command and str(directory).encode() in command
        forked = (b"satip_controller.relay_worker" in command
                  and owner.get("start_ticks") == process_start(capture_pid))
        if not (legacy or forked):
            raise ValueError("Capture ownership changed")
        children = []
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                fields = dict(line.split(":", 1) for line in (proc / "status").read_text().splitlines() if ":" in line)
                command = (proc / "cmdline").read_bytes().split(bytes([0]))
                if (int(fields["PPid"]) == capture_pid and command[0] == b"ffmpeg"
                        and str(directory / "segment-%09d.ts").encode() in command):
                    children.append(int(proc.name))
            except (OSError, KeyError, ValueError):
                continue
        if len(children) != 1:
            raise ValueError("Cannot identify exactly one owned prebuffer FFmpeg")
        fd = os.pidfd_open(children[0])
        started = time.monotonic()
        signal.pidfd_send_signal(fd, signal.SIGSTOP)
        stopped = True
        observations = []
        async with asyncio.timeout(25):
            while True:
                status = read_json(status_file)
                if status.get("owner_pid") != initial["owner_pid"] or status.get("requested") != channel.uuid:
                    raise ValueError("Controller restarted or channel changed during fault test")
                health = status.get("health", {})
                observations.append({name: health.get(name) for name in
                    ("time-pos", "relay_age", "capture_write_age", "capture_queue_chunks")})
                with contextlib.suppress(IPCError):
                    if status.get("playing") == channel.uuid and await ipc.get("path") == url(channel):
                        position = await ipc.get("time-pos")
                        await asyncio.sleep(1)
                        later = await ipc.get("time-pos")
                        tracks = await ipc.get("track-list")
                        if later > position and any(t.get("selected") and normal_german(t) for t in tracks):
                            break
                await asyncio.sleep(.25)
        print(json.dumps({"direct_playback_recovered_seconds": round(time.monotonic() - started, 3),
            "controller_unchanged": True, "german_audio_selected": True,
            "max_capture_queue_chunks": max((s["capture_queue_chunks"] or 0 for s in observations), default=0),
            "physical_picture_audio_verified": False}), flush=True)
    finally:
        if fd is not None:
            if stopped:
                with contextlib.suppress(ProcessLookupError):
                    signal.pidfd_send_signal(fd, signal.SIGCONT)
            os.close(fd)
        await ipc.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/satip-player/config.json")
    parser.add_argument("--confirm-interrupt", action="store_true")
    asyncio.run(exercise(parser.parse_args()))
