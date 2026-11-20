"""Bounded client for the fixed-action, root-owned device helper."""
import asyncio
import json

SOCKET = "/run/satip-device/control.sock"
LIMIT = 16384


async def request(action, args=None):
    async with asyncio.timeout(120 if action == "backup_validate" else 180 if action == "storage_format" else 50 if action == "storage_eject" else 25):
        reader, writer = await asyncio.open_unix_connection(SOCKET, limit=LIMIT)
        try:
            message = json.dumps({"action": action, "args": args or {}}).encode() + b"\n"
            if len(message) > LIMIT:
                raise ValueError("Device request too large")
            writer.write(message)
            await writer.drain()
            result = json.loads(await reader.readline())
            if not result.get("ok"):
                raise ValueError(result.get("message", "Device change failed"))
            return result["data"]
        finally:
            writer.close()
            await writer.wait_closed()
