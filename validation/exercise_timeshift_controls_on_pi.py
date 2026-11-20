#!/usr/bin/env python3
"""Exercise installed TV controls through virtual remote events; no formatting.

Requires root, evdev/uinput, configured playback and USB. Temporarily uses a
one-minute retained duration for a real-channel paused-expiry test, restoring the
readiness marker in finally. Changes TV controls, not persisted device settings.
Physical HDMI/audio/remote observation remains separate.
"""
import asyncio
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from evdev import UInput, ecodes
from satip_controller.config import Config
from satip_controller.mpv_ipc import MPVIPC
from satip_controller.state import atomic_json, read_json, runtime_path
from satip_controller.timeshift import READY


async def main():
    config = Config.load(Path("/etc/satip-player/config.json"))
    status_path = runtime_path(config.state_dir, "status.json")
    def status():
        return read_json(status_path)
    def ts():
        return status()["timeshift"]
    async with asyncio.timeout(30):
        while not (status().get("playing") and ts().get("available")):
            await asyncio.sleep(.2)
    ui = UInput({ecodes.EV_KEY: [ecodes.KEY_INFO, ecodes.KEY_MUTE, ecodes.KEY_PLAYPAUSE,
        ecodes.KEY_VOLUMEUP, ecodes.KEY_VOLUMEDOWN]}, name="gpio_ir_recv", bustype=0)
    import grp
    import os
    await asyncio.sleep(.5)  # Let udev finish initializing the virtual node.
    # Only our virtual node: a fake name has no real GPIO driver udev match.
    os.chown(ui.device.path, -1, grp.getgrnam("satip-input").gr_gid)
    os.chmod(ui.device.path, 0o660)
    ipc = await MPVIPC.connect(config.runtime_dir / "mpv.sock")
    original = READY.read_bytes()
    marker = json.loads(original)
    marker["minutes"] = 1
    results = []
    async def press(code):
        ui.write(ecodes.EV_KEY, code, 1)
        ui.syn()
        await asyncio.sleep(.05)
        ui.write(ecodes.EV_KEY, code, 0)
        ui.syn()
        await asyncio.sleep(1.5)
    async def wait(predicate, timeout=10):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(.2)
    try:
        atomic_json(READY, marker, 0o644, durable=False)
        await asyncio.sleep(2)  # Input hotplug discovery.
        channel = status()["playing"]
        assert ts()["mode"] == "ram" and not ts()["usb_writing"]
        results.append("live TV uses RAM only")
        buffer = config.runtime_dir / ("rolling-" + urlsplit(await ipc.get("path")).path.split("/")[1] + ".json")
        await press(ecodes.KEY_PLAYPAUSE)
        await wait(lambda: ts()["paused"])
        position = await ipc.get("time-pos")
        await asyncio.sleep(3)
        assert abs(await ipc.get("time-pos") - position) < .2
        results.append("remote pause preserves picture position")
        await press(ecodes.KEY_VOLUMEDOWN)
        assert ts()["paused"]
        assert config.runtime_dir / ("rolling-" + urlsplit(await ipc.get("path")).path.split("/")[1] + ".json") == buffer
        await press(ecodes.KEY_VOLUMEUP)
        assert ts()["paused"]
        results.append("paused left/right keep pause and capture identity")
        await press(ecodes.KEY_PLAYPAUSE)
        await wait(lambda: not ts()["paused"])
        await press(ecodes.KEY_INFO)
        await wait(lambda: not ts()["paused"] and not ts()["enabled"] and not ts()["usb_writing"])
        results.append("Apple Menu returns to live and stops USB writing")
        await press(ecodes.KEY_PLAYPAUSE)
        await wait(lambda: ts()["paused"])
        await press(ecodes.KEY_MUTE)  # Centre: favourites.
        await press(ecodes.KEY_INFO)  # Details instead of Return to live.
        assert ts()["paused"]
        results.append("Menu retains overlay navigation while delayed")
        await press(ecodes.KEY_INFO)  # Close details.
        initial_start = min(s["start"] for s in read_json(buffer)["segments"])
        initial_playback_path = await ipc.get("path")
        deadline = time.monotonic() + 80
        advanced = False
        while time.monotonic() < deadline:
            assert status()["playing"] == channel and not status().get("error")
            assert ts()["paused"]
            # RAM metadata is always short; the public retained duration
            # refers to the USB session. Expiry must actually move the frame.
            node = read_json(buffer)
            if node["segments"] and node["segments"][0]["start"] > initial_start and await ipc.get("path") != initial_playback_path:
                advanced = True
                break
            await asyncio.sleep(1)
        assert advanced, "Retention did not roll"
        await asyncio.sleep(4)
        assert ts()["paused"] and ts()["available_seconds"] <= 62
        results.append("real-channel duration boundary rolls and remains paused")
        await press(ecodes.KEY_INFO)
        await wait(lambda: not ts()["paused"] and not ts()["enabled"] and not ts()["usb_writing"])
        assert config.runtime_dir / ("rolling-" + urlsplit(await ipc.get("path")).path.split("/")[1] + ".json") == buffer
        results.append("return to live after boundary stops USB and retains only RAM prebuffer")
        print(json.dumps({"channel": channel, "passed": results, "status": ts()}, indent=2))
    finally:
        READY.write_bytes(original)
        READY.chmod(0o644)
        try:
            if ts()["paused"]:
                await press(ecodes.KEY_INFO)
        finally:
            ui.close()
            await ipc.close()


if __name__ == "__main__":
    asyncio.run(main())
