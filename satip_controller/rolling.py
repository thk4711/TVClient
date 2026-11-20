"""Bounded, stream-copy TS capture and private continuous HTTP playback.

The capture supervisor is a separate process: retention continues if the UI
freezes, and a parent pipe closes it if the controller dies. No SD fallback.
"""
import asyncio
import csv
import contextlib
import json
import logging
import math
import os
from pathlib import Path
import re
import resource
import queue
import threading
import socket
from collections import deque
import secrets
import select
import signal
import subprocess
import sys
import time

from .state import atomic_json, read_json
from .timeshift import DIRECTORY, RESERVE, storage

LOG = logging.getLogger(__name__)
SEGMENT_BYTES = 32 * 1024**2
MAX_SEGMENTS = 4096
RAM_BYTES = 64 * 1024**2
RAM_SECONDS = 20
RAM_RESERVE = 64 * 1024**2
RAM_SEGMENTS = 32
RAW_BYTES = 6 * 1024**2
RAW_QUEUE = 64
RAW_SECONDS = 2
PTS_WRAP = 2**33 / 90000
NAME = re.compile(r"segment-[0-9]{9}\.ts")
SESSION = re.compile(r"rolling-[0-9a-f]{32}")


def cleanup(directory):
    """Remove only our flat, owned session files; never follow links."""
    if (not SESSION.fullmatch(directory.name) or directory.is_symlink()
            or directory.stat().st_uid != os.getuid()):
        return
    if (directory / "owner.json").is_symlink():
        return
    try:
        owner = read_json(directory / "owner.json")
        command = (Path("/proc") / str(owner["pid"]) / "cmdline").read_bytes().split(b"\0")
        if (b"satip_controller.rolling" in command and str(directory).encode() in command):
            return  # A different live capture (including a validation probe).
        if (b"satip_controller.relay_worker" in command
                and owner.get("start_ticks") == process_start(owner["pid"])):
            return  # A forked generation inherits its supervisor argv.
    except (OSError, ValueError, KeyError, TypeError):
        pass
    for path in directory.iterdir():
        if (NAME.fullmatch(path.name.removesuffix(".part")) or path.name in ("segments.csv", "owner.json")) and not path.is_symlink():
            if path.is_file() and path.stat().st_uid == os.getuid():
                path.unlink(missing_ok=True)
    try:
        directory.rmdir()
    except OSError:
        pass


def process_start(pid):
    # comm may contain spaces/parentheses; field 22 follows the final ")".
    return (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19]


def retained(segments, budget, minutes, free):
    """Whole-segment window. Leave room for the in-progress bounded file."""
    items = list(segments)
    total = sum(s["bytes"] for s in items)
    while len(items) > 1 and (total + SEGMENT_BYTES > budget
            or free < RESERVE + SEGMENT_BYTES
            or len(items) > MAX_SEGMENTS
            or (minutes and items[-1]["end"] - items[0]["start"] > minutes * 60)):
        old = items.pop(0)
        total -= old["bytes"]
        free += old["bytes"]
    if total + SEGMENT_BYTES > budget or free < RESERVE + SEGMENT_BYTES:
        raise OSError("Insufficient USB buffer space")
    return items



def ram_storage(directory):
    """Require real tmpfs; /run is not permission to fall back to the SD card."""
    directory = directory.absolute()
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise OSError("Linked RAM directory refused")
    directory = directory.resolve(strict=True)
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        mount = Path(fields[4])
        if directory == mount or mount in directory.parents:
            mounts.append((len(mount.parts), fields[fields.index("-") + 1]))
    if not mounts or max(mounts)[1] != "tmpfs":
        raise OSError("Time-shift prebuffer requires tmpfs")
    space = os.statvfs(directory)
    if space.f_bavail * space.f_frsize < RAM_RESERVE:
        raise OSError("Insufficient RAM prebuffer space")


def retained_ram(segments):
    items = list(segments)
    total = sum(s["bytes"] for s in items)
    while len(items) > 1 and (total + SEGMENT_BYTES > RAM_BYTES
            or len(items) > RAM_SEGMENTS or items[-1]["end"] - items[0]["start"] > RAM_SECONDS):
        total -= items.pop(0)["bytes"]
    if total + SEGMENT_BYTES > RAM_BYTES:
        raise OSError("RAM segment exceeded the prebuffer budget")
    return items

def first_video_pts(path, near):
    """Recover the first list entry's real PTS (FFmpeg lists it as zero)."""
    with path.open("rb") as source:
        while data := source.read(188 * 256):
            for start in range(0, len(data) - 187, 188):
                packet = data[start:start + 188]
                if packet[0] != 0x47 or not packet[1] & 0x40:
                    continue
                control = (packet[3] >> 4) & 3
                if control not in (1, 3):
                    continue
                offset = 4 + (1 + packet[4] if control == 3 else 0)
                p = packet[offset:]
                if len(p) < 14 or p[:3] != b"\x00\x00\x01" or not 0xe0 <= p[3] <= 0xef or not p[7] & 0x80:
                    continue
                q = p[9:14]
                pts = (((q[0] >> 1) & 7) << 30) | (q[1] << 22) | ((q[2] >> 1) << 15) | (q[3] << 7) | (q[4] >> 1)
                value = pts / 90000
                return value + round((near - value) / PTS_WRAP) * PTS_WRAP
    raise OSError("No video timestamp in the first RAM segment")


class CaptureFeed:
    """Bounded best-effort muxer input; a blocked pipe never blocks live TV."""
    def __init__(self, source):
        self.source = source
        self.pending = queue.Queue(maxsize=RAW_QUEUE)
        self.failed = threading.Event()
        self.closed = threading.Event()
        self.last_write = time.monotonic()

    def offer(self, data):
        if self.failed.is_set() or self.closed.is_set():
            return False
        try:
            self.pending.put_nowait(data)
            return True
        except queue.Full:
            # Never discard TS bytes and continue a corrupted recording.
            self.failed.set()
            return False

    def run(self):
        try:
            while not self.closed.is_set():
                try:
                    data = self.pending.get(timeout=.25)
                except queue.Empty:
                    continue
                self.source.write(data)
                self.source.flush()
                self.last_write = time.monotonic()
        except (OSError, ValueError):
            self.failed.set()
        finally:
            with contextlib.suppress(OSError, ValueError):
                self.source.close()


def capture(directory, index, parent_fd, raw_fd, upstream, header, released_fd=None):
    """Child entry point. Secrets are read on stdin, not exposed in argv."""
    ram_storage(directory)
    if directory.parent != index.parent or not SESSION.fullmatch(directory.name):
        raise OSError("Invalid RAM capture directory")
    config = None
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Each individual segment is bounded even if FFmpeg never finds a keyframe.
    resource.setrlimit(resource.RLIMIT_FSIZE, (SEGMENT_BYTES, SEGMENT_BYTES))
    # Feed HTTP ourselves, keeping backend credentials out of child argv.
    process = subprocess.Popen([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts", "-copyts", "-f", "mpegts", "-probesize", "5242880",
        "-analyzeduration", "5000000", "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a?",
        "-c", "copy", "-f", "segment", "-segment_time", "2",
        "-segment_list", str(directory / "segments.csv"),
        "-segment_list_type", "csv", "-segment_list_size", str(RAM_SEGMENTS),
        "-segment_list_flags", "+live", "-segment_format", "mpegts",
        "-segment_format_options", "mpegts_copyts=1",
        str(directory / "segment-%09d.ts")],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # A bounded HTTP pump thread is deliberately daemonized. Termination closes
    # FFmpeg's stdin; a blocked tuner read cannot keep this process alive.
    import urllib.request
    feed = CaptureFeed(process.stdin)
    threading.Thread(target=feed.run, daemon=True).start()
    upstream_bytes = 0
    upstream_at = time.monotonic()
    os.set_blocking(raw_fd, False)
    upstream_socket = []
    def pump():
        nonlocal upstream_bytes, upstream_at
        pending = bytearray()
        raw_open = True
        try:
            request = urllib.request.Request(upstream, headers={"Authorization": header.removeprefix("Authorization: ")})
            with urllib.request.urlopen(request, timeout=10) as source:
                upstream_socket.append(source.fp.raw._sock)
                while running and process.poll() is None:
                    # read1 forwards the first available bytes; read(n) waits
                    # for a full block and delays sparse initial channel data.
                    data = source.read1(188 * 256)
                    if not data:
                        break
                    upstream_bytes += len(data)
                    upstream_at = time.monotonic()
                    # Deliver original TS before the background muxer. Never
                    # backpressure capture/USB retention on a frozen UI reader.
                    if raw_open:
                        pending.extend(data)
                        try:
                            while pending:
                                count = os.write(raw_fd, pending)
                                del pending[:count]
                        except BlockingIOError:
                            pass
                        except OSError:
                            raw_open = False
                        if not raw_open or len(pending) > 2 * 1024**2:
                            os.close(raw_fd)
                            raw_open = False
                            pending.clear()
                    # Do not let a blocked FFmpeg stdin starve the raw relay.
                    # Overflow fails capture explicitly; it never drops bytes
                    # and silently publishes corrupt time-shift content.
                    feed.offer(data)
        except (OSError, ValueError):
            pass
        finally:
            if raw_open:
                os.close(raw_fd)
            feed.closed.set()
    pump_thread = threading.Thread(target=pump, daemon=True)
    pump_thread.start()
    items = []
    last_end = -math.inf
    last_segment = time.monotonic()
    error = ""
    published = None
    health_published = 0
    try:
        while running:
            if select.select([parent_fd], [], [], .25)[0] and not os.read(parent_fd, 1):
                break
            ram_storage(directory)
            rows = []
            try:
                with (directory / "segments.csv").open() as source:
                    rows = list(csv.reader(source))[-RAM_SEGMENTS:]
            except FileNotFoundError:
                pass
            known = {s["name"] for s in items}
            for row in rows:
                if len(row) != 3:
                    continue  # May observe an unfinished CSV line.
                name = Path(row[0]).name
                if not NAME.fullmatch(name) or name in known:
                    continue
                try:
                    start, end = float(row[1]), float(row[2])
                    if name == "segment-000000000.ts":
                        start = first_video_pts(directory / name, end)
                    if not math.isfinite(start + end) or end <= start or end <= last_end:
                        continue
                    size = (directory / name).stat().st_size
                except (ValueError, FileNotFoundError):
                    continue
                items.append({"name": name, "start": start, "end": end, "bytes": size})
                known.add(name)
                last_end = end
                last_segment = time.monotonic()
            keep = retained_ram(items)
            names = {s["name"] for s in keep}
            for item in items:
                if item["name"] not in names:
                    (directory / item["name"]).unlink(missing_ok=True)
            items = keep
            # Unlinked files held by a slow client are accounted by statvfs;
            # the HTTP side closes each descriptor before waiting on a socket.
            now = time.monotonic()
            if feed.failed.is_set():
                error = "Time-shift capture input blocked – returning to live TV"
                raise OSError("Capture input blocked")
            if process.poll() is not None or now - last_segment > 15:
                raise OSError("Capture stopped")
            signature = tuple(s["name"] for s in items)
            if signature != published or now - health_published >= 1:
                atomic_json(index, {"segments": items, "config": config, "error": "",
                    "health": {"upstream_bytes": upstream_bytes,
                               "upstream_age": round(now - upstream_at, 3),
                               "capture_write_age": round(now - feed.last_write, 3),
                               "capture_queue_chunks": feed.pending.qsize()}}, 0o600, durable=False)
                published = signature
                health_published = now
    except (OSError, ValueError):
        error = error or "Time-shift prebuffer unavailable – returning to live TV"
    finally:
        running = False
        feed.closed.set()
        if released_fd is not None:
            # A connection setup may not have supplied a socket yet. Never
            # acknowledge release while its pump can still open a subscription.
            for upstream_sock in upstream_socket:
                with contextlib.suppress(OSError):
                    upstream_sock.shutdown(socket.SHUT_RDWR)
            pump_thread.join(.5)
            if not pump_thread.is_alive():
                os.write(released_fd, b"R")
            os.close(released_fd)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        process.wait()
        atomic_json(index, {"segments": items, "config": config, "error": error}, 0o600, durable=False)
        os.close(parent_fd)


def copy_usb(directory, ram_directory, ram_index, index, parent_fd, position):
    """Independent on-demand copier; slow USB cannot block RAM retention.

    No backend subscription and no FFmpeg restart. Only completed RAM segments
    are copied. The parent lifetime pipe stops this writer on Return to live.
    """
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    resource.setrlimit(resource.RLIMIT_FSIZE, (SEGMENT_BYTES, SEGMENT_BYTES))
    def alive(timeout=0):
        nonlocal running
        if select.select([parent_fd], [], [], timeout)[0] and not os.read(parent_fd, 1):
            running = False
        return running
    items = []
    config = None
    error = ""
    ready = False
    owned = False
    last_end = -1
    stage = "activation"
    failure = {}
    published = None
    ram_stamp = None
    ram_seen = time.monotonic()
    try:
        config = storage()
        if (not config or directory.parent != DIRECTORY or not SESSION.fullmatch(directory.name)
                or not SESSION.fullmatch(ram_directory.name) or ram_directory.parent != ram_index.parent
                or type(position) not in (int, float) or not math.isfinite(position)):
            raise OSError("USB activation unavailable")
        ram_storage(ram_directory)
        for old in DIRECTORY.iterdir():
            if SESSION.fullmatch(old.name) and old.is_dir() and not old.is_symlink():
                cleanup(old)
        directory.mkdir(mode=0o700)
        owned = True
        atomic_json(directory / "owner.json", {"pid": os.getpid()}, 0o600, durable=False)
        while alive(.25):
            stage = "storage"
            current = storage()
            if not current or current["uuid"] != config["uuid"] or current["device"] != config["device"]:
                raise OSError("USB unavailable")
            config = current
            stage = "ram_index"
            stamp = ram_index.stat().st_mtime_ns
            if stamp != ram_stamp:
                ram_stamp = stamp
                ram_seen = time.monotonic()
            if time.monotonic() - ram_seen > 15:
                raise OSError("RAM capture stalled")
            data = read_json(ram_index)
            segments = data.get("segments", [])
            if data.get("error") or not segments:
                raise OSError("RAM capture unavailable")
            if not ready and not segments[0]["start"] <= position <= segments[-1]["end"]:
                raise OSError("Paused position no longer retained in RAM")
            for item in segments:
                if item["end"] <= last_end:
                    continue
                name = item["name"]
                if not NAME.fullmatch(name) or not 0 < item["bytes"] <= SEGMENT_BYTES:
                    raise OSError("Invalid RAM segment")
                temporary = directory / (name + ".part")
                stage = "ram_segment"
                try:
                    source_fd = os.open(ram_directory / name, os.O_RDONLY | os.O_NOFOLLOW)
                except FileNotFoundError:
                    # An activation snapshot can contain expired history older
                    # than the requested pause point. It is not needed. Never
                    # skip a missing segment after capture has started, or one
                    # containing the pause point: that would hide a real gap.
                    if not ready and last_end < 0 and item["end"] < position:
                        continue
                    raise
                try:
                    stage = "usb_write"
                    dest_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    try:
                        copied = 0
                        while alive():
                            chunk = os.read(source_fd, 64 * 1024)
                            if not chunk:
                                break
                            offset = 0
                            while offset < len(chunk) and alive():
                                written = os.write(dest_fd, chunk[offset:])
                                if written <= 0:
                                    raise OSError("USB write failed")
                                offset += written
                            copied += offset
                    finally:
                        os.close(dest_fd)
                finally:
                    os.close(source_fd)
                if not running:
                    break
                if copied != item["bytes"]:
                    raise OSError("Incomplete USB segment")
                stage = "usb_publish"
                os.replace(temporary, directory / name)
                items.append(dict(item))
                last_end = item["end"]
                space = os.statvfs(directory)
                keep = retained(items, config["max_bytes"], config["minutes"], space.f_bavail * space.f_frsize)
                removed = [s for s in items if s["name"] not in {k["name"] for k in keep}]
                items = keep
                ready = any(s["start"] <= position <= s["end"] for s in items) if not ready else True
                # Publish readiness promptly, then batch later additions below.
                if published is None and ready:
                    atomic_json(index, {"segments": items, "config": config, "ready": ready, "error": ""}, 0o600, durable=False)
                    published = (tuple(s["name"] for s in items), dict(config), ready)
                for old in removed:
                    (directory / old["name"]).unlink(missing_ok=True)
            # Apply a changed budget even when no new segment completed.
            space = os.statvfs(directory)
            keep = retained(items, config["max_bytes"], config["minutes"], space.f_bavail * space.f_frsize)
            removed = [s for s in items if s["name"] not in {k["name"] for k in keep}]
            items = keep
            signature = (tuple(s["name"] for s in items), dict(config), ready)
            if signature != published:
                atomic_json(index, {"segments": items, "config": config, "ready": ready, "error": ""}, 0o600, durable=False)
                published = signature
            for old in removed:
                (directory / old["name"]).unlink(missing_ok=True)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if running:
            error = ("Time-shift capture fell behind – returning to live TV"
                     if stage in ("ram_index", "ram_segment") else
                     "USB time-shift storage unavailable – returning to live TV"
                     if stage == "storage" else
                     "USB time-shift storage error – returning to live TV")
            failure = {"stage": stage, "exception": type(exc).__name__,
                       "errno": exc.errno if isinstance(exc, OSError) else None,
                       "last_end": last_end, "segments": len(items),
                       "ram_age": round(time.monotonic() - ram_seen, 3)}
            # Keep one bounded, credential-free record across session cleanup.
            atomic_json(index.parent / "last-usb-failure.json", failure, 0o600, durable=False)
    finally:
        # Cleanup happens here, not on the controller's event loop. A blocked
        # USB syscall must not prevent the independent RAM writer from trimming.
        atomic_json(index, {"segments": [], "config": config, "ready": False, "error": error,
                            "failure": failure}, 0o600, durable=False)
        if owned and directory.exists() and not directory.is_symlink():
            # Our own pid is still live; remove only this process's known files.
            for path in directory.iterdir():
                if (NAME.fullmatch(path.name.removesuffix(".part")) or path.name == "owner.json") and not path.is_symlink():
                    if path.is_file() and path.stat().st_uid == os.getuid():
                        path.unlink(missing_ok=True)
            try:
                directory.rmdir()
            except OSError:
                pass
        os.close(parent_fd)


class RollingBuffer:
    def __init__(self, worker=None):
        self.worker = worker
        self.process = None
        self.parent_fd = None
        self.server = None
        self.clients = set()
        self.tasks = set()
        self.directory = None
        self.index = None
        self.segments = []
        self.config = None
        self.error = ""
        self.origin = 0
        self.path = None
        self.token = secrets.token_hex(16)
        self.index_stamp = None
        self.capture_health = {}
        self.raw_at = None
        self.raw_received = 0
        self.ram_segments = []
        self.disk_segments = []
        self.disk_process = None
        self.disk_parent_fd = None
        self.disk_directory = None
        self.disk_index = None
        self.disk_stamp = None
        self.disk_enabled = False
        self.disk_ready = False
        self.disk_error = ""
        self.disk_failure = {}
        self.raw_reader = None
        self.raw_writer = None
        self.raw_task = None
        self.raw_preroll = deque()
        self.raw_bytes = 0
        self.raw_error = ""
        self.live_clients = {}
        self.live_paused = False
        self.live_reader = False
        self.raw_overflow = False
        self.anchor = None
        self.position_offset = None
        self.closing = False
        self.close_task = None

    def refresh(self):
        try:
            stamp = self.index.stat().st_mtime_ns if self.index else None
            if stamp != self.index_stamp:
                data = read_json(self.index) if self.index else {}
                self.index_stamp = stamp
                self.ram_segments = data.get("segments", [])
                self.error = data.get("error", "")
                self.capture_health = data.get("health", {})
        except FileNotFoundError:
            self.ram_segments = []
        if self.process and self.process.returncode is not None:
            self.error = self.error or "Time-shift prebuffer unavailable – returning to live TV"
        if self.disk_enabled:
            try:
                stamp = self.disk_index.stat().st_mtime_ns
                if stamp != self.disk_stamp:
                    data = read_json(self.disk_index)
                    self.disk_stamp = stamp
                    self.disk_segments = data.get("segments", [])
                    self.disk_ready = data.get("ready") is True
                    self.disk_error = data.get("error", "")
                    self.disk_failure = data.get("failure", {})
                    self.config = data.get("config") or self.config
            except FileNotFoundError:
                pass
            if self.disk_process and self.disk_process.returncode is not None:
                self.disk_error = self.disk_error or "USB time-shift storage error – returning to live TV"
        items = {s["name"]: {**s, "location": "ram"} for s in self.ram_segments}
        if self.disk_enabled:
            items.update({s["name"]: {**s, "location": "usb"} for s in self.disk_segments})
        self.segments = sorted(items.values(), key=lambda s: s["start"])

    async def feed_raw(self):
        try:
            while data := await self.raw_reader.read(64 * 1024):
                now = time.monotonic()
                self.raw_at = now
                self.raw_received += len(data)
                self.raw_preroll.append((now, data))
                self.raw_bytes += len(data)
                while self.raw_preroll and (self.raw_bytes > RAW_BYTES or now - self.raw_preroll[0][0] > RAW_SECONDS):
                    self.raw_bytes -= len(self.raw_preroll.popleft()[1])
                for writer, queue in list(self.live_clients.items()):
                    if queue.full():
                        if not self.raw_overflow:
                            LOG.warning("Live TV relay reader queue overflow (paused=%s)", self.live_paused)
                        self.raw_overflow = True
                        if not self.live_paused:
                            writer.transport.abort()
                        continue
                    queue.put_nowait(data)
            if not self.closing:
                self.raw_error = "Live TV relay unavailable"
        except OSError:
            if not self.closing:
                self.raw_error = "Live TV relay unavailable"
        finally:
            for queue in self.live_clients.values():
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(None)

    def diagnostics(self):
        now = time.monotonic()
        health = {"relay_worker_pid": self.worker.process.pid if self.worker and self.worker.process else None,
                  "relay_capture_pid": self.process.pid if self.process else None,
                  "relay_bytes": self.raw_received,
                  "relay_age": round(now - self.raw_at, 3) if self.raw_at is not None else None,
                  "relay_overflow": self.raw_overflow,
                  "relay_queue_chunks": max((q.qsize() for q in self.live_clients.values()), default=0)}
        # Only fixed numeric fields, never paths, authentication or raw metadata.
        for name in ("upstream_bytes", "upstream_age", "capture_write_age", "capture_queue_chunks"):
            value = self.capture_health.get(name)
            health[name] = value if type(value) in (int, float) and math.isfinite(value) else None
        return health

    async def serve_live(self, writer):
        queue = asyncio.Queue(maxsize=RAW_QUEUE)
        initial = [data for _, data in self.raw_preroll]
        self.live_clients[writer] = queue
        try:
            for data in initial:
                writer.write(data)
                await writer.drain()
            while not self.raw_error:
                data = await queue.get()
                if data is None:
                    break
                writer.write(data)
                await writer.drain()
        finally:
            self.live_clients.pop(writer, None)

    def live(self):
        if self.raw_error:
            raise OSError("Live relay unavailable")
        self.refresh()
        self.anchor = self.ram_segments[-1]["end"] if self.ram_segments else None
        self.position_offset = None
        self.origin = 0
        self.live_reader = True
        self.live_paused = False
        self.raw_overflow = False
        port = self.server.sockets[0].getsockname()[1]
        self.path = f"http://127.0.0.1:{port}/{self.token}/live.ts"
        return self.path

    def calibrate(self, position):
        # Both paths preserve broadcast PTS. Correct only MPEG-TS wrap epochs;
        # do not add the segment start to mpv's absolute time-pos.
        if self.position_offset is None:
            anchor = self.anchor
            if anchor is None and self.ram_segments:
                anchor = self.ram_segments[-1]["end"]
            if anchor is not None:
                offset = round((anchor - position) / PTS_WRAP) * PTS_WRAP
                # mpv can briefly report zero during reader replacement. It
                # is not a PTS wrap: even after epoch correction it is nowhere
                # near the segment we opened. Wait for a real decoder position.
                if abs(anchor - position - offset) > 60:
                    return False
                self.position_offset = offset
                self.origin = self.position_offset
        return True

    async def activate(self, position):
        if self.disk_enabled and self.disk_ready:
            return
        if self.disk_process:
            await self.deactivate()
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            self.refresh()
            if self.error:
                raise OSError("RAM capture unavailable")
            if self.ram_segments and self.ram_segments[0]["start"] <= position <= self.ram_segments[-1]["end"]:
                break
            if self.ram_segments and position < self.ram_segments[0]["start"]:
                raise OSError("Pause position is outside the RAM prebuffer")
            await asyncio.sleep(.05)
        else:
            raise OSError("Building RAM prebuffer timed out")
        token = secrets.token_hex(16)
        self.disk_directory = DIRECTORY / ("rolling-" + token)
        self.disk_index = self.index.parent / ("usb-" + token + ".json")
        self.disk_stamp = None
        self.disk_error = ""
        self.disk_ready = False
        self.disk_segments = []
        read_fd, self.disk_parent_fd = os.pipe()
        try:
            self.disk_process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "satip_controller.rolling", "--usb",
                str(self.disk_directory), str(self.directory), str(self.index),
                str(self.disk_index), str(read_fd), pass_fds=(read_fd,),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            self.disk_enabled = True
            self.disk_process.stdin.write(json.dumps(position).encode())
            await self.disk_process.stdin.drain()
            self.disk_process.stdin.close()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                self.refresh()
                if self.disk_error:
                    raise OSError("USB capture unavailable")
                if self.disk_ready:
                    return
                await asyncio.sleep(.05)
            raise OSError("USB activation timed out")
        except BaseException:
            await self.deactivate()
            raise
        finally:
            os.close(read_fd)

    async def deactivate(self):
        self.disk_enabled = False
        self.disk_ready = False
        if self.disk_parent_fd is not None:
            os.close(self.disk_parent_fd)
            self.disk_parent_fd = None
        if self.disk_process:
            try:
                await asyncio.wait_for(self.disk_process.wait(), 3)
            except TimeoutError:
                if self.disk_process.returncode is None:
                    self.disk_process.kill()
                try:
                    await asyncio.wait_for(self.disk_process.wait(), 2)
                except TimeoutError:
                    raise OSError("USB writer has not stopped; storage is not safe to remove") from None
            self.disk_process = None
        if self.disk_index:
            self.disk_index.unlink(missing_ok=True)
            self.disk_index = None
        self.disk_segments = []
        self.disk_error = ""
        self.refresh()

    def read_segment(self, item):
        directories = [self.directory]
        if self.disk_enabled and self.disk_directory:
            directories.insert(0, self.disk_directory)
        for directory in directories:
            try:
                return (directory / item["name"]).read_bytes()
            except FileNotFoundError:
                continue
        raise FileNotFoundError("Retained segment expired")

    async def start(self, upstream, header, runtime):
        self.config = storage()
        if not self.config:
            return False
        ram_storage(runtime)
        for directory in runtime.iterdir():
            if SESSION.fullmatch(directory.name) and directory.is_dir() and not directory.is_symlink():
                cleanup(directory)
        for index in runtime.glob("rolling-*.json"):
            if (SESSION.fullmatch(index.stem) and not index.is_symlink()
                    and index.stat().st_uid == os.getuid()
                    and not (runtime / index.stem).exists()):
                index.unlink(missing_ok=True)
        self.directory = runtime / ("rolling-" + self.token)
        self.directory.mkdir(mode=0o700)
        self.index = runtime / ("rolling-" + self.token + ".json")
        read_fd, self.parent_fd = os.pipe()
        read_socket, write_socket = socket.socketpair()
        raw_fd = write_socket.detach()
        self.raw_reader, self.raw_writer = await asyncio.open_connection(sock=read_socket, limit=128 * 1024)
        self.raw_task = asyncio.create_task(self.feed_raw())
        os.set_inheritable(read_fd, True)
        try:
            # No tuner URL or credentials in argv/logs/status.
            if self.worker:
                self.process = await self.worker.spawn(self.directory, self.index,
                    upstream, header, read_fd, raw_fd)
                owner = {"pid": self.process.pid, "start_ticks": process_start(self.process.pid)}
            else:
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "satip_controller.rolling", str(self.directory),
                    str(self.index), str(read_fd), str(raw_fd), pass_fds=(read_fd, raw_fd),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
                owner = {"pid": self.process.pid}
                self.process.stdin.write(json.dumps([upstream, header]).encode())
                await self.process.stdin.drain()
                self.process.stdin.close()
            atomic_json(self.directory / "owner.json", owner, 0o600, durable=False)
        finally:
            os.close(read_fd)
            os.close(raw_fd)
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", 0, limit=8192)
        # Raw live data does not wait for FFmpeg probing or segment completion.
        return True

    def playback(self, target=None):
        self.refresh()
        if not self.segments:
            raise OSError("No retained content")
        target = self.segments[-1]["start"] if target is None else max(self.segments[0]["start"], min(target, self.segments[-1]["end"] - .1))
        # Broadcast segment timestamps can have small gaps. Start at the
        # preceding segment, never jump to the newest file for a gap target.
        segment = next((s for s in reversed(self.segments) if s["start"] <= target), self.segments[0])
        self.anchor = segment["start"]
        self.position_offset = None
        self.origin = 0
        self.live_reader = False
        port = self.server.sockets[0].getsockname()[1]
        self.path = f"http://127.0.0.1:{port}/{self.token}/{segment['name']}"
        return self.path, max(0, target - segment["start"])

    async def serve(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.clients.add(writer)
        try:
            async with asyncio.timeout(3):
                request = await reader.readline()
                while (line := await reader.readline()) not in (b"\r\n", b"\n", b""):
                    pass
            parts = request.decode("ascii").split()
            prefix = "/" + self.token + "/"
            if (len(parts) != 3 or parts[0] != "GET" or not parts[1].startswith(prefix)
                    or (parts[1][len(prefix):] != "live.ts" and not NAME.fullmatch(parts[1][len(prefix):])) or len(self.clients) > 2):
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            name = parts[1][len(prefix):]
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n")
            await writer.drain()
            if name == "live.ts":
                await self.serve_live(writer)
                return
            sequence = int(name[8:17])
            while self.process and self.process.returncode is None:
                self.refresh()
                if self.error:
                    break
                item = next((s for s in self.segments if int(s["name"][8:17]) == sequence), None)
                if item:
                    # Never retain an open unlinked segment over socket waits.
                    data = self.read_segment(item)
                    for offset in range(0, len(data), 64 * 1024):
                        writer.write(data[offset:offset + 64 * 1024])
                        # A paused player may intentionally stop reading for
                        # hours. Keep this bounded segment in RAM, never on an
                        # open disk descriptor; close() cancels the socket task.
                        await writer.drain()
                    sequence += 1
                elif self.segments and sequence < int(self.segments[0]["name"][8:17]):
                    break  # Controller will explicitly clamp an expired viewer.
                else:
                    await asyncio.sleep(.1)
        except (OSError, ValueError, TimeoutError, asyncio.LimitOverrunError):
            pass
        finally:
            # A paused socket can contain pending writes. Do not wait forever
            # for those bytes to drain when a seek/shutdown cancels the reader.
            writer.transport.abort()
            self.clients.discard(writer)
            self.tasks.discard(task)
            try:
                async with asyncio.timeout(1):
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                pass

    async def disconnect(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        if self.worker:
            from .lifecycle import complete
            if self.close_task is None:
                self.close_task = asyncio.create_task(self._close())
            await complete(self.close_task)
        else:
            await self._close()

    async def _close(self):
        LOG.debug("Stopping rolling capture")
        self.closing = True
        await self.deactivate()
        server, self.server = self.server, None
        if server:
            server.close()
        # Python 3.13+ wait_closed() also waits for active client transports.
        # Cancel/abort those first, otherwise channel changes deadlock here.
        await self.disconnect()
        if server:
            await server.wait_closed()
        LOG.debug("Rolling readers closed")
        if self.raw_task:
            self.raw_task.cancel()
            await asyncio.gather(self.raw_task, return_exceptions=True)
            self.raw_task = None
        if self.raw_writer:
            self.raw_writer.transport.abort()
            self.raw_writer = None
        if self.parent_fd is not None:
            os.close(self.parent_fd)
            self.parent_fd = None
        if self.process and self.worker:
            await self.process.release()
            LOG.debug("Rolling upstream released; retiring capture generation")
            self.worker.retire(self.process, self.directory, self.index)
            self.process = None
            return
        if self.process:
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
            self.process = None
        LOG.info("Time-shift buffer closed")
        if self.directory and self.directory.exists():
            cleanup(self.directory)
        if self.index:
            self.index.unlink(missing_ok=True)


if __name__ == "__main__":
    if sys.argv[1] == "--usb":
        position = json.loads(sys.stdin.buffer.read())
        copy_usb(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]),
                 Path(sys.argv[5]), int(sys.argv[6]), position)
    else:
        upstream, header = json.loads(sys.stdin.buffer.read())
        capture(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), upstream, header)
