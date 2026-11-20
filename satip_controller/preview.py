"""Exclusive, bounded HLS preview. Credentials never enter FFmpeg arguments/logs."""
import asyncio
import contextlib
import os
import re
import secrets
import shutil
import time
from pathlib import Path

import httpx

from .backend import BackendError
from .coordination import wait_quiet, process_identity
from .lifecycle import stop_task
from .config import credentials, library
from .state import atomic_json, read_json, runtime_path, lease_data


class PreviewError(Exception):
    pass


def ffmpeg_arguments(channel, directory):
    args = ["/usr/bin/ffmpeg", "-hide_banner", "-loglevel", "quiet", "-nostdin",
            "-threads", "2", "-i", "pipe:0", "-map", "0:v:0",
            "-map", f"0:i:{channel.audio_pid}", "-sn", "-dn"]
    if channel.video == "h264":
        args += ["-c:v", "copy"]
    else:
        args += ["-vf", "bwdif=mode=send_frame:parity=auto:deint=interlaced,scale=640:trunc(640/dar/2)*2,setsar=1",
                 "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25",
                 "-threads", "2", "-pix_fmt", "yuv420p", "-g", "50", "-sc_threshold", "0"]
    return args + ["-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000",
                   "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
                   "-hls_flags", "delete_segments+temp_file",
                   "-hls_segment_filename", str(directory / "segment-%06d.ts"),
                   str(directory / "index.m3u8")]


class Preview:
    idle_timeout = 20
    startup_timeout = 45

    def __init__(self, manager, credential_path=None, runtime_dir=None):
        self.manager = manager
        self.auth = credentials(Path(credential_path)) if credential_path else None
        if os.environ.get("SATIP_RUNTIME_ROOT"):
            runtime_dir = Path(os.environ["SATIP_RUNTIME_ROOT"]) / "satip-web"
        self.root = (Path(runtime_dir) if runtime_dir else manager.directory) / "preview"
        self.lease_path = runtime_path(manager.shared, "preview-lease.json")
        self.task = None
        self.token = None
        self.directory = None
        self.status = {"state": "idle", "enabled": self.auth is not None}
        self.touched = 0
        self.started = asyncio.Event()
        self.generation = None
        self.encoder_identity = None
        self.takeover_path = runtime_path(Path(manager.config["player_state_dir"]), "local-takeover.json")

    @property
    def active(self):
        return self.task is not None and not self.task.done()

    def lease(self, seconds):
        atomic_json(self.lease_path, lease_data(seconds, generation=self.generation, active=seconds > 0,
                    owners=[process_identity(os.getpid()), self.encoder_identity] if seconds > 0 else []),
                    0o640, durable=False)

    async def recover(self):
        # The service owner must finish exact child cleanup before restarting
        # (systemd cgroup on Debian; owned process generation on Buildroot). Only
        # delete this component's generated directory, never management data.
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(mode=0o700)
        self.lease(0)

    async def start(self, service):
        if self.auth is None:
            raise PreviewError("Preview streaming credentials are not configured")
        manager = self.manager
        if self.active or manager.lock.locked() or (manager.task and not manager.task.done()) or manager.journal.exists():
            raise PreviewError("Preview, search or channel edit already running; stop it first")
        channels, _ = library(read_json(manager.library_path))
        channel = next((c for c in channels.values() if c.service_uuid == service), None)
        if channel is None:
            raise ValueError("Save this channel to favourites before previewing it")
        await manager.lock.acquire()
        self.token = secrets.token_hex(16)
        # Public handover identity is independent of the private browser token.
        self.generation = secrets.token_hex(16)
        self.directory = self.root / self.token
        try:
            self.directory.mkdir(parents=True, mode=0o700)
            self.lease(15)
        except BaseException:
            manager.lock.release()
            raise
        self.touched = time.monotonic()
        self.status = {"state": "starting", "enabled": True, "channel_name": channel.name,
                       "channel_uuid": channel.uuid}
        self.started.clear()
        self.task = asyncio.create_task(self.run(channel))
        return {"session": self.token, "url": f"/api/preview/{self.token}/index.m3u8"}

    def touch(self, token):
        if not self.active or not self.token or not secrets.compare_digest(token, self.token):
            raise PreviewError("Preview session expired or stopped")
        self.touched = time.monotonic()

    def media(self, token, name):
        if not self.active or token != self.token:
            raise PreviewError("Preview session expired or stopped")
        if name != "index.m3u8" and not re.fullmatch(r"segment-[0-9]{6,}\.ts", name):
            raise ValueError("Invalid preview media name")
        path = self.directory / name
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError
        # Actual HLS consumption also keeps a background browser alive when
        # its JavaScript timers are throttled. Never count missing media.
        self.touch(token)
        return path

    async def watch_local(self):
        while True:
            try:
                if read_json(self.takeover_path).get("generation") == self.generation:
                    self.status.update(state="stopped",
                                       reason="Streaming stopped because the receiver was used locally.")
                    self.task.cancel()
                    return
            except (OSError, ValueError, AttributeError):
                pass
            await asyncio.sleep(.1)

    async def quiet(self):
        await wait_quiet(self.manager.backend,
                         runtime_path(Path(self.manager.config["player_state_dir"]), "status.json"),
                         lambda: self.lease(15))

    async def feed(self, process, channel):
        # Python owns the authenticated HTTP request. FFmpeg sees only a pipe;
        # no password/header in /proc, subprocess logs, playlist or browser.
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(10), auth=httpx.BasicAuth(**self.auth)) as client:
            address = f"http://127.0.0.1:9981/stream/channel/{channel.uuid}?profile=pass"
            async with client.stream("GET", address) as response:
                if response.status_code != 200:
                    raise PreviewError("Backend rejected preview stream")
                async for chunk in response.aiter_bytes(65536):
                    process.stdin.write(chunk)
                    await process.stdin.drain()
        raise PreviewError("Backend preview stream ended")

    async def run(self, channel):
        self.started.set()
        process = feeder = None
        watcher = asyncio.create_task(self.watch_local())
        started = time.monotonic()
        try:
            await asyncio.wait_for(self.quiet(), timeout=12)
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(*ffmpeg_arguments(channel, self.directory),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL))
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                process = await spawn
                raise
            self.encoder_identity = process_identity(process.pid)
            self.lease(15)
            feeder = asyncio.create_task(self.feed(process, channel))
            last_lease = 0
            while True:
                now = time.monotonic()
                if now - self.touched > self.idle_timeout:
                    self.status.update(state="stopped", reason="Preview timed out; TV resumes automatically")
                    break
                if process.returncode is not None:
                    raise PreviewError("Preview encoder stopped")
                if feeder.done():
                    await feeder
                    raise PreviewError("Preview stream ended")
                playlist = self.directory / "index.m3u8"
                if playlist.exists():
                    self.status["state"] = "ready"
                elif now - started > self.startup_timeout:
                    raise PreviewError("Preview did not become ready; check the channel and reception")
                if now - last_lease > 3:
                    self.lease(15)
                    last_lease = now
                await asyncio.sleep(.5)
        except asyncio.CancelledError:
            self.status["state"] = "stopped"
            raise
        except (PreviewError, BackendError, httpx.HTTPError, OSError, ValueError, TimeoutError):
            # Do not publish raw exceptions, URLs or authenticated request details.
            self.status.update(state="error", error="Cannot play preview. Check reception and try again.")
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            if feeder:
                feeder.cancel()
                await asyncio.gather(feeder, return_exceptions=True)
            if process:
                if process.stdin:
                    process.stdin.close()
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 3)
                    except TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                        await process.wait()
            try:
                if self.directory and self.directory.exists():
                    shutil.rmtree(self.directory)
            finally:
                try:
                    self.lease(0)
                    self.encoder_identity = None
                finally:
                    self.manager.lock.release()

    async def stop(self, token=None):
        if token is not None and (not self.token or not secrets.compare_digest(token, self.token)):
            raise PreviewError("Not the active preview session")
        if self.active:
            task = self.task
            await self.started.wait()
            await stop_task(task)
