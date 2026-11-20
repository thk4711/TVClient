"""Generation release barrier and Linux real-process relay isolation."""
import asyncio
import contextlib
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, Mock, patch

from satip_controller.relay_worker import Generation, RelayWorker
from satip_controller.rolling import RollingBuffer, cleanup, process_start
from satip_controller.timeshift import Timeshift


class ReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_ack_allows_cleanup_to_continue_without_waiting_for_exit(self):
        exited = asyncio.StreamReader()
        released = asyncio.StreamReader()
        released.feed_data(b'R')
        generation = Generation(123, exited, Mock(), released, Mock())
        await generation.release()
        self.assertIsNone(generation.returncode)
        self.assertFalse(generation.finished.done())
        exited.feed_data(b'0')
        exited.feed_eof()
        self.assertEqual(await generation.wait(), 0)

    async def test_eof_without_ack_kills_and_waits_before_reuse(self):
        exited = asyncio.StreamReader()
        released = asyncio.StreamReader()
        released.feed_eof()
        generation = Generation(123, exited, Mock(), released, Mock())
        def kill():
            exited.feed_data(b'-9')
            exited.feed_eof()
        generation.kill = Mock(side_effect=kill)
        await generation.release()
        generation.kill.assert_called_once()
        self.assertEqual(generation.returncode, -9)

    async def test_wait_timeout_does_not_cancel_exit_observer(self):
        exited = asyncio.StreamReader()
        generation = Generation(123, exited, Mock(), asyncio.StreamReader(), Mock())
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(generation.wait(), .01)
        self.assertFalse(generation.finished.cancelled())
        exited.feed_data(b'0')
        exited.feed_eof()
        await generation.wait()

    async def test_close_releases_before_retirement_and_does_not_delete_live_files(self):
        events = []
        worker = Mock()
        buffer = RollingBuffer(worker=worker)
        buffer.process = Mock()
        buffer.process.release = AsyncMock(side_effect=lambda: events.append('release'))
        worker.retire.side_effect = lambda *_: events.append('retire')
        buffer.deactivate = AsyncMock()
        buffer.disconnect = AsyncMock()
        with patch('satip_controller.rolling.cleanup') as remove:
            await buffer.close()
            remove.assert_not_called()
        self.assertEqual(events, ['release', 'retire'])
        self.assertIsNone(buffer.process)

    async def test_repeated_cancel_of_close_still_finishes_release_once(self):
        entered = asyncio.Event()
        finish = asyncio.Event()
        async def release():
            entered.set()
            await finish.wait()
        worker = Mock()
        buffer = RollingBuffer(worker=worker)
        buffer.process = Mock(release=AsyncMock(side_effect=release))
        buffer.deactivate = AsyncMock()
        buffer.disconnect = AsyncMock()
        task = asyncio.create_task(buffer.close())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        worker.retire.assert_called_once()
        await buffer.close()
        worker.retire.assert_called_once()

    async def test_retirement_backpressure_cancellation_does_not_cancel_cleanup(self):
        worker = RelayWorker()
        worker.start = AsyncMock()
        entered = asyncio.Event()
        finish = asyncio.Event()
        async def cleanup():
            entered.set()
            await finish.wait()
        retired = [asyncio.create_task(cleanup()) for _ in range(2)]
        worker.retired.update(retired)
        task = asyncio.create_task(worker.spawn(None, None, '', '', -1, -1))
        await entered.wait()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(any(t.cancelled() for t in retired))
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(all(t.done() and not t.cancelled() for t in retired))

    async def test_shutdown_drains_worker_but_ordinary_close_keeps_it_warm(self):
        timeshift = Timeshift()
        timeshift.worker = Mock(close=AsyncMock())
        worker = timeshift.worker
        await timeshift.close()
        worker.close.assert_not_awaited()
        await timeshift.shutdown()
        worker.close.assert_awaited_once()
        self.assertIsNone(timeshift.worker)


@unittest.skipUnless(sys.platform == 'linux' and shutil.which('ffmpeg'),
                     'Requires Linux tmpfs, SCM_RIGHTS, fork and FFmpeg')
class WorkerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.worker = RelayWorker()
        self.temp = tempfile.TemporaryDirectory(prefix='satip-worker-test-',
            dir=os.environ.get('SATIP_TEST_RUNTIME', '/run'))
        self.runtime = Path(self.temp.name)
        self.buffers = []
        self.active = 0
        self.maximum = 0
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                outer.active += 1
                outer.maximum = max(outer.maximum, outer.active)
                try:
                    self.send_response(200)
                    self.end_headers()
                    while True:
                        self.wfile.write(self.path[-1:].encode() * (188 * 256))
                        self.wfile.flush()
                        time.sleep(.01)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    outer.active -= 1
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    async def asyncTearDown(self):
        for buffer in self.buffers:
            await buffer.close()
        await self.worker.close()
        await asyncio.to_thread(self.http.shutdown)
        self.http.server_close()
        self.temp.cleanup()

    async def open_buffer(self, tag):
        buffer = RollingBuffer(worker=self.worker)
        self.buffers.append(buffer)
        with patch('satip_controller.rolling.storage', return_value={'uuid': 'test'}):
            await buffer.start(f'http://127.0.0.1:{self.http.server_port}/{tag}', '', self.runtime)
        async with asyncio.timeout(3):
            while not buffer.raw_received:
                await asyncio.sleep(.01)
        return buffer

    async def test_reuses_supervisor_and_separates_generations(self):
        first = await self.open_buffer('a')
        supervisor = self.worker.process.pid
        child = first.process.pid
        owner_directory = first.directory
        cleanup(owner_directory)
        self.assertTrue((owner_directory / 'owner.json').exists())
        self.assertEqual(first.raw_preroll[-1][1][:1], b'a')
        await first.close()
        # Remote close processing is not instantaneous; wait for server teardown
        # before testing its connection count (production has backend barriers).
        async with asyncio.timeout(2):
            while self.active:
                await asyncio.sleep(.01)
        second = await self.open_buffer('b')
        self.assertEqual(supervisor, self.worker.process.pid)
        self.assertNotEqual(child, second.process.pid)
        self.assertNotEqual(first.token, second.token)
        self.assertEqual(second.raw_preroll[-1][1][:1], b'b')
        self.assertTrue(all(data[:1] == b'b' for _, data in second.raw_preroll))
        self.assertEqual(self.maximum, 1)
        await second.close()
        await self.worker.close()
        self.assertFalse(owner_directory.exists())

    async def test_cancelled_spawn_finishes_transaction_and_reaps_child(self):
        exchanged = asyncio.Event()
        original = asyncio.to_thread
        async def delayed(function):
            result = await original(function)
            exchanged.set()
            await asyncio.sleep(.1)
            return result
        buffer = RollingBuffer(worker=self.worker)
        self.buffers.append(buffer)
        with patch('satip_controller.rolling.storage', return_value={'uuid': 'test'}), \
                patch('satip_controller.relay_worker.asyncio.to_thread', side_effect=delayed):
            task = asyncio.create_task(buffer.start(
                f'http://127.0.0.1:{self.http.server_port}/a', '', self.runtime))
            async with asyncio.timeout(5):
                await exchanged.wait()
                task.cancel()
                await asyncio.sleep(.02)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertIsNone(self.worker.process)
        self.assertFalse(self.worker.generations)
        async with asyncio.timeout(2):
            while self.active:
                await asyncio.sleep(.01)

    async def test_supervisor_death_releases_generation_and_can_restart(self):
        first = await self.open_buffer('a')
        old = self.worker.process.pid
        # An unexpected SIGKILL must also release the capture, not leave an
        # orphan subscription until the controller's next channel change.
        self.worker.process.kill()
        async with asyncio.timeout(3):
            await self.worker.process.wait()
            await first.process.wait()
        self.assertIsNotNone(first.process.returncode)
        await first.close()
        second = await self.open_buffer('b')
        self.assertNotEqual(old, self.worker.process.pid)
        self.assertGreater(second.raw_received, 0)
