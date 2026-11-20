"""Shared single-tuner handover and process-generation checks."""
import asyncio
from pathlib import Path
import time
from typing import Callable, Protocol, Any

from .backend import BackendError
from .state import read_json, lease_active, boot_id


class BackendStatus(Protocol):
    async def grid(self, endpoint: str) -> list[dict[str, Any]]: ...


def process_identity(pid: int) -> dict[str, Any] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] != "Z":
            return {"pid": pid, "start": fields[19], "boot_id": boot_id()}
    except (OSError, ValueError, IndexError):
        pass
    return None


def owner_alive(owner: Any) -> bool:
    return (isinstance(owner, dict) and type(owner.get("pid")) is int
            and owner == process_identity(owner["pid"]))


def preview_held(lease: dict) -> bool:
    # Expiry alone cannot revoke a live owner or an encoder still being reaped.
    return lease_active(lease) or (lease.get("active") is True and
                                  any(owner_alive(owner) for owner in lease.get("owners", [])))


async def wait_quiet(backend: BackendStatus, status_path: Path, renew: Callable[[], None],
                     timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while True:
        renew()
        subscriptions = await backend.grid("status/subscriptions")
        try:
            player = read_json(status_path)
            if "owner" in player:
                running = owner_alive(player["owner"])
            else:
                pid = player.get("owner_pid")
                running = type(pid) is int and Path(f"/proc/{pid}").exists()
            acknowledged = not running or player.get("paused") is True
        except (OSError, ValueError, AttributeError):
            acknowledged = True
        if not subscriptions and acknowledged:
            return
        if time.monotonic() >= deadline:
            raise BackendError("Playback did not pause or another client is using the tuner")
        await asyncio.sleep(.25)
