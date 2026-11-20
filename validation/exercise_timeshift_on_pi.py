#!/usr/bin/env python3
"""Synthetic rolling-buffer check on the Pi; real USB, headless mpv, no tuner.

Run as root with PYTHONPATH pointing at the application. Temporarily sets the
readiness marker to a one-minute retained window and restores it in finally.
Does not format storage, change persistent settings, or exercise physical HDMI.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from satip_controller.mpv_ipc import MPVIPC
from satip_controller.timeshift import READY, Timeshift, storage
from satip_controller.state import atomic_json


async def main():
    assert storage(), "Configure writable USB storage first"
    original = READY.read_bytes()
    config = json.loads(original)
    config["minutes"] = 1
    atomic_json(READY, config, 0o644, durable=False)
    source = subprocess.Popen([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-readrate", "4",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-c:v", "mpeg2video", "-g", "25", "-b:v", "1M", "-c:a", "mp2",
        "-output_ts_offset", "10000", "-f", "mpegts", "pipe:1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    upstream_tasks = set()
    async def serve(reader, writer):
        task = asyncio.current_task()
        upstream_tasks.add(task)
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\nConnection: close\r\n\r\n")
            await writer.drain()
            while True:
                data = await asyncio.to_thread(source.stdout.read, 188 * 256)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            upstream_tasks.discard(task)
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    timeshift = Timeshift()
    player = None
    ipc = None
    results = []
    try:
        with tempfile.TemporaryDirectory(prefix="satip-rolling-", dir="/run") as temp:
            runtime = Path(temp)
            sock = runtime / "mpv.sock"
            player = await asyncio.create_subprocess_exec("mpv", "--no-config", "--vo=null", "--ao=null",
                "--idle=yes", "--no-terminal", "--input-ipc-server=" + str(sock),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            while not sock.exists():
                await asyncio.sleep(.1)
            ipc = await MPVIPC.connect(sock)
            async def reload(path, start, paused):
                ipc.begin_load()
                await ipc.command("set_property", "pause", paused)
                await ipc.command("loadfile", path, "replace", -1,
                                  {**timeshift.options(), "start": "none"})
                await ipc.command("set_property", "pause", paused)
            url = "http://127.0.0.1:" + str(server.sockets[0].getsockname()[1]) + "/test.ts"
            path = await timeshift.prepare(url, "Authorization: Basic test", runtime, reload)
            assert timeshift.buffer and timeshift.config, "Capture did not start"
            await reload(path, 0, False)
            await asyncio.sleep(3)
            await timeshift.sample(ipc)
            print("initial", timeshift.status(), "raw", await ipc.get("time-pos"), flush=True)
            assert timeshift.position is not None
            assert not timeshift.active and timeshift.buffer.disk_process is None
            assert timeshift.status()["bytes"] == 0
            results.append("live prebuffer is RAM only")
            await timeshift.action(ipc, "pause")
            paused = timeshift.position
            await asyncio.sleep(1)
            await timeshift.sample(ipc)
            assert timeshift.paused and abs(timeshift.position - paused) < .2
            assert timeshift.active and timeshift.buffer.disk_process is not None
            results.append("pause preserves position and starts USB only on demand")
            await timeshift.action(ipc, "pause")
            resumed = await ipc.get("time-pos")
            print("resume", resumed, "expected", paused, "cache", await ipc.get("demuxer-cache-state"), flush=True)
            assert not timeshift.paused and abs(resumed - paused) < .3, "Resume lost the original pause point"
            results.append("raw-to-archive resume preserves absolute pause PTS")
            await timeshift.action(ipc, "pause")
            await timeshift.action(ipc, "skip_forward")
            await asyncio.sleep(1)
            await timeshift.sample(ipc)
            print("seek", timeshift.status(), "raw", await ipc.get("time-pos"), flush=True)
            assert timeshift.paused and timeshift.position > paused + 8
            results.append("paused seek displays target without resume")
            deadline = time.monotonic() + 25
            moved = False
            while time.monotonic() < deadline:
                assert await timeshift.update(ipc, player.pid) is None
                moved |= "Oldest" in timeshift.notice
                if moved:
                    break
                await asyncio.sleep(.5)
            print("boundary", timeshift.status(), flush=True)
            assert moved and timeshift.paused, "Paused expiry did not clamp and remain paused"
            # RAM completion and the independent USB copier publish separate
            # indexes. A fast cached expiry seek can observe a newly completed
            # RAM segment before USB trimming has caught up (e.g. 62 s).
            # Keep the strict 60-second assertion, but wait for convergence.
            deadline = time.monotonic() + 3
            while timeshift.status()["available_seconds"] > 60.1 and time.monotonic() < deadline:
                await asyncio.sleep(.05)
                assert await timeshift.update(ipc, player.pid) is None
                assert timeshift.paused
            assert timeshift.status()["available_seconds"] <= 60.1
            results.append("rolling expiry clamps and stays paused")
            await timeshift.action(ipc, "live")
            await asyncio.sleep(2)
            await timeshift.sample(ipc)
            print("live", timeshift.status(), flush=True)
            assert not timeshift.paused and not timeshift.active
            assert timeshift.buffer.disk_process is None
            assert not timeshift.buffer.disk_directory.exists()
            results.append("return to live stops USB and removes its history")
            assert timeshift.buffer.process.returncode is None
            print(json.dumps({"passed": results}, indent=2), flush=True)
            await timeshift.close()
            assert not list(runtime.glob("rolling-*.json"))
    finally:
        READY.write_bytes(original)
        os.chmod(READY, 0o644)
        await timeshift.close()
        if ipc:
            await ipc.close()
        if player and player.returncode is None:
            player.terminate()
            await player.wait()
        source.terminate()
        try:
            source.wait(timeout=3)
        except subprocess.TimeoutExpired:
            source.kill()
            source.wait()
        server.close()
        await server.wait_closed()
        for task in list(upstream_tasks):
            task.cancel()
        await asyncio.gather(*upstream_tasks, return_exceptions=True)
        READY.write_bytes(original)
        os.chmod(READY, 0o644)


if __name__ == "__main__":
    asyncio.run(main())
