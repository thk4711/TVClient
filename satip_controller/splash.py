"""Matching startup artwork and a small animated progress strip in mpv."""
import asyncio
import contextlib
import logging
import math
import os
from pathlib import Path
import time

from .mpv_ipc import IPCError

LOG = logging.getLogger(__name__)
RENDERER = Path("/usr/bin/satip-splash")
# Player milestones match render.c; 100 is reserved for confirmed playback.
PLAYER_PROGRESS = {35: 80, 3: 86, 36: 94, 37: 100, 31: 86, 27: 86}
WAITING = {35, 3, 36, 31, 27}


def bar_bounds(width, height):
    """Same centred/scaled strip as artwork_bar_top/bottom (C lround)."""
    scale = min(width / 1920, height / 1080)
    top = (height - 1080 * scale) / 2
    return tuple(math.floor(top + y * scale + .5) for y in (650, 654))


class BootSplash:
    def __init__(self, player, enabled=None):
        self.player = player
        self.enabled = os.environ.get("SATIP_BOOT_SPLASH") == "1" if enabled is None else enabled
        self.shown = None
        self.language = "en"
        self.state, self.detail = 35, ""
        self.path = None
        self.bar_path = None
        self.dimensions = (1920, 1080)
        self.progress = self.target = 80
        self.animation = None
        self.lock = asyncio.Lock()

    async def show(self, state=3, detail=""):
        async with self.lock:
            await self._show(state, detail)

    async def set_language(self, language):
        async with self.lock:
            if language != self.language:
                self.language = language
                if self.shown:
                    await self._show(self.state, self.detail)

    async def _show(self, state, detail):
        if not self.enabled:
            return False
        ipc = self.player.ipc
        key = (state, detail, self.language)
        if not ipc or ipc.closed:
            return False
        if self.shown == (ipc, key):
            return True
        try:
            dimensions = await ipc.get("osd-dimensions")
            if isinstance(dimensions, dict):
                width, height = dimensions.get("w"), dimensions.get("h")
            else:
                width = height = None
            if type(width) is not int or type(height) is not int or not (0 < width <= 4096 and 0 < height <= 2160):
                width, height = map(int, self.player.config.drm_mode.split("@")[0].split("x"))
            self.dimensions = width, height
            if not await self._render(ipc, state, detail):
                return False
            # The previous strip must not cover a new warning or language frame.
            await ipc.command("overlay-remove", 8)
            self.shown = (ipc, key)
            self.state, self.detail = state, detail
            self.target = max(self.target, PLAYER_PROGRESS[state]) if state in PLAYER_PROGRESS else int(self.progress)
            LOG.info("Startup display state %s, target %s%% (%.3f s since Linux start)",
                     state, self.target, time.monotonic())
            if self.animation is None or self.animation.done():
                self.animation = asyncio.create_task(self._animate())
            return True
        except (OSError, ValueError, IPCError, asyncio.TimeoutError):
            LOG.warning("Startgrafik nicht verfügbar; Wiedergabe wird fortgesetzt")
            return False

    async def _render(self, ipc, state, detail, *, bar=False, tick=0):
        process = staging = None
        try:
            width, height = self.dimensions
            top, bottom = bar_bounds(width, height) if bar else (0, height)
            path = self.player.config.runtime_dir / ("startbalken.bgra" if bar else "startbild.bgra")
            staging = path.with_suffix(".next")
            fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as output:
                process = await asyncio.create_subprocess_exec(
                    str(RENDERER), "--bar" if bar else "--frame", str(width), str(height),
                    str(state), detail, self.language, str(int(self.progress)), str(tick),
                    stdin=asyncio.subprocess.DEVNULL, stdout=output, stderr=asyncio.subprocess.DEVNULL)
                async with asyncio.timeout(3):
                    result = await process.wait()
            if result or staging.stat().st_size != width * (bottom-top) * 4:
                raise OSError("Startgrafik konnte nicht erzeugt werden")
            if ipc is not self.player.ipc or ipc.closed:
                return False
            # Never truncate a file which mpv may still have mapped.
            os.replace(staging, path)
            if bar:
                self.bar_path = path
            else:
                self.path = path
            await ipc.command("overlay-add", 8 if bar else 7, 0, top, str(path), 0,
                              "bgra", width, bottom-top, width * 4)
            return True
        except (OSError, ValueError, IPCError, asyncio.TimeoutError):
            LOG.warning("Startgrafik nicht verfügbar; Wiedergabe wird fortgesetzt")
            return False
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            if staging:
                with contextlib.suppress(OSError):
                    staging.unlink(missing_ok=True)

    async def _animate(self):
        updated = time.monotonic()
        completed = None
        while True:
            await asyncio.sleep(.2)
            async with self.lock:
                if not self.shown:
                    return
                ipc, _ = self.shown
                if ipc is not self.player.ipc or ipc.closed:
                    await self._hide(False)
                    return
                now = time.monotonic()
                # Smooth only towards an observed milestone, never towards a timer.
                self.progress = min(self.target, self.progress + 40 * (now-updated))
                updated = now
                if not await self._render(ipc, self.state, self.detail, bar=True, tick=int(now*10) % 1000000):
                    if self.state == 37:
                        await self._hide(True)
                    return
                if self.state == 37 and self.progress == 100:
                    if completed is None:
                        completed = now
                        LOG.info("Startup display 100%% (%.3f s since Linux start)", now)
                    elif now-completed >= .35:
                        await self._hide(True)
                        return
                elif self.state != 37 and self.state not in WAITING:
                    return

    async def complete(self):
        """Called only after picture advancement and audio selection are confirmed.

        The animation finishes asynchronously so input and playback monitoring
        continue during the brief 100% frame.
        """
        async with self.lock:
            if not self.enabled:
                return
            if not self.shown or not await self._show(37, ""):
                await self._hide(True)
            self.enabled = False

    async def hide(self, finished=False):
        task, self.animation = self.animation, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self.lock:
            await self._hide(finished)

    async def _hide(self, finished):
        if finished:
            self.enabled = False
        if self.shown:
            ipc, _ = self.shown
            if ipc is self.player.ipc and not ipc.closed:
                for layer in (8, 7):
                    with contextlib.suppress(IPCError, OSError):
                        await ipc.command("overlay-remove", layer)
            if self.state == 37:
                LOG.info("Startup display removed (%.3f s since Linux start)", time.monotonic())
            self.shown = None
        for path in (self.path, self.bar_path):
            if path:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
        self.path = self.bar_path = None
