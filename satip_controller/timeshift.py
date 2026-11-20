"""USB-backed rolling time shifting and explicit playback-boundary policy."""
import asyncio
import math
import logging
import os
from pathlib import Path
import time

from .state import boot_id, read_json
from .mpv_ipc import IPCError

READY = Path("/run/satip-timeshift/storage.json")
DIRECTORY = Path("/media/satip-timeshift/buffer")
RESERVE = 1024**3


def storage():
    try:
        info = READY.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            return None
        data = read_json(READY)
        if (data.get("version") != 1 or data.get("boot_id") != boot_id()
                or data.get("directory") != str(DIRECTORY)
                or type(data.get("minutes")) is not int or not 0 <= data["minutes"] <= 120
                or type(data.get("max_bytes")) is not int or not 1024**3 <= data["max_bytes"] < 2**63):
            return None
        # A stale marker cannot authorize writing to an ordinary root directory.
        device = None
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            fields = line.split()
            if fields[4] == str(DIRECTORY.parent):
                index = fields.index("-")
                if fields[index + 1] == "ext4" and "rw" in fields[5].split(","):
                    device = fields[2]
        sys = Path("/sys/dev/block") / str(device)
        if not sys.exists() or "/usb" not in str(sys.resolve()):
            return None
        if device != data.get("device") or DIRECTORY.is_symlink():
            return None
        value = DIRECTORY.stat()
        if f"{os.major(value.st_dev)}:{os.minor(value.st_dev)}" != device:
            return None
        space = os.statvfs(DIRECTORY)
        if space.f_bavail * space.f_frsize < RESERVE:
            return None
        return data
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def ranges(value):
    result = []
    if not isinstance(value, dict):
        return result
    for item in value.get("seekable-ranges", []):
        if not isinstance(item, dict):
            continue
        start, end = item.get("start"), item.get("end")
        if (type(start) in (int, float) and type(end) in (int, float)
                and math.isfinite(start) and math.isfinite(end) and end > start):
            result.append((start, end))
    return result


class Timeshift:
    def __init__(self):
        self.buffer = None
        self.worker = None
        self.active = False
        self.config = None
        self.paused = False
        self.position = None
        self.ranges = []
        self.bytes = 0
        self.seek_until = 0
        self.failed_uuid = None
        self.notice = ""
        self.warning = ""
        self.expiry_seconds = None
        self.reload = None
        self.boundary_notified = False
        self.ready_identity = None
        self.retry_after = None
        self.retry_delay = 30
        self.resume_position = None
        self.healthy_since = None
        self.upstream = None
        self.raw_failure_since = None

    def capture_failed(self, config):
        self.healthy_since = None
        if config:
            self.failed_uuid = config["uuid"]
            self.retry_after = time.monotonic() + self.retry_delay if self.retry_delay < 180 else None
            self.retry_delay = min(180, self.retry_delay * 2)

    def options(self):
        # Disk retention is owned by the segment writer, never mpv's append-only
        # cache. Keep live read-ahead and packet metadata bounded independently.
        return {"cache-on-disk": "no", "demuxer-max-bytes": "64MiB",
                "demuxer-max-back-bytes": "32MiB", "demuxer-seekable-cache": "yes",
                "rebase-start-time": "no"}

    def clear(self):
        self.active = False
        self.config = None
        self.paused = False
        self.position = None
        self.resume_position = None
        self.ranges = []
        self.bytes = 0
        self.seek_until = 0
        self.warning = ""
        self.expiry_seconds = None
        self.boundary_notified = False

    async def close(self):
        if self.buffer:
            await self.buffer.close()
        self.buffer = None
        self.clear()

    async def shutdown(self):
        await self.close()
        if self.worker:
            await self.worker.close()
            self.worker = None

    async def prepare(self, upstream, header, runtime, reload):
        from .rolling import RollingBuffer
        await self.close()
        self.reload = reload
        config = storage()
        if (self.upstream is not None and upstream != self.upstream
                or config and self.failed_uuid is not None and self.failed_uuid != config["uuid"]):
            self.failed_uuid = None
            self.retry_after = None
            self.retry_delay = 30
        self.upstream = upstream
        self.healthy_since = None
        self.raw_failure_since = None
        self.ready_identity = (config["boot_id"], config["uuid"], config["device"]) if config else None
        if not config:
            return upstream
        if (self.failed_uuid == config["uuid"] and (
                self.retry_after is not None and time.monotonic() < self.retry_after
                or self.retry_after is None and self.retry_delay == 180)):
            # Recover direct playback first; do not immediately reopen the
            # prebuffer path that just starved or lost its capture input.
            return upstream
        try:
            from .switching import SwitchExperiments
            if SwitchExperiments.load(runtime).persistent_relay:
                if self.worker is None:
                    from .relay_worker import RelayWorker
                    self.worker = RelayWorker()
                self.buffer = RollingBuffer(worker=self.worker)
            else:
                if self.worker:
                    await self.worker.close()
                    self.worker = None
                self.buffer = RollingBuffer()
            if not await self.buffer.start(upstream, header, runtime):
                await self.close()
                return upstream
            self.config = self.buffer.config
            self.healthy_since = time.monotonic()
            return self.buffer.live()
        except (OSError, ValueError, ImportError):
            self.capture_failed(config)
            self.notice = "Time-shift prebuffer unavailable – returning to live TV"
            await self.close()
            return upstream
        except BaseException:
            await self.close()
            raise

    def status(self):
        delay = max(0, self.ranges[-1][1] - self.position) if self.ranges and self.position is not None and self.active else 0
        ram = self.buffer.ram_segments if self.buffer else []
        return {"enabled": self.active, "available": bool(self.config and self.buffer), "paused": self.paused,
            "mode": "usb" if self.active else "ram" if self.buffer else "off",
            "usb_writing": bool(self.buffer and self.buffer.disk_process and self.buffer.disk_process.returncode is None),
            "ram_bytes": sum(s["bytes"] for s in ram),
            "ram_prebuffer_seconds": round(ram[-1]["end"] - ram[0]["start"], 1) if ram else 0,
            "behind_live_seconds": round(delay, 1),
            "available_seconds": round(sum(b - a for a, b in self.ranges), 1) if self.active else 0,
            "timeline_seconds": round(self.ranges[-1][1] - self.ranges[0][0], 1) if self.ranges and self.active else 0,
            "bytes": self.bytes if self.active else 0, "notice": self.notice, "warning": self.warning,
            "position_expires_seconds": self.expiry_seconds, "rolling": bool(self.active and self.buffer)}

    async def update(self, ipc, pid):
        if not self.buffer:
            current = storage()
            identity = (current["boot_id"], current["uuid"], current["device"]) if current else None
            if identity and (identity != self.ready_identity or (self.retry_after is not None
                    and time.monotonic() >= self.retry_after)):
                if identity != self.ready_identity:
                    self.failed_uuid = None
                    self.retry_after = None
                    self.retry_delay = 30
                self.ready_identity = identity
                self.notice = "USB storage available – enabling time shift"
                return "ready"
            if not identity:
                self.ready_identity = None
                self.failed_uuid = None
                self.retry_after = None
                self.retry_delay = 30
            return None
        current = storage()
        try:
            self.buffer.refresh()
        except (OSError, ValueError):
            self.capture_failed(current)
            self.notice = "Time-shift prebuffer unavailable – returning to live TV"
            return "storage"
        if not self.active and self.buffer.raw_error and not self.buffer.error:
            # The raw pipe can close before the independent capture supervisor
            # publishes its cause. Give that record one bounded health tick,
            # rather than reporting a generic relay error and discarding it.
            now = time.monotonic()
            if self.raw_failure_since is None:
                self.raw_failure_since = now
            if now - self.raw_failure_since < 1:
                return None
        else:
            self.raw_failure_since = None
        if self.buffer.error or (not self.active and self.buffer.raw_error):
            self.notice = self.buffer.error or "Live TV relay unavailable"
            self.capture_failed(current)
            if self.buffer.capture_failure:
                logging.getLogger(__name__).warning("Time-shift capture failed: %s", self.buffer.capture_failure)
            return "storage"
        # Starting a process is not recovery: only sustained fresh input resets
        # the automatic retry budget. Short-lived generations retain backoff.
        health = self.buffer.capture_health
        observed = self.buffer.capture_health_at
        snapshot_age = time.monotonic() - observed if type(observed) in (int, float) else None
        fresh = (snapshot_age is not None and 0 <= snapshot_age < 5
                 and isinstance(health, dict)
                 and all(type(health.get(key)) in (int, float)
                         and 0 <= health[key] + snapshot_age < 5
                         for key in ("upstream_age", "capture_write_age")))
        if fresh:
            if self.healthy_since is None:
                self.healthy_since = time.monotonic()
            if time.monotonic() - self.healthy_since >= 120:
                self.failed_uuid = None
                self.retry_after = None
                self.retry_delay = 30
        else:
            self.healthy_since = None
        if self.active and (not current or self.buffer.disk_error
                or current["uuid"] != self.config["uuid"] or current["device"] != self.config["device"]):
            self.failed_uuid = self.config["uuid"]
            self.notice = (self.buffer.disk_error if current and self.buffer.disk_error else
                           "USB time-shift storage unavailable – returning to live TV")
            if self.buffer.disk_error:
                logging.getLogger(__name__).warning("Time-shift USB copier failed: %s", self.buffer.disk_failure)
            return "storage"
        # Missing USB while watching live does not interrupt the RAM stream.
        self.config = current
        await self.sample(ipc)
        if not self.active:
            self.warning = ""
            self.expiry_seconds = None
            if not self.buffer.live_reader and self.position is not None and self.ranges and self.position < self.ranges[0][0]:
                await self.jump(max(self.ranges[0][0], self.ranges[-1][1] - 1), ipc)
            return None
        if self.position is not None and self.ranges and self.position < self.ranges[0][0]:
            await self.jump(self.ranges[0][0], ipc)
            self.resume_position = None
            if not self.boundary_notified:
                self.notice = "Oldest buffered content reached – viewing position moved forward"
                self.boundary_notified = True
        self.warning = ""
        self.expiry_seconds = None
        segments = self.buffer.segments
        if self.position is not None and len(segments) > 1:
            duration = segments[-1]["end"] - segments[0]["start"]
            rate = self.bytes / duration if duration else 0
            capacity = max(0, (self.config["max_bytes"] - 32 * 1024**2) / rate) if rate else math.inf
            if self.config["minutes"]:
                capacity = min(capacity, self.config["minutes"] * 60)
            capacity = min(capacity, 4096 * duration / len(segments))
            delay = segments[-1]["end"] - self.position
            remaining = max(0, capacity - delay)
            if math.isfinite(remaining):
                self.expiry_seconds = round(remaining)
            # Forecast only while paused: playing normally does not approach
            # the boundary unless the window shrinks or the source changes rate.
            if self.paused and remaining <= 60:
                self.warning = "Paused position will expire soon – resume or return to live"
        return None

    async def sample(self, ipc):
        self.paused = await ipc.get("pause") is True
        try:
            position = await ipc.get("time-pos")
        except IPCError:
            if ipc.closed:
                raise
            position = None
        self.position = position if type(position) in (int, float) and math.isfinite(position) else None
        if self.buffer:
            self.buffer.refresh()
            if self.position is not None:
                if self.buffer.calibrate(self.position) is False:
                    self.position = None
                else:
                    self.position += self.buffer.origin
            segments = self.buffer.segments
            self.ranges = [(segments[0]["start"], segments[-1]["end"])] if segments else []
            self.bytes = sum(s["bytes"] for s in self.buffer.disk_segments) if self.active else 0
        else:
            self.ranges = ranges(await ipc.get("demuxer-cache-state"))

    async def jump(self, target, ipc, precise=False):
        started = time.monotonic()
        if self.buffer and not self.buffer.live_reader:
            cached = ranges(await ipc.get("demuxer-cache-state"))
            local = target - self.buffer.origin
            if any(a <= local <= b for a, b in cached):
                # Most remote scrubbing stays in this reader's packet cache.
                # Do not restart demuxing/audio/the Pi decoder for every press.
                await ipc.command("seek", local, "absolute+exact" if precise else "absolute+keyframes")
                await self.wait_seek(ipc)
                await self.sample(ipc)
                self.seek_until = time.monotonic() + 8
                logging.getLogger(__name__).info("Time-shift seek phases: cache=hit total=%.3fs",
                                                 time.monotonic() - started)
                return
        if self.buffer:
            # A little history before an exact target makes the next backward
            # press cache-seekable too. This remains bounded RAM, not disk cache.
            path, _ = self.buffer.playback(target - 15 if precise else target)
            ipc.begin_load()
            await self.buffer.disconnect()
            # The relay is continuous/non-seekable. Start at this keyframe,
            # rather than requesting an unsupported demuxer startup seek.
            await self.reload(path, 0, self.paused)
            reloaded = time.monotonic()
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                try:
                    if await ipc.get("path") == path:
                        position = await ipc.get("time-pos")
                        if type(position) in (int, float):
                            if self.buffer.calibrate(position) is not False:
                                break
                except IPCError:
                    if ipc.closed:
                        raise
                await asyncio.sleep(.05)
            if precise:
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    cached = ranges(await ipc.get("demuxer-cache-state"))
                    if any(a <= target - self.buffer.origin <= b for a, b in cached):
                        await ipc.command("seek", target - self.buffer.origin, "absolute+exact")
                        await asyncio.sleep(.05)
                        while time.monotonic() < deadline and await ipc.get("seeking") is True:
                            await asyncio.sleep(.05)
                        break
                    await asyncio.sleep(.05)
                else:
                    raise IPCError("Time-shift position is not yet seekable", reason="timeshift_seek_unavailable")
            await self.sample(ipc)
            target = self.position if self.position is not None else target
            logging.getLogger(__name__).info(
                "Time-shift seek phases: cache=miss reader=%.3fs target=%.3fs total=%.3fs",
                reloaded - started, time.monotonic() - reloaded, time.monotonic() - started)
        else:
            await ipc.command("seek", target, "absolute+exact" if precise else "absolute+keyframes")
        self.position = target
        self.seek_until = time.monotonic() + 8

    async def wait_seek(self, ipc):
        deadline = time.monotonic() + 3
        await asyncio.sleep(.05)
        while time.monotonic() < deadline and await ipc.get("seeking") is True:
            await asyncio.sleep(.05)

    async def action(self, ipc, action, target=None):
        if action == "live" and not self.buffer and not self.active:
            return "Live"
        if not self.config and action != "live":
            if storage() and not self.buffer:
                self.failed_uuid = None
                self.retry_delay = 30
                self.retry_after = time.monotonic()
                return "Preparing time shift – press Pause again when TV resumes"
            return "Time shift unavailable – connect and configure USB storage"
        if not self.active and action not in ("pause", "live"):
            return "Time shift inactive – press the time-shift button first"
        await self.sample(ipc)
        self.warning = ""
        self.notice = ""
        self.expiry_seconds = None
        self.boundary_notified = False
        if action == "pause":
            if not self.active and self.buffer:
                # Freeze the existing picture first. Promotion copies completed
                # RAM segments; neither mpv nor the tuner is reloaded.
                current = storage()
                if not current or self.position is None:
                    return "Time shift unavailable – connect and configure USB storage"
                self.config = current
                await ipc.command("set_property", "pause", True)
                self.paused = True
                self.buffer.live_paused = True
                self.resume_position = self.position
                try:
                    await self.buffer.activate(self.position)
                    self.active = True
                except (OSError, ValueError):
                    self.active = False
                    self.paused = False
                    self.buffer.live_paused = False
                    self.resume_position = None
                    await ipc.command("set_property", "pause", False)
                    self.notice = "USB time shift could not start – live TV continues"
                    return self.notice
                except BaseException:
                    self.paused = False
                    self.buffer.live_paused = False
                    self.resume_position = None
                    await ipc.command("set_property", "pause", False)
                    raise
                await self.sample(ipc)
            else:
                if self.paused and self.buffer and (self.buffer.live_reader or self.resume_position is not None):
                    target = self.resume_position if self.resume_position is not None else self.position
                    try:
                        await self.jump(target, ipc, precise=True)
                    except IPCError as error:
                        if error.reason != "timeshift_seek_unavailable":
                            raise
                        return "Building time-shift buffer – no seek range available yet"
                    self.resume_position = None
                self.active = True
                self.paused = not self.paused
                await ipc.command("set_property", "pause", self.paused)
        else:
            if action == "live" and self.buffer:
                ipc.begin_load()
                await self.buffer.disconnect()
                await self.buffer.deactivate()
                self.active = False
                self.paused = False
                await ipc.command("set_property", "pause", False)
                self.resume_position = None
                path = self.buffer.live()
                await self.reload(path, 0, False)
                self.seek_until = time.monotonic() + 8
                return "Live"
            if self.position is None or not self.ranges:
                return "Building time-shift buffer – no seek range available yet"
            a, b = self.ranges[0][0], self.ranges[-1][1]
            delta = {"seek_back": -30, "seek_forward": 30,
                     "skip_back": -10, "skip_forward": 10}.get(action, 0)
            target = max(a, min(b - .5, self.position + delta if target is None else target))
            if action == "live":
                target = max(a, b - 1)
                self.paused = False
                self.active = False
                await ipc.command("set_property", "pause", False)
            self.resume_position = None
            precise = action in ("skip_back", "skip_forward")
            await self.jump(target, ipc, precise)
            if precise and not self.buffer:
                deadline = time.monotonic() + 3
                await asyncio.sleep(.05)
                while time.monotonic() < deadline:
                    try:
                        position = await ipc.get("time-pos")
                        if type(position) in (int, float) and await ipc.get("seeking") is not True:
                            break
                    except IPCError:
                        if ipc.closed:
                            raise
                    await asyncio.sleep(.05)
        return "TV paused" if self.paused else "Live TV / time-shift playback"
