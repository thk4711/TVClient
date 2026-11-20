#!/usr/bin/env python3
"""Native synthetic capture faults; no tuner, player controls or USB writes.

Requires configured storage readiness, root and FFmpeg on the Pi. Uses a private
/run tmpfs directory and loopback input; leaves the production player untouched.
"""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

from satip_controller.rolling import RollingBuffer
from satip_controller.state import read_json
from satip_controller.timeshift import storage


async def exercise():
    if os.geteuid() != 0 or not storage():
        raise ValueError("Requires root and configured writable USB readiness")
    sample = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-t", "8",
        "-c:v", "mpeg2video", "-g", "25", "-b:v", "500k",
        "-output_ts_offset", "10000", "-f", "mpegts", "pipe:1"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20).stdout
    for fault in ("upstream_eof", "upstream_timeout", "muxer_exit"):
        release = asyncio.Event()
        tasks = set()
        async def serve(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\nConnection: close\r\n\r\n")
                writer.write(sample)
                await writer.drain()
                await release.wait()
            finally:
                writer.transport.abort()
                tasks.discard(task)
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        buffer = RollingBuffer()
        try:
            with tempfile.TemporaryDirectory(prefix="satip-capture-probe-", dir="/run") as temp:
                runtime = Path(temp)
                url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/test"
                assert await buffer.start(url, "Authorization: Basic synthetic", runtime)
                async with asyncio.timeout(8):
                    while not buffer.ram_segments:
                        buffer.refresh()
                        assert not buffer.error, buffer.error
                        await asyncio.sleep(.05)
                assert buffer.disk_process is None and not buffer.disk_enabled
                started = time.monotonic()
                if fault == "upstream_eof":
                    release.set()
                elif fault == "muxer_exit":
                    children = []
                    for proc in Path("/proc").iterdir():
                        if not proc.name.isdigit():
                            continue
                        try:
                            fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
                            command = (proc / "cmdline").read_bytes().split(b"\0")
                            if (int(fields[1]) == buffer.process.pid and command[0] == b"ffmpeg"
                                    and str(buffer.directory / "segment-%09d.ts").encode() in command):
                                children.append(int(proc.name))
                        except (OSError, ValueError, IndexError):
                            continue
                    assert len(children) == 1, "Cannot identify synthetic muxer"
                    fd = os.pidfd_open(children[0])
                    try:
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                    finally:
                        os.close(fd)
                # Timeout leaves only the synthetic HTTP source idle.
                record = runtime / "last-capture-failure.json"
                async with asyncio.timeout(18):
                    while not record.exists():
                        await asyncio.sleep(.05)
                data = read_json(record)
                assert data["reason"] == fault, data
                assert data["health"]["upstream_bytes"] > 0
                assert record.stat().st_mode & 0o777 == 0o600
                assert "synthetic" not in record.read_text() and url not in record.read_text()
                await buffer.process.wait()
                buffer.refresh()
                assert buffer.capture_health == data["health"]
                assert buffer.capture_failure == data
                notice = buffer.error
                await buffer.close()
                assert record.exists(), "Failure record lost in session cleanup"
                print(json.dumps({"fault": fault, "notice": notice,
                    "detected_seconds": round(time.monotonic() - started, 3),
                    "terminal_health_preserved": True, "usb_writes": False}), flush=True)
        finally:
            release.set()
            await buffer.close()
            server.close()
            for task in list(tasks):
                task.cancel()
            await asyncio.gather(*list(tasks), return_exceptions=True)
            await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(exercise())
