#!/usr/bin/env python3
"""Opt-in real-TV burst-seek timings; optionally preserve an existing USB session."""
import argparse
import asyncio
import grp
import json
import os
from pathlib import Path
import signal
import time

from satip_controller.config import Config, library
from satip_controller.mpv_ipc import MPVIPC, IPCError
from satip_controller.state import read_json, runtime_path
from satip_controller.timeshift import ranges


async def restore_session(ipc, status_path, owner, channel, capture, position, settled):
    current = read_json(status_path)
    assert (current["owner_pid"] == owner and current.get("playing") == channel
        and current["health"].get("relay_capture_pid") == capture
        and current["timeshift"]["enabled"]), "Original session lost; refusing cleanup controls"
    await ipc.command("set_property", "pause", True)
    if any(a <= position <= b for a, b in ranges(await ipc.get("demuxer-cache-state"))):
        started = time.monotonic()
        await ipc.command("seek", position, "absolute+exact")
        return (await settled(position, started, timeout=20))["target_reached"]
    # Never seek a non-seekable HTTP reader outside its packet cache merely to
    # restore a diagnostic's start frame. Leave the original USB session paused.
    return False


async def main():
    from evdev import UInput, ecodes
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-interrupt", action="store_true")
    parser.add_argument("--use-current-session", action="store_true",
                        help="Require paused USB playback; keep its capture/history, no channel switch or resume cycles")
    parser.add_argument("--wait-seconds", type=float, default=65)
    parser.add_argument("--cycles", type=int, default=1, help="Repeat seek bursts with resume/pause between cycles")
    parser.add_argument("--resume-seconds", type=float, default=3)
    parser.add_argument("--channel", help="Optional exact favourite name; restores the original channel")
    args = parser.parse_args()
    if not args.confirm_interrupt:
        parser.error("--confirm-interrupt is required (changes HDMI playback)")
    if args.wait_seconds < (0 if args.use_current_session else 65) or not 1 <= args.cycles <= 100 or args.resume_seconds < 0:
        parser.error("Require wait >=65 seconds (>=0 for current session), 1–100 cycles and nonnegative resume time")
    if args.use_current_session and args.channel:
        parser.error("--channel cannot be used with --use-current-session")
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    config = Config.load(Path("/etc/satip-player/config.json"))
    status_path = runtime_path(config.state_dir, "status.json")
    initial = read_json(status_path)
    assert initial.get("playing") and not initial.get("error"), "Healthy playback required"
    if args.use_current_session:
        assert initial["timeshift"]["mode"] == "usb" and initial["timeshift"]["usb_writing"], "Active USB session required"
        assert initial["timeshift"]["paused"], "Pause the existing session first"
    else:
        assert initial["timeshift"]["mode"] == "ram", "Start from live RAM playback"
    channels, favourites = library(read_json(config.library))
    original_channel = initial["playing"]
    selected = original_channel
    if args.channel:
        matches = [c for c in favourites if channels[c].name == args.channel]
        assert len(matches) == 1, "Channel must identify exactly one favourite"
        selected = matches[0]
    ipc = await MPVIPC.connect(config.runtime_dir / "mpv.sock")
    original_volume = await ipc.get("volume")
    original_mute = await ipc.get("mute")
    original_position = await ipc.get("time-pos")
    original_owner = initial["owner_pid"]
    original_capture = initial["health"].get("relay_capture_pid")
    if args.use_current_session:
        assert await ipc.get("pause") is True
        assert initial["timeshift"]["behind_live_seconds"] >= 60, "Need 60 seconds of forward history"
        assert initial["timeshift"]["available_seconds"] - initial["timeshift"]["behind_live_seconds"] >= 30, "Need backward history"
    ui = UInput({ecodes.EV_KEY: [ecodes.KEY_PLAYPAUSE, ecodes.KEY_INFO,
        ecodes.KEY_VOLUMEUP, ecodes.KEY_VOLUMEDOWN, ecodes.KEY_MUTE,
        ecodes.KEY_ESC, ecodes.KEY_UP, ecodes.KEY_DOWN]}, name="gpio_ir_recv", bustype=0)
    await asyncio.sleep(.5)
    os.chown(ui.device.path, -1, grp.getgrnam("satip-input").gr_gid)
    os.chmod(ui.device.path, 0o660)
    results = []

    async def tap(code):
        ui.write(ecodes.EV_KEY, code, 1)
        ui.syn()
        await asyncio.sleep(.04)
        ui.write(ecodes.EV_KEY, code, 0)
        ui.syn()

    async def settled(target, started, timeout=90):
        paths = set()
        stable = None
        final = None
        position = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                paths.add(await ipc.get("path"))
                position = await ipc.get("time-pos")
                seeking = await ipc.get("seeking")
            except IPCError:
                if ipc.closed:
                    raise
                # Properties temporarily disappear during reader reloads.
                stable = None
                await asyncio.sleep(.04)
                continue
            if isinstance(position, (int, float)) and abs(position - target) < 1 and not seeking:
                if stable is None:
                    stable = time.monotonic()
                if time.monotonic() - stable >= .4:
                    final = position
                    break
            else:
                stable = None
            await asyncio.sleep(.04)
        return {"settle_seconds": round(time.monotonic() - started, 3),
            "target_reached": final is not None, "error_seconds": round(final - target, 3) if final is not None else None,
            "last_position": position, "target": target,
            "observed_reader_paths": len(paths), "paused": await ipc.get("pause")}

    async def switch(channel):
        current = read_json(status_path)
        if current["playing"] == channel:
            return
        await tap(ecodes.KEY_ESC)
        await asyncio.sleep(.3)
        await tap(ecodes.KEY_MUTE)
        await asyncio.sleep(.3)
        index, target = favourites.index(current["requested"]), favourites.index(channel)
        down, up = (target - index) % len(favourites), (index - target) % len(favourites)
        for _ in range(min(down, up)):
            await tap(ecodes.KEY_DOWN if down <= up else ecodes.KEY_UP)
            await asyncio.sleep(.25)
        await tap(ecodes.KEY_MUTE)
        async with asyncio.timeout(25):
            while read_json(status_path).get("playing") != channel:
                await asyncio.sleep(.1)

    try:
        await asyncio.sleep(2)
        if not args.use_current_session:
            await switch(selected)
        initial = read_json(status_path)
        if not args.use_current_session:
            await tap(ecodes.KEY_PLAYPAUSE)
        async with asyncio.timeout(20):
            while not read_json(status_path)["timeshift"]["enabled"]:
                await asyncio.sleep(.1)
        assert await ipc.get("pause") is True
        initial_position = await ipc.get("time-pos")
        capture = read_json(status_path)["health"].get("relay_capture_pid")
        def healthy():
            current = read_json(status_path)
            assert current["owner_pid"] == original_owner, "Player restarted"
            assert current["playing"] == initial["playing"] and not current.get("error")
            assert current["timeshift"]["enabled"] and current["timeshift"]["usb_writing"]
            assert current["health"].get("relay_capture_pid") == capture, "Capture restarted"
        deadline = time.monotonic() + args.wait_seconds
        while time.monotonic() < deadline:
            healthy()
            await asyncio.sleep(min(5, max(0, deadline - time.monotonic())))
        for cycle in range(args.cycles):
            for label, codes, delta in (
                ("five_forward", [ecodes.KEY_VOLUMEUP] * 5, 50),
                ("five_backward", [ecodes.KEY_VOLUMEDOWN] * 5, -50),
                ("alternating", [ecodes.KEY_VOLUMEUP, ecodes.KEY_VOLUMEDOWN] * 2 + [ecodes.KEY_VOLUMEUP], 10),
                ("single_backward", [ecodes.KEY_VOLUMEDOWN], -10),
            ):
                before = await ipc.get("time-pos")
                before_path = await ipc.get("path")
                before_health = read_json(status_path).get("health", {})
                started = time.monotonic()
                for code in codes:
                    await tap(code)
                    await asyncio.sleep(.11)
                result = await settled(before + delta, started)
                result["reader_reloaded"] = await ipc.get("path") != before_path
                after_health = read_json(status_path).get("health", {})
                result["archive_reads"] = (after_health["archive_reads"] - before_health["archive_reads"]
                    if "archive_reads" in after_health and "archive_reads" in before_health else None)
                result["case"] = label
                result["cycle"] = cycle + 1
                results.append(result)
                print(json.dumps(result), flush=True)
                if not result["target_reached"]:
                    break
                await asyncio.sleep(1)
                healthy()
            assert all(r["target_reached"] for r in results), "Seek target check failed"
            if cycle + 1 < args.cycles and not args.use_current_session:
                await tap(ecodes.KEY_PLAYPAUSE)
                async with asyncio.timeout(15):
                    while await ipc.get("pause") is True:
                        await asyncio.sleep(.1)
                before = await ipc.get("time-pos")
                await asyncio.sleep(args.resume_seconds)
                assert await ipc.get("time-pos") > before + args.resume_seconds * .5
                healthy()
                await tap(ecodes.KEY_PLAYPAUSE)
                async with asyncio.timeout(15):
                    while await ipc.get("pause") is not True:
                        await asyncio.sleep(.1)
        assert read_json(status_path)["playing"] == initial["playing"], "Channel changed unexpectedly"
        print(json.dumps({"channel": channels[initial["playing"]].name, "pause_position": initial_position,
            "wait_seconds": args.wait_seconds, "results": results}), flush=True)
        assert all(r["target_reached"] and r["paused"] for r in results), "Seek target/pause check failed"
    finally:
        try:
            if args.use_current_session:
                restored = await restore_session(ipc, status_path, original_owner, original_channel,
                                                 original_capture, original_position, settled)
                print(json.dumps({"session_preserved": True, "original_position_restored": restored}), flush=True)
            else:
                await tap(ecodes.KEY_INFO)
                async with asyncio.timeout(25):
                    while read_json(status_path)["timeshift"]["enabled"]:
                        await asyncio.sleep(.1)
                await switch(original_channel)
            await ipc.command("set_property", "volume", original_volume)
            await ipc.command("set_property", "mute", original_mute)
            await asyncio.sleep(1.5)  # Allow normal controller state sampling.
        finally:
            ui.close()
            await ipc.close()


if __name__ == "__main__":
    asyncio.run(main())
