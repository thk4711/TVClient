"""Atomic JSON storage; private player state is separate from shared management data."""
import json
import copy
import hashlib
import math
import subprocess
import sys
import time
from functools import lru_cache
from collections import OrderedDict

import os
import tempfile
from pathlib import Path


def atomic_json(path: Path, data: dict, mode: int = 0o600, *, durable=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            os.fchmod(out.fileno(), mode)
            json.dump(data, out, ensure_ascii=False, allow_nan=False)
            out.write("\n")
            out.flush()
            if durable:
                os.fsync(out.fileno())
        os.replace(name, path)
        if durable:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path: Path):
    with path.open() as src:
        return json.load(src)


def runtime_path(directory: Path, name: str):
    """Shared RAM paths when enabled by deployment; otherwise use configured paths."""
    root = os.environ.get("SATIP_RUNTIME_ROOT")
    if root:
        group = "satip-status" if name in ("status.json", "local-takeover.json", "youtube-status.json") else "satip-management"
        return Path(root) / group / name
    return Path(directory) / name


@lru_cache(maxsize=1)
def boot_id():
    if sys.platform == "darwin":
        # Host-side development only; both deployed stacks use Linux boot_id.
        return subprocess.check_output(["/usr/sbin/sysctl", "-n", "kern.boottime"], text=True).strip()
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def lease_data(seconds, **values):
    return {**values, "boot_id": boot_id(), "monotonic_until": time.monotonic() + seconds}


def lease_active(node):
    if not isinstance(node, dict) or node.get("boot_id") != boot_id():
        return False
    deadline = node.get("monotonic_until")
    return type(deadline) in (int, float) and math.isfinite(deadline) and deadline > time.monotonic()


def library_revision(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class JSONCache:
    """Cache parsed immutable snapshots; atomic replacements change the identity."""
    def __init__(self, limit=8):
        self.entries = OrderedDict()
        self.limit = limit

    def read(self, path):
        path = Path(path)
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        previous = self.entries.get(path)
        if previous and previous[0] == identity:
            self.entries.move_to_end(path)
            return previous[1]
        data = read_json(path)
        self.entries[path] = identity, data
        self.entries.move_to_end(path)
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)
        return data


class JSONWriter:
    """Serialize durable snapshots off-loop, joining every write on cancellation."""
    def __init__(self):
        import asyncio
        self.lock = asyncio.Lock()

    async def write(self, path, data, mode=0o600):
        import asyncio
        from .lifecycle import complete
        snapshot = copy.deepcopy(data)
        async with self.lock:
            await complete(asyncio.create_task(asyncio.to_thread(atomic_json, path, snapshot, mode)))


def retain_snapshots(directory, count=10, byte_limit=32 * 1024 * 1024):
    """Only prune generated API snapshots, never credentials or rollback journals."""
    paths = sorted([*directory.glob("scan-backup-*.json"),
                    *directory.glob("favourites-backup-*.json")], key=lambda p: int(p.stem.rsplit("-", 1)[1]), reverse=True)
    total = 0
    for index, path in enumerate(paths):
        if path.is_symlink():
            raise ValueError("Linked snapshot refused")
        total += path.stat().st_size
        if index >= count or total > byte_limit:
            path.unlink()


class State:
    def __init__(self, path: Path, channels: list[str]):
        self.path = path
        self.data = {"version": 1, "channel": channels[0], "volume": 35, "mute": False}
        self.task = None
        self.dirty = False
        self.error = None
        self.writer = JSONWriter()
        self.revision = 0
        self.changed_at = 0.0
        try:
            saved = read_json(path)
            if (saved.get("version") == 1 and saved.get("channel") in channels
                    and type(saved.get("volume")) in (int, float)
                    and 0 <= saved["volume"] <= 100 and type(saved.get("mute")) is bool):
                self.data = {k: saved[k] for k in self.data}
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def update(self, **values):
        import asyncio
        if all(self.data.get(k) == v for k, v in values.items()):
            return
        self.data.update(values)
        self.dirty = True
        self.revision += 1
        self.changed_at = time.monotonic()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._save_later())

    async def _save_later(self):
        import asyncio
        import logging
        while self.dirty:
            await asyncio.sleep(max(.05, 2 - (time.monotonic() - self.changed_at)))
            if time.monotonic() - self.changed_at < 2:
                continue
            revision = self.revision
            try:
                await self.writer.write(self.path, self.data)
                self.error = None
                self.dirty = self.revision != revision
            except OSError:
                self.error = "state_write_failed"
                logging.getLogger(__name__).warning("state_write_failed: private state remains unsaved")
                await asyncio.sleep(5)

    async def close(self):
        from .lifecycle import stop_task
        await stop_task(self.task)
        if self.dirty or not self.path.exists():
            await self.writer.write(self.path, self.data)
            self.dirty = False
            self.error = None
