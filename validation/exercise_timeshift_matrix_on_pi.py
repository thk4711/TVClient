#!/usr/bin/env python3
"""Short installed HD/SD control matrix; virtual remote, actual tuner/player.

No formatting or persistent configuration edits. Returns to the original channel.
Requires configured favourites containing both H.264 HD and MPEG-2 SD.
"""
import asyncio
import grp
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
from evdev import UInput, ecodes
from satip_controller.config import Config, library
from satip_controller.mpv_ipc import MPVIPC
from satip_controller.state import read_json, runtime_path


async def main():
    config = Config.load(Path("/etc/satip-player/config.json"))
    channels, favourites = library(read_json(config.library))
    path = runtime_path(config.state_dir, "status.json")
    def status():
        return read_json(path)
    async with asyncio.timeout(30):
        while not status().get("playing"):
            await asyncio.sleep(.2)
    original = status()["playing"]
    targets = [next(u for u in favourites if channels[u].video == kind) for kind in ("h264", "mpeg2")]
    ui = UInput({ecodes.EV_KEY: [ecodes.KEY_INFO, ecodes.KEY_MUTE, ecodes.KEY_PLAYPAUSE,
        ecodes.KEY_VOLUMEUP, ecodes.KEY_VOLUMEDOWN, ecodes.KEY_CHANNELDOWN, ecodes.KEY_CHANNELUP]}, name="gpio_ir_recv", bustype=0)
    await asyncio.sleep(.5)
    os.chown(ui.device.path, -1, grp.getgrnam("satip-input").gr_gid)
    os.chmod(ui.device.path, 0o660)
    ipc = await MPVIPC.connect(config.runtime_dir / "mpv.sock")
    async def press(code, delay=.15):
        ui.write(ecodes.EV_KEY, code, 1); ui.syn()
        await asyncio.sleep(.05)
        ui.write(ecodes.EV_KEY, code, 0); ui.syn()
        await asyncio.sleep(delay)
    async def choose(uuid):
        if status().get("playing") == uuid:
            return 0
        current = status()["requested"]
        await press(ecodes.KEY_MUTE)
        down = (favourites.index(uuid) - favourites.index(current)) % len(favourites)
        up = (favourites.index(current) - favourites.index(uuid)) % len(favourites)
        for _ in range(min(down, up)):
            await press(ecodes.KEY_CHANNELDOWN if down <= up else ecodes.KEY_CHANNELUP)
        began = time.monotonic()
        await press(ecodes.KEY_MUTE)
        async with asyncio.timeout(35):
            while status().get("playing") != uuid or status().get("loading"):
                await asyncio.sleep(.2)
        return round(time.monotonic() - began, 2)
    async def wait_pause(paused):
        async with asyncio.timeout(10):
            while status()["timeshift"]["paused"] != paused:
                await asyncio.sleep(.2)
    results = []
    try:
        await asyncio.sleep(2)
        for uuid in targets:
            tune = await choose(uuid)
            assert status()["timeshift"]["available"]
            assert not status()["timeshift"]["enabled"]
            buffer = config.runtime_dir / ("rolling-" + urlsplit(await ipc.get("path")).path.split("/")[1] + ".json")
            initial_drop = await ipc.get("frame-drop-count")
            position = await ipc.get("time-pos")
            began = time.monotonic()
            await asyncio.sleep(15)
            elapsed = time.monotonic() - began
            assert await ipc.get("time-pos") > position + 10
            node = read_json(buffer)
            assert len(node["segments"]) >= 4
            drop = await ipc.get("frame-drop-count") - initial_drop
            await press(ecodes.KEY_PLAYPAUSE, 1)
            await wait_pause(True)
            paused_position = await ipc.get("time-pos")
            await asyncio.sleep(1)
            assert abs(await ipc.get("time-pos") - paused_position) < .2
            await press(ecodes.KEY_VOLUMEDOWN, 2)
            assert status()["timeshift"]["paused"]
            await press(ecodes.KEY_VOLUMEUP, 2)
            assert status()["timeshift"]["paused"]
            await press(ecodes.KEY_INFO, 1)
            await wait_pause(False)
            async with asyncio.timeout(10):
                while status()["timeshift"]["enabled"] or status()["timeshift"]["usb_writing"]:
                    await asyncio.sleep(.2)
            assert config.runtime_dir / ("rolling-" + urlsplit(await ipc.get("path")).path.split("/")[1] + ".json") == buffer
            assert not status().get("error")
            results.append({"channel": channels[uuid].name, "video": channels[uuid].video,
                "tune_seconds": tune, "sample_seconds": round(elapsed, 2),
                "rendering_drops": drop, "segments": len(node["segments"]),
                "paused_seek_and_return_live": "passed"})
        print(json.dumps(results, indent=2))
    finally:
        try:
            if status().get("timeshift", {}).get("paused"):
                await press(ecodes.KEY_INFO, 1)
            await choose(original)
        finally:
            ui.close()
            await ipc.close()


if __name__ == "__main__":
    asyncio.run(main())
