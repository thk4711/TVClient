"""Preloaded, single-threaded fork supervisor for isolated relay generations.

Private socketpair carries credentials and SCM_RIGHTS, never argv or disk.
The supervisor owns/reaps children; each capture has its own process group.
"""
import array
import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import socket
import sys
import time
import urllib.request  # Preload before fork, not on the switching path.

from .rolling import capture, cleanup
from .lifecycle import complete


def supervise(fd):
    control = socket.socket(fileno=fd)
    children = {}
    signal.signal(signal.SIGTERM, lambda *_: control.close())
    try:
        while True:
            message, ancillary, flags, _ = control.recvmsg(8192, socket.CMSG_SPACE(5 * 4))
            if not message:
                break
            descriptors = array.array('i')
            for level, kind, data in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    descriptors.frombytes(data[:len(data) - len(data) % descriptors.itemsize])
            try:
                if flags or len(descriptors) != 5:
                    raise ValueError('Invalid relay command')
                directory, index, upstream, header = json.loads(message)
                pid = os.fork()
                if pid == 0:
                    control.close()
                    signal.signal(signal.SIGTERM, signal.SIG_DFL)
                    os.setsid()
                    parent, raw, released, exited, start = descriptors
                    # No upstream can open until the controller has tracked our
                    # PID and observed that the process group is ready to kill.
                    os.write(released, b'S')
                    if os.read(start, 1) != b'G':
                        os._exit(0)
                    os.close(start)
                    # Never keep another generation's lifetime descriptors.
                    for old in children.values():
                        os.close(old)
                    try:
                        capture(Path(directory), Path(index), parent, raw,
                                upstream, header, released)
                    finally:
                        os._exit(0)
                children[pid] = descriptors[3]
                control.send(json.dumps(pid).encode())
            finally:
                for position, item in enumerate(descriptors):
                    if position != 3:
                        os.close(item)
            # Reap while idle as well as while switching.
            control.settimeout(.1)
            while True:
                for pid, exited in list(children.items()):
                    result, status = os.waitpid(pid, os.WNOHANG)
                    if result:
                        os.write(exited, str(os.waitstatus_to_exitcode(status)).encode())
                        os.close(exited)
                        del children[pid]
                try:
                    if control.recv(1, socket.MSG_PEEK):
                        break
                    return
                except socket.timeout:
                    pass
            control.settimeout(None)
    except (OSError, ValueError):
        pass
    finally:
        control.close()
        for pid in children:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)
        for pid, exited in children.items():
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)
            os.close(exited)


async def pipe_reader(fd):
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: protocol, os.fdopen(fd, 'rb', buffering=0))
    return reader, transport


class Generation:
    def __init__(self, pid, reader, transport, released, release_transport):
        self.pid = pid
        self.returncode = None
        self.released = released
        self.release_transport = release_transport
        self.transport = transport
        self.finished = asyncio.create_task(self._wait(reader))

    async def _wait(self, reader):
        try:
            result = await reader.read()
            self.returncode = int(result) if result else -signal.SIGKILL
        finally:
            self.transport.close()
        return self.returncode

    async def wait(self):
        return await asyncio.shield(self.finished)

    def kill(self):
        if self.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.pid, signal.SIGKILL)

    async def release(self):
        try:
            async with asyncio.timeout(1):
                if await self.released.read(1) == b'R':
                    return
        except TimeoutError:
            pass
        # No acknowledgment means no permission to open another subscription.
        self.kill()
        await self.wait()
        self.release_transport.close()


class RelayWorker:
    def __init__(self):
        self.process = None
        self.control = None
        self.retired = set()
        self.generations = set()
        self.monitor = None

    async def start(self):
        if self.process and self.process.returncode is None:
            return
        if self.process:
            await self.close()
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        parent.settimeout(2)
        try:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, '-m', 'satip_controller.relay_worker', str(child.fileno()),
                pass_fds=(child.fileno(),), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            self.control = parent
            async def monitor():
                await self.process.wait()
                for generation in list(self.generations):
                    generation.kill()
            self.monitor = asyncio.create_task(monitor())
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()

    async def spawn(self, directory, index, upstream, header, parent, raw):
        await self.start()
        # Bound retired generations/memory even if FFmpeg cleanup stalls.
        if len(self.retired) >= 2:
            await complete(asyncio.gather(*self.retired))
        released, release_write = os.pipe()
        exited, exit_write = os.pipe()
        start_read, start_write = os.pipe()
        reader, transport = await pipe_reader(exited)
        release_reader, release_transport = await pipe_reader(released)
        def exchange():
            self.control.sendmsg([json.dumps([str(directory), str(index), upstream, header]).encode()],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                  array.array('i', [parent, raw, release_write, exit_write, start_read]))])
            return json.loads(self.control.recv(128))
        async def transaction():
            nonlocal release_write, exit_write
            pid = await asyncio.to_thread(exchange)
            os.close(release_write)
            os.close(exit_write)
            release_write = exit_write = None
            async with asyncio.timeout(2):
                if await release_reader.read(1) != b'S':
                    raise OSError('Relay process group did not become ready')
            generation = Generation(pid, reader, transport, release_reader, release_transport)
            self.generations.add(generation)
            os.write(start_write, b'G')
            return generation
        try:
            # Defer even repeated cancellation until transferred descriptors and
            # the child are tracked. A thread cannot be cancelled by its caller.
            return await complete(asyncio.create_task(transaction()))
        except BaseException:
            await complete(asyncio.create_task(self.close()))
            transport.close()
            release_transport.close()
            raise
        finally:
            if release_write is not None:
                os.close(release_write)
            if exit_write is not None:
                os.close(exit_write)
            os.close(start_read)
            os.close(start_write)

    def retire(self, generation, directory, index):
        async def finish():
            try:
                async with asyncio.timeout(5):
                    await generation.wait()
            except TimeoutError:
                generation.kill()
                await generation.wait()
            finally:
                generation.release_transport.close()
                self.generations.discard(generation)
            if directory and directory.exists():
                cleanup(directory)
            if index:
                index.unlink(missing_ok=True)
        task = asyncio.create_task(finish())
        self.retired.add(task)
        task.add_done_callback(self.retired.discard)
        # Retrieve errors even when a later spawn does not need to wait.
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def close(self):
        if self.control:
            self.control.close()
            self.control = None
        if self.process:
            try:
                async with asyncio.timeout(3):
                    await self.process.wait()
            except TimeoutError:
                for generation in self.generations:
                    generation.kill()
                self.process.kill()
                await self.process.wait()
            if self.monitor:
                await self.monitor
                self.monitor = None
            self.process = None
        if self.retired:
            await complete(asyncio.gather(*self.retired))
        for generation in list(self.generations):
            generation.kill()
            await generation.wait()
            generation.release_transport.close()
        self.generations.clear()


if __name__ == '__main__':
    supervise(int(sys.argv[1]))
