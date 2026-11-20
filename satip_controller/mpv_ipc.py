"""Correlated asynchronous mpv IPC. Never include commands/replies in errors."""
import asyncio
import contextlib
import json


class IPCError(Exception):
    def __init__(self, message, *, reason="ipc_error"):
        super().__init__(message)
        self.reason = reason


class MPVIPC:
    def __init__(self, reader, writer, timeout=3):
        self.reader, self.writer = reader, writer
        self.timeout = timeout
        self.sequence = 0
        self.pending = {}
        self.lock = asyncio.Lock()
        self.closed = False
        self.load_entry = None
        self.load_failure = False
        self.task = asyncio.create_task(self._read())

    @classmethod
    async def connect(cls, path, timeout=3):
        reader, writer = await asyncio.open_unix_connection(str(path))
        return cls(reader, writer, timeout)

    async def command(self, *command):
        if self.closed:
            raise IPCError("Player disconnected")
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            async with asyncio.timeout(self.timeout):
                async with self.lock:
                    payload = command[0] if len(command) == 1 and isinstance(command[0], dict) else command
                    self.writer.write((json.dumps({"command": payload, "request_id": request_id}) + "\n").encode())
                    await self.writer.drain()
                return await future
        except (OSError, asyncio.TimeoutError) as exc:
            # A live socket does not mean the mpv core is responsive. Retrying
            # commands on it forever leaves both playback and the splash stuck.
            self.closed = True
            self.writer.close()
            reason = "ipc_timeout" if isinstance(exc, asyncio.TimeoutError) else "ipc_disconnected"
            raise IPCError("Player command timed out or disconnected", reason=reason) from None
        finally:
            self.pending.pop(request_id, None)

    async def get(self, name):
        return await self.command("get_property", name)

    def begin_load(self):
        self.load_entry = None
        self.load_failure = False

    def load_event(self, message):
        # Only an error for the current playlist entry can fail this load.
        # Successful playback still requires path/position/audio confirmation.
        entry = message.get("playlist_entry_id")
        if message.get("event") == "start-file":
            self.load_entry = entry
        elif (message.get("event") == "end-file" and entry is not None and
              entry == self.load_entry and message.get("reason") == "error"):
            self.load_failure = True

    async def _read(self):
        try:
            while line := await self.reader.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("Invalid IPC message")
                self.load_event(message)
                future = self.pending.get(message.get("request_id"))
                if future and not future.done():
                    if message.get("error") == "success":
                        future.set_result(message.get("data"))
                    else:
                        future.set_exception(IPCError("Player rejected command"))
                # Events are deliberately not used as load completion: readiness
                # uses the current path plus advancing playback (stale-safe).
        except (OSError, ValueError, asyncio.LimitOverrunError):
            pass
        finally:
            self.closed = True
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(IPCError("Player disconnected"))

    async def close(self):
        self.writer.close()
        with contextlib.suppress(OSError):
            await self.writer.wait_closed()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
