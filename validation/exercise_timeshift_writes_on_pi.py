#!/usr/bin/env python3
"""Measure actual USB partition writes for live/pause/resume/Return to live.

Root/evdev/uinput required. Uses the installed controller and current TV channel.
Never formats, changes USB settings or erases unrelated USB files. Waits for final
journal flushing before steady-live measurements. Physical HDMI remains separate.
"""
import asyncio
import grp
import json
import os
from pathlib import Path

from evdev import UInput, ecodes
from satip_controller.config import Config
from satip_controller.mpv_ipc import MPVIPC
from satip_controller.state import read_json, runtime_path
from satip_controller.timeshift import READY


async def main():
    config = Config.load(Path("/etc/satip-player/config.json"))
    path = runtime_path(config.state_dir, "status.json")
    def status():
        return read_json(path)
    def ts():
        return status()["timeshift"]
    async with asyncio.timeout(70):
        while not (status().get("playing") and ts().get("available")):
            await asyncio.sleep(.2)
    marker = read_json(READY)
    counter = Path("/sys/dev/block") / marker["device"] / "stat"
    def written():
        assert read_json(READY)["device"] == marker["device"], "USB changed during test"
        return int(counter.read_text().split()[6]) * 512
    ui = UInput({ecodes.EV_KEY: [ecodes.KEY_INFO, ecodes.KEY_PLAYPAUSE]}, name="gpio_ir_recv", bustype=0)
    await asyncio.sleep(.5)
    os.chown(ui.device.path, -1, grp.getgrnam("satip-input").gr_gid)
    os.chmod(ui.device.path, 0o660)
    ipc = await MPVIPC.connect(config.runtime_dir / "mpv.sock")
    async def press(code):
        ui.write(ecodes.EV_KEY, code, 1); ui.syn()
        await asyncio.sleep(.05)
        ui.write(ecodes.EV_KEY, code, 0); ui.syn()
        await asyncio.sleep(.5)
    async def wait(predicate):
        async with asyncio.timeout(15):
            while not predicate():
                await asyncio.sleep(.2)
    results = []
    async def flush_usb():
        # Separate finite session/journal writeback from steady-state capture.
        # This is validation only, not an extra appliance dependency or live I/O.
        import ctypes
        def flush():
            fd = os.open("/media/satip-timeshift", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NOATIME)
            try:
                device = os.fstat(fd).st_dev
                assert f"{os.major(device)}:{os.minor(device)}" == marker["device"]
                libc = ctypes.CDLL(None, use_errno=True)
                libc.syncfs.argtypes = [ctypes.c_int]
                libc.syncfs.restype = ctypes.c_int
                if libc.syncfs(fd) != 0:
                    raise OSError(ctypes.get_errno(), "USB flush failed")
            finally:
                os.close(fd)
        await asyncio.to_thread(flush)
    async def measure(label, seconds, expect_writes):
        before = written()
        await asyncio.sleep(seconds)
        if expect_writes:
            await flush_usb()
        delta = written() - before
        print(json.dumps({"state": label, "seconds": seconds, "usb_bytes_written": delta}), flush=True)
        assert (delta > 0) if expect_writes else (delta == 0), (label, delta)
        results.append({"state": label, "seconds": seconds, "usb_bytes_written": delta})
    try:
        await asyncio.sleep(2)
        assert ts()["mode"] == "ram" and not ts()["enabled"] and not ts()["usb_writing"]
        producer = status()["owner_pid"]
        initial_path = await ipc.get("path")
        await flush_usb()
        await asyncio.sleep(2)
        await measure("live RAM prebuffer", 20, False)
        await press(ecodes.KEY_PLAYPAUSE)
        await wait(lambda: ts()["paused"] and ts()["enabled"] and ts()["usb_writing"])
        assert await ipc.get("path") == initial_path, "First Pause reloaded mpv"
        position = await ipc.get("time-pos")
        await measure("paused USB session", 10, True)
        assert abs(await ipc.get("time-pos") - position) < .2
        await press(ecodes.KEY_PLAYPAUSE)
        try:
            await wait(lambda: not ts()["paused"] and ts()["enabled"])
        except TimeoutError:
            print("resume diagnostic", await ipc.get("track-list"), await ipc.get("demuxer-cache-state"), flush=True)
            raise
        resumed = await ipc.get("time-pos")
        assert -.1 <= resumed - position <= 2, "Resume skipped or rewound the paused viewing point"
        await measure("resumed delayed USB session", 10, True)
        await press(ecodes.KEY_INFO)
        await wait(lambda: ts()["mode"] == "ram" and not ts()["usb_writing"] and not ts()["enabled"])
        assert not ts()["paused"] and status()["owner_pid"] == producer
        assert ts()["bytes"] == 0 and ts()["ram_bytes"] <= 64 * 1024**2
        await flush_usb()
        await asyncio.sleep(2)
        await measure("returned live RAM prebuffer", 20, False)
        print(json.dumps({"passed": results, "status": ts()}, indent=2))
    finally:
        try:
            if ts()["enabled"]:
                await press(ecodes.KEY_INFO)
        finally:
            ui.close()
            await ipc.close()


if __name__ == "__main__":
    asyncio.run(main())
