"""Rolling retention, lifecycle and user-facing boundary policy; no real tuner."""
import asyncio
import copy
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from satip_controller.mpv_ipc import IPCError
from satip_controller.player import Player
from satip_controller.rolling import RollingBuffer, SEGMENT_BYTES, retained, cleanup
from satip_controller.timeshift import Timeshift
from tests.test_timeshift import CONFIG, StorageTests
from satip_controller.state import atomic_json


def segment(number, start, end, size=10 * 1024**2):
    return {"name": f"segment-{number:09d}.ts", "start": start, "end": end, "bytes": size}


class RelayDefaultTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_prepare_uses_worker_and_legacy_override_drains_it(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            worker = Mock(close=AsyncMock())
            buffer = Mock(config=CONFIG, start=AsyncMock(return_value=True),
                          close=AsyncMock(), live=Mock(return_value="relay"))
            ts = Timeshift()
            with patch("satip_controller.timeshift.storage", return_value=CONFIG), \
                    patch("satip_controller.relay_worker.RelayWorker", return_value=worker) as factory, \
                    patch("satip_controller.rolling.RollingBuffer", return_value=buffer) as rolling:
                self.assertEqual(await ts.prepare("upstream", "header", runtime, AsyncMock()), "relay")
                factory.assert_called_once_with()
                rolling.assert_called_once_with(worker=worker)
                atomic_json(runtime / "switch-experiments.json", {"persistent_relay": False})
                rolling.reset_mock()
                await ts.prepare("upstream", "header", runtime, AsyncMock())
                worker.close.assert_awaited_once()
                rolling.assert_called_once_with()
                self.assertIsNone(ts.worker)
                await ts.shutdown()


class RetentionTests(unittest.TestCase):
    def test_duration_rolls_oldest_whole_segments(self):
        items = [segment(i, i * 10, (i + 1) * 10) for i in range(10)]
        keep = retained(items, 1024**3, 1, 10 * 1024**3)
        self.assertEqual(keep, items[4:])
        self.assertEqual(len(items), 10)

    def test_bytes_reserve_space_for_in_progress_segment(self):
        items = [segment(i, i, i + 1) for i in range(10)]
        keep = retained(items, 60 * 1024**2, 0, 10 * 1024**3)
        self.assertEqual(keep, items[-2:])
        self.assertLessEqual(sum(s["bytes"] for s in keep) + SEGMENT_BYTES, 60 * 1024**2)

    def test_low_space_evicts_history_before_failing(self):
        items = [segment(i, i, i + 1) for i in range(5)]
        keep = retained(items, 1024**3, 0, 1024**3)
        self.assertEqual(len(keep), 1)
        with self.assertRaises(OSError):
            retained(items, 1024**3, 0, 0)

    def test_zero_minutes_has_no_elapsed_session_cutoff(self):
        items = [segment(i, i * 86400, (i + 1) * 86400) for i in range(3)]
        self.assertEqual(retained(items, 1024**3, 0, 10 * 1024**3), items)

    def test_metadata_is_bounded_independently(self):
        items = [segment(i, i, i + 1, 1) for i in range(4100)]
        self.assertEqual(len(retained(items, 1024**3, 0, 10 * 1024**3)), 4096)

    def test_cannot_retain_even_one_segment(self):
        with self.assertRaises(OSError):
            retained([segment(0, 0, 10)], SEGMENT_BYTES, 0, 10 * 1024**3)

    def test_cleanup_only_removes_owned_flat_session_files(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / ("rolling-" + "a" * 32)
            directory.mkdir()
            (directory / "segment-000000000.ts").write_bytes(b"owned")
            foreign = directory / "holiday.ts"
            foreign.write_bytes(b"keep")
            cleanup(directory)
            self.assertTrue(foreign.exists())
            self.assertFalse((directory / "segment-000000000.ts").exists())

    def test_cleanup_never_follows_links(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            foreign = root / "keep"
            foreign.write_bytes(b"keep")
            directory = root / ("rolling-" + "a" * 32)
            directory.mkdir()
            (directory / "segment-000000000.ts").symlink_to(foreign)
            cleanup(directory)
            self.assertEqual(foreign.read_bytes(), b"keep")

    def test_playback_clamps_and_uses_stable_segment_origin(self):
        buffer = RollingBuffer()
        buffer.refresh = Mock()
        buffer.server = Mock(sockets=[Mock(getsockname=lambda: ("127.0.0.1", 12345))])
        buffer.segments = [segment(8, 80, 90), segment(9, 90, 100)]
        path, start = buffer.playback(85)
        self.assertIn("segment-000000008.ts", path)
        self.assertEqual((buffer.origin, start), (0, 5))
        buffer.calibrate(85)
        self.assertEqual(buffer.origin, 0)
        buffer.playback(0)
        self.assertEqual(buffer.anchor, 80)
        buffer.playback(200)
        self.assertEqual(buffer.anchor, 90)

    def test_playback_timestamp_gap_uses_preceding_not_latest_segment(self):
        buffer = RollingBuffer()
        buffer.refresh = Mock()
        buffer.server = Mock(sockets=[Mock(getsockname=lambda: ("127.0.0.1", 12345))])
        buffer.segments = [segment(0, 0, 1.8), segment(1, 2, 3.8), segment(2, 4, 5.8)]
        path, _ = buffer.playback(1.9)
        self.assertIn("segment-000000000.ts", path)

    def test_transient_zero_is_not_mistaken_for_broadcast_timestamp_wrap(self):
        buffer = RollingBuffer()
        buffer.anchor = 71000
        self.assertFalse(buffer.calibrate(0))
        self.assertIsNone(buffer.position_offset)
        self.assertEqual(buffer.origin, 0)
        self.assertTrue(buffer.calibrate(71001))
        self.assertEqual(buffer.origin, 0)


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ts = Timeshift()
        self.ts.config = copy.deepcopy(CONFIG)
        self.ts.active = True
        self.ts.config["minutes"] = 1
        self.ts.buffer = Mock(error="", origin=0, segments=[segment(0, 0, 30), segment(1, 30, 60)])
        self.ts.buffer.raw_error = ""
        self.ts.buffer.capture_failure = {}
        self.ts.buffer.live_reader = False
        self.ts.buffer.disk_error = ""
        self.ts.buffer.disk_segments = self.ts.buffer.segments
        self.ts.buffer.ram_segments = []
        self.ts.buffer.disk_process = None
        self.raw_position = 20
        self.pause = True
        self.ipc = Mock(closed=False)
        self.ipc.get = AsyncMock(side_effect=lambda name: {"pause": self.pause, "time-pos": self.raw_position}[name])
        self.ipc.command = AsyncMock()
        self.ts.jump = AsyncMock()

    async def update(self):
        with patch("satip_controller.timeshift.storage", return_value=self.ts.config):
            return await self.ts.update(self.ipc, 123)

    async def test_warns_before_paused_position_expires(self):
        self.assertIsNone(await self.update())
        self.assertIn("expire", self.ts.warning)
        self.assertEqual(self.ts.expiry_seconds, 20)
        self.ts.jump.assert_not_awaited()

    async def test_paused_expiry_clamps_without_resuming_or_resetting(self):
        self.ts.buffer.segments = [segment(2, 30, 60), segment(3, 60, 90)]
        self.assertIsNone(await self.update())
        self.ts.jump.assert_awaited_once_with(30, self.ipc)
        self.assertTrue(self.ts.paused)
        self.assertIn("Oldest", self.ts.notice)
        self.ipc.command.assert_not_awaited()

    async def test_playing_expiry_clamps_without_return_to_live(self):
        self.pause = False
        self.ts.buffer.segments = [segment(2, 30, 60), segment(3, 60, 90)]
        self.assertIsNone(await self.update())
        self.ts.jump.assert_awaited_once_with(30, self.ipc)
        self.assertFalse(self.ts.paused)

    async def test_warning_only_forecasts_for_paused_viewer(self):
        self.pause = False
        await self.update()
        self.assertEqual(self.ts.warning, "")

    async def test_storage_loss_and_capture_failure_request_live_recovery(self):
        with patch("satip_controller.timeshift.storage", return_value=None):
            self.assertEqual(await self.ts.update(self.ipc, 123), "storage")
        self.ts.buffer.error = "Capture stopped"
        self.assertEqual(await self.update(), "storage")
        self.assertEqual(self.ts.failed_uuid, CONFIG["uuid"])

    async def test_index_io_error_requests_recovery(self):
        self.ts.buffer.refresh.side_effect = OSError("USB error")
        self.assertEqual(await self.update(), "storage")

    async def test_budget_update_does_not_reset(self):
        self.ts.config["max_bytes"] = 1024**3
        self.assertIsNone(await self.update())
        self.assertTrue(self.ts.config)

    async def test_transient_position_unavailable_during_reload(self):
        self.ipc.get.side_effect = lambda name: True if name == "pause" else (_ for _ in ()).throw(IPCError("Not ready"))
        self.assertIsNone(await self.update())


class ActivationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.player = Player(SimpleNamespace(runtime_dir=Path(temp.name), drm_connector="HDMI-A-2",
            audio_device="alsa/hdmi:CARD=vc4hdmi1,DEV=0", drm_device=str(Path(temp.name) / "missing")), SimpleNamespace())
        self.player.process = SimpleNamespace(pid=123)
        self.player.ipc = Mock(closed=False, command=AsyncMock(), get=AsyncMock(return_value=True))
        self.channel = SimpleNamespace(uuid="channel", video="h264")

    async def test_pause_resume_preserve_current_picture_without_reload(self):
        self.player.timeshift.config = CONFIG
        self.player.ipc.get.side_effect = lambda name: {"pause": self.player.timeshift.paused,
            "time-pos": 2, "demuxer-cache-state": {}}[name]
        for paused in (True, False, True):
            await self.player.timeshift_action("pause")
            self.assertEqual(self.player.timeshift.paused, paused)
        self.assertTrue(all(c.args[0] != "loadfile" for c in self.player.ipc.command.await_args_list))

    async def test_missing_usb_leaves_live_picture_unchanged(self):
        with patch("satip_controller.timeshift.storage", return_value=None):
            self.assertIn("USB", await self.player.timeshift_action("pause"))
        self.player.ipc.command.assert_not_awaited()

    async def test_channel_change_closes_history_and_loads_unpaused(self):
        buffer = Mock(close=AsyncMock())
        self.player.timeshift.buffer = buffer
        self.player.timeshift.config = CONFIG
        self.player.timeshift.paused = True
        with patch("satip_controller.timeshift.storage", return_value=None):
            await self.player.load(self.channel)
        buffer.close.assert_awaited_once()
        self.assertFalse(self.player.timeshift.paused)
        self.player.ipc.command.assert_any_await("stop")
        self.player.ipc.command.assert_any_await("set_property", "pause", True)
        self.assertTrue(self.player.initial_audio_pending)

    async def test_available_storage_prepares_rolling_source(self):
        self.player.timeshift.prepare = AsyncMock(return_value="local")
        await self.player.load(self.channel)
        self.player.timeshift.prepare.assert_awaited_once()
        self.assertEqual(self.player.ipc.command.await_args.args[1], "local")

    async def test_start_failure_cleans_up_and_uses_direct_live(self):
        with patch("satip_controller.timeshift.storage", return_value=CONFIG), patch("satip_controller.rolling.RollingBuffer") as rolling:
            rolling.return_value.start = AsyncMock(side_effect=OSError("Unavailable"))
            rolling.return_value.close = AsyncMock()
            path = await self.player.timeshift.prepare("live", "header", Path("/unused"), AsyncMock())
        self.assertEqual(path, "live")
        rolling.return_value.close.assert_awaited_once()

    async def test_failed_prebuffer_recovers_direct_before_retrying(self):
        ts = self.player.timeshift
        ts.buffer = Mock(error="capture blocked", raw_error="", capture_failure={}, refresh=Mock())
        with patch("satip_controller.timeshift.storage", return_value=CONFIG):
            self.assertEqual(await ts.update(self.player.ipc, 123), "storage")
        retry_after = ts.retry_after
        ts.buffer.close = AsyncMock()
        with patch("satip_controller.timeshift.storage", return_value=CONFIG), \
             patch("satip_controller.rolling.RollingBuffer") as rolling:
            self.assertEqual(await ts.prepare("live", "header", Path("/unused"), AsyncMock()), "live")
            rolling.assert_not_called()
        self.assertEqual(ts.retry_after, retry_after)
        self.assertEqual(ts.failed_uuid, CONFIG["uuid"])
        ts.buffer = None
        with patch("satip_controller.timeshift.storage", return_value=CONFIG), \
             patch("satip_controller.timeshift.time.monotonic", return_value=retry_after + 1):
            self.assertEqual(await ts.update(self.player.ipc, 123), "ready")

    async def test_cancelled_start_cleans_up(self):
        with patch("satip_controller.timeshift.storage", return_value=CONFIG), patch("satip_controller.rolling.RollingBuffer") as rolling:
            rolling.return_value.start = AsyncMock(side_effect=asyncio.CancelledError)
            rolling.return_value.close = AsyncMock()
            with self.assertRaises(asyncio.CancelledError):
                await self.player.timeshift.prepare("live", "header", Path("/unused"), AsyncMock())
        rolling.return_value.close.assert_awaited_once()


class RemovalTests(StorageTests):
    def test_eject_keeps_remembered_settings_and_blocks_immediate_remount(self):
        config = {"version": 1, "uuid": "test-uuid", "minutes": 0, "max_bytes": 8 * 1024**3}
        atomic_json(self.store.config, config, 0o600)
        self.store.status = Mock(return_value={"device_id": self.identifier})
        self.store.mounted_device = Mock(return_value="8:1")
        self.store.handle("storage_eject", {})
        self.assertEqual(self.store.saved(), config)
        self.assertTrue(self.store.ejected.exists())
        self.store.mounted_device.return_value = None
        self.store.attach = Mock()
        self.store.tick()
        self.store.attach.assert_not_called()
        self.store.tree.return_value = []
        self.store.next_retry = 0
        self.store.tick()
        self.assertFalse(self.store.ejected.exists())
        self.store.tree.return_value = [self.disk]
        self.store.next_retry = 0
        self.store.tick()
        self.store.attach.assert_called_once()

    def test_forget_requires_unmounted_and_never_erases_disk(self):
        self.store.config.write_text("{}")
        self.store.mounted_device = Mock(return_value="8:1")
        with self.assertRaises(ValueError):
            self.store.handle("storage_forget", {})
        self.assertTrue(self.store.config.exists())
        self.store.mounted_device.return_value = None
        self.store.handle("storage_forget", {})
        self.assertFalse(self.store.config.exists())
        self.command.assert_not_called()


class RelayLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_paused_backpressure_reader_cancels_within_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            buffer = RollingBuffer()
            buffer.directory = Path(temp)
            item = segment(0, 0, 2, 8 * 1024**2)
            (buffer.directory / item["name"]).write_bytes(b"x" * item["bytes"])
            buffer.process = SimpleNamespace(returncode=None)
            buffer.segments = [item]
            buffer.segment_by_name = {item["name"]: item}
            buffer.refresh = Mock()
            buffer.server = await asyncio.start_server(buffer.serve, "127.0.0.1", 0)
            port = buffer.server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"GET /{buffer.token}/{item['name']} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
            await writer.drain()
            await reader.readuntil(b"\r\n\r\n")
            await asyncio.sleep(.1)
            async with asyncio.timeout(2):
                await buffer.disconnect()
            self.assertFalse(buffer.clients)
            buffer.server.close()
            await buffer.server.wait_closed()
            writer.transport.abort()

    async def test_bad_session_path_is_not_served(self):
        buffer = RollingBuffer()
        buffer.server = await asyncio.start_server(buffer.serve, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", buffer.server.sockets[0].getsockname()[1])
        writer.write(b"GET /wrong/segment-000000000.ts HTTP/1.1\r\n\r\n")
        await writer.drain()
        self.assertIn(b"404", await reader.readline())
        writer.close()
        await writer.wait_closed()
        await buffer.disconnect()
        buffer.server.close()
        await buffer.server.wait_closed()

    def test_unchanged_index_retains_segments_and_detects_dead_writer(self):
        with tempfile.TemporaryDirectory() as temp:
            buffer = RollingBuffer()
            buffer.index = Path(temp) / "index.json"
            items = [segment(0, 0, 2)]
            atomic_json(buffer.index, {"segments": items, "config": CONFIG, "error": ""})
            buffer.process = SimpleNamespace(returncode=None)
            buffer.refresh()
            timeline = buffer.segments
            lookup = buffer.segment_by_name
            buffer.refresh()
            self.assertIs(buffer.segments, timeline)
            self.assertIs(buffer.segment_by_name, lookup)
            self.assertEqual(buffer.segments, [{**s, "location": "ram"} for s in items])
            buffer.process.returncode = 1
            buffer.refresh()
            self.assertIn("prebuffer", buffer.error)

    def test_large_history_rebuilds_only_for_content_or_mode_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            buffer = RollingBuffer()
            buffer.index = Path(temp) / "ram.json"
            buffer.disk_index = Path(temp) / "usb.json"
            buffer.disk_enabled = True
            items = [segment(i, i * 2, i * 2 + 2, 1) for i in range(4096)]
            atomic_json(buffer.index, {"segments": items[-10:], "health": {"upstream_age": 0}})
            atomic_json(buffer.disk_index, {"segments": items, "ready": True, "config": CONFIG})
            buffer.refresh()
            timeline, lookup = buffer.segments, buffer.segment_by_name
            for _ in range(20):
                buffer.refresh()
                self.assertIs(buffer.segments, timeline)
                self.assertIs(buffer.segment_by_name, lookup)
            atomic_json(buffer.index, {"segments": items[-10:], "health": {"upstream_age": 1}})
            buffer.refresh()
            self.assertEqual(buffer.capture_health, {"upstream_age": 1})
            self.assertIs(buffer.segments, timeline)
            atomic_json(buffer.disk_index, {"segments": items, "ready": False, "error": "failed"})
            buffer.refresh()
            self.assertFalse(buffer.disk_ready)
            self.assertEqual(buffer.disk_error, "failed")
            self.assertIs(buffer.segments, timeline)
            atomic_json(buffer.disk_index, {"segments": items[1:]})
            buffer.refresh()
            self.assertNotIn(items[0]["name"], buffer.segment_by_name)
            self.assertEqual(buffer.segments[-1]["location"], "usb")
            buffer.disk_enabled = False
            buffer.refresh()
            self.assertEqual(len(buffer.segments), 10)
            self.assertEqual(buffer.segments[-1]["location"], "ram")
            buffer.index.unlink()
            buffer.refresh()
            self.assertEqual(buffer.segments, [])
            atomic_json(buffer.index, {"segments": items[-1:]})
            buffer.refresh()
            self.assertEqual(len(buffer.segments), 1)

    async def test_slow_archive_read_does_not_block_input_or_accumulate_workers(self):
        buffer = RollingBuffer()
        buffer.directory = Path("/ram/original")
        buffer.disk_directory = Path("/usb/original")
        buffer.disk_enabled = True
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        calls = []

        def read(item, directories):
            calls.append((item, directories))
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise OSError("Test read timed out")
            return b"programme"

        buffer.read_segment = Mock(side_effect=read)
        first = asyncio.create_task(buffer.read_archive(segment(0, 0, 2)))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            pending = buffer.archive_read
            self.assertIsNotNone(pending)
            self.assertFalse(pending.cancelled())
            second = asyncio.create_task(buffer.read_archive(segment(1, 2, 4)))
            await asyncio.sleep(.02)
            self.assertEqual(len(calls), 1)
            self.assertFalse(second.done())
            self.assertEqual(calls[0][1], (Path("/usb/original"), Path("/ram/original")))
            release.set()
            self.assertEqual(await asyncio.wait_for(second, 1), b"programme")
            self.assertEqual(len(calls), 2)
            self.assertIsNone(buffer.archive_read)
            self.assertEqual(buffer.diagnostics()["archive_reads"], 2)
        finally:
            release.set()
            if buffer.archive_read:
                await asyncio.gather(buffer.archive_read, return_exceptions=True)

    async def test_obsolete_read_failure_is_consumed_and_next_reader_can_continue(self):
        buffer = RollingBuffer()
        buffer.directory = Path("/ram")
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def read(item, directories):
            loop.call_soon_threadsafe(entered.set)
            release.wait(3)
            raise FileNotFoundError("expired")

        buffer.read_segment = Mock(side_effect=read)
        first = asyncio.create_task(buffer.read_archive(segment(0, 0, 2)))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            second = asyncio.create_task(buffer.read_archive(segment(1, 2, 4)))
            await asyncio.sleep(0)
            buffer.read_segment = Mock(return_value=b"next")
            release.set()
            self.assertEqual(await asyncio.wait_for(second, 1), b"next")
        finally:
            release.set()
            if buffer.archive_read:
                await asyncio.gather(buffer.archive_read, return_exceptions=True)

    async def test_close_refuses_safe_storage_claim_while_read_is_still_blocked(self):
        worker = Mock()
        buffer = RollingBuffer(worker=worker)
        buffer.process = Mock(release=AsyncMock())
        process = buffer.process
        buffer.deactivate = AsyncMock()
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def read(item, directories):
            loop.call_soon_threadsafe(entered.set)
            release.wait(4)
            return b"programme"

        buffer.read_segment = Mock(side_effect=read)
        first = asyncio.create_task(buffer.read_archive(segment(0, 0, 2)))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            pending = buffer.archive_read
            with self.assertRaisesRegex(OSError, "storage is not safe"):
                await buffer.close()
            self.assertIs(buffer.archive_read, pending)
            self.assertFalse(pending.done())
            process.release.assert_not_awaited()
            worker.retire.assert_not_called()
            release.set()
            await pending
            await buffer.close()
            await buffer.close()
            process.release.assert_awaited_once()
            worker.retire.assert_called_once()
            self.assertIsNone(buffer.process)
        finally:
            release.set()
            if buffer.archive_read:
                await asyncio.gather(buffer.archive_read, return_exceptions=True)

    async def test_new_storage_triggers_one_live_reload_not_a_retry_loop(self):
        timeshift = Timeshift()
        with patch("satip_controller.timeshift.storage", return_value=CONFIG):
            self.assertEqual(await timeshift.update(Mock(), 123), "ready")
            self.assertIsNone(await timeshift.update(Mock(), 123))
        with patch("satip_controller.timeshift.storage", return_value=None):
            self.assertIsNone(await timeshift.update(Mock(), 123))
        with patch("satip_controller.timeshift.storage", return_value=CONFIG):
            self.assertEqual(await timeshift.update(Mock(), 123), "ready")


class ServerCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_disconnects_clients_before_waiting_for_server(self):
        buffer = RollingBuffer()
        buffer.server = await asyncio.start_server(buffer.serve, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", buffer.server.sockets[0].getsockname()[1])
        # An active client that has not sent a full request still keeps the
        # Python 3.13+ server's wait_closed() pending until it is disconnected.
        await asyncio.sleep(.05)
        async with asyncio.timeout(2):
            await buffer.close()
        self.assertFalse(buffer.clients)
        self.assertEqual(await reader.read(), b"")
        writer.close()
        await writer.wait_closed()


class CaptureFailureTests(unittest.TestCase):
    def test_input_eof_and_timeouts_retain_sanitized_cause(self):
        from satip_controller.rolling import capture
        from satip_controller.state import read_json
        from urllib.error import URLError
        import os
        for fault, reason in ((None, "upstream_eof"),
                              (TimeoutError("secret URL"), "upstream_timeout"),
                              (URLError(TimeoutError("secret URL")), "upstream_timeout"),
                              (URLError("secret URL"), "upstream_error")):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temp:
                runtime = Path(temp)
                directory = runtime / ("rolling-" + "b" * 32)
                directory.mkdir()
                index = runtime / (directory.name + ".json")
                parent, parent_write = os.pipe()
                raw_read, raw = os.pipe()
                process = Mock(stdin=Mock(), poll=Mock(return_value=None))
                source = MagicMock()
                source.__enter__.return_value.read1.return_value = b""
                def thread(target, daemon):
                    return Mock(start=Mock(side_effect=target if target.__name__ == "pump" else None))
                try:
                    with patch("satip_controller.rolling.ram_storage"), \
                            patch("satip_controller.rolling.resource.setrlimit"), \
                            patch("satip_controller.rolling.signal.signal"), \
                            patch("satip_controller.rolling.threading.Thread", side_effect=thread), \
                            patch("satip_controller.rolling.subprocess.Popen", return_value=process), \
                            patch("urllib.request.urlopen", return_value=source, side_effect=fault):
                        capture(directory, index, parent, raw, "http://secret.invalid/private", "Authorization: secret")
                        parent = raw = None  # capture and its pump close these
                    record = runtime / "last-capture-failure.json"
                    data = read_json(record)
                    self.assertEqual(data["reason"], reason)
                    self.assertNotIn("secret", record.read_text())
                    self.assertEqual(read_json(index)["health"], data["health"])
                    process.terminate.assert_called_once()
                finally:
                    for fd in (parent, parent_write, raw_read, raw):
                        if fd is not None:
                            os.close(fd)

    def test_muxer_failure_keeps_terminal_health_and_private_record(self):
        from satip_controller.rolling import capture, capture_notice
        from satip_controller.state import read_json
        import os
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            directory = runtime / ("rolling-" + "a" * 32)
            directory.mkdir()
            index = runtime / (directory.name + ".json")
            parent, parent_write = os.pipe()
            raw_read, raw = os.pipe()
            process = Mock(stdin=Mock(), poll=Mock(return_value=2), wait=Mock(return_value=2))
            try:
                with patch("satip_controller.rolling.ram_storage"), \
                        patch("satip_controller.rolling.resource.setrlimit"), \
                        patch("satip_controller.rolling.signal.signal"), \
                        patch("satip_controller.rolling.threading.Thread"), \
                        patch("satip_controller.rolling.subprocess.Popen", return_value=process):
                    capture(directory, index, parent, raw, "http://secret.invalid/private", "Authorization: secret")
                    parent = None
                data = read_json(index)
                self.assertEqual(data["error"], capture_notice("muxer_exit"))
                self.assertEqual(data["failure"]["muxer_returncode"], 2)
                self.assertEqual(data["health"]["upstream_bytes"], 0)
                self.assertIsInstance(data["health"]["upstream_age"], (int, float))
                record = runtime / "last-capture-failure.json"
                self.assertEqual(record.stat().st_mode & 0o777, 0o600)
                self.assertNotIn("secret", record.read_text())
                buffer = RollingBuffer()
                buffer.index = index
                buffer.refresh()
                self.assertEqual(buffer.capture_failure["reason"], "muxer_exit")
                self.assertEqual(buffer.diagnostics()["upstream_bytes"], 0)
                cleanup(directory)
                index.unlink()
                self.assertTrue(record.exists())
            finally:
                for fd in (parent, parent_write, raw_read, raw):
                    if fd is not None:
                        os.close(fd)

    def test_input_and_muxer_notices_are_distinct_and_translated(self):
        from satip_controller.rolling import capture_notice
        from satip_controller.i18n import GERMAN
        for reason in ("upstream_eof", "upstream_timeout", "upstream_error", "upstream_stalled"):
            self.assertEqual(capture_notice(reason), "TV input interrupted – returning to live TV")
        self.assertNotEqual(capture_notice("muxer_exit"), capture_notice("upstream_timeout"))
        for reason in ("upstream_timeout", "muxer_exit", "segment_stalled", "capture_blocked", "ram_storage"):
            self.assertIn(capture_notice(reason), GERMAN)


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_eof_allows_capture_cause_to_arrive_before_recovery(self):
        ts = Timeshift()
        ts.buffer = Mock(error="", raw_error="Live TV relay unavailable", capture_failure={})
        with patch("satip_controller.timeshift.storage", return_value=CONFIG), \
                patch("satip_controller.timeshift.time.monotonic", return_value=100) as clock:
            self.assertIsNone(await ts.update(Mock(), 1))
            self.assertIsNone(ts.retry_after)
            clock.return_value = 101
            ts.buffer.error = "TV input interrupted – returning to live TV"
            self.assertEqual(await ts.update(Mock(), 1), "storage")
            self.assertEqual(ts.notice, ts.buffer.error)
            self.assertEqual(ts.retry_after, 131)

    async def test_raw_eof_without_capture_cause_still_recovers_promptly(self):
        ts = Timeshift()
        ts.buffer = Mock(error="", raw_error="Live TV relay unavailable", capture_failure={})
        with patch("satip_controller.timeshift.storage", return_value=CONFIG), \
                patch("satip_controller.timeshift.time.monotonic", return_value=100) as clock:
            self.assertIsNone(await ts.update(Mock(), 1))
            clock.return_value = 101
            self.assertEqual(await ts.update(Mock(), 1), "storage")
            self.assertEqual(ts.notice, "Live TV relay unavailable")

    async def test_repeated_runtime_failure_exhausts_automatic_retries(self):
        ts = Timeshift()
        buffer = Mock(config=CONFIG, error="Input lost", raw_error="",
                      capture_failure={}, start=AsyncMock(return_value=True), close=AsyncMock())
        with patch("satip_controller.timeshift.storage", return_value=CONFIG), \
                patch("satip_controller.rolling.RollingBuffer", return_value=buffer) as rolling, \
                patch("satip_controller.timeshift.time.monotonic", return_value=100) as clock:
            for delay in (30, 60, 120, None):
                await ts.prepare("live", "header", Path("/unused"), AsyncMock())
                self.assertIs(ts.buffer, buffer)
                self.assertEqual(await ts.update(Mock(), 1), "storage")
                self.assertEqual(ts.retry_after, clock.return_value + delay if delay else None)
                await ts.close()
                if delay:
                    clock.return_value = ts.retry_after + 1
                    self.assertEqual(await ts.update(Mock(), 1), "ready")
            calls = rolling.call_count
            self.assertEqual(await ts.prepare("live", "header", Path("/unused"), AsyncMock()), "live")
            self.assertEqual(rolling.call_count, calls)
            self.assertIsNone(await ts.update(Mock(), 1))
            self.assertIn("Preparing", await ts.action(Mock(), "pause"))
            await ts.prepare("live", "header", Path("/unused"), AsyncMock())
            self.assertIs(ts.buffer, buffer)

    async def test_backoff_resets_only_after_sustained_fresh_capture(self):
        ts = Timeshift()
        ts.retry_delay = 120
        ts.failed_uuid = CONFIG["uuid"]
        ts.retry_after = 90
        buffer = Mock(config=CONFIG, error="", raw_error="", capture_failure={},
                      capture_health={"upstream_age": 0, "capture_write_age": 0},
                      start=AsyncMock(return_value=True), close=AsyncMock())
        ts.sample = AsyncMock()
        with patch("satip_controller.timeshift.storage", return_value=CONFIG), \
                patch("satip_controller.rolling.RollingBuffer", return_value=buffer), \
                patch("satip_controller.timeshift.time.monotonic", return_value=100) as clock:
            await ts.prepare("live", "header", Path("/unused"), AsyncMock())
            self.assertEqual(ts.retry_delay, 120)
            clock.return_value = 219
            buffer.capture_health_at = clock.return_value
            await ts.update(Mock(), 1)
            self.assertEqual(ts.retry_delay, 120)
            buffer.capture_health["upstream_age"] = 6
            await ts.update(Mock(), 1)
            self.assertIsNone(ts.healthy_since)
            buffer.capture_health["upstream_age"] = 0
            clock.return_value = 220
            buffer.capture_health_at = clock.return_value
            await ts.update(Mock(), 1)
            clock.return_value = 340
            buffer.capture_health_at = clock.return_value
            await ts.update(Mock(), 1)
            self.assertEqual(ts.retry_delay, 30)
            self.assertIsNone(ts.failed_uuid)

    async def test_stale_health_snapshot_does_not_reset_backoff(self):
        ts = Timeshift()
        ts.retry_delay = 120
        ts.failed_uuid = CONFIG["uuid"]
        ts.healthy_since = 100
        ts.sample = AsyncMock()
        ts.buffer = Mock(error="", raw_error="", capture_failure={}, live_reader=True,
                         capture_health={"upstream_age": 0, "capture_write_age": 0},
                         capture_health_at=100)
        with patch("satip_controller.timeshift.storage", return_value=CONFIG), \
                patch("satip_controller.timeshift.time.monotonic", return_value=220):
            await ts.update(Mock(), 1)
        self.assertEqual(ts.retry_delay, 120)
        self.assertEqual(ts.failed_uuid, CONFIG["uuid"])
        self.assertIsNone(ts.healthy_since)

    def test_unchanged_index_does_not_refresh_health_observation(self):
        from satip_controller.state import atomic_json
        with tempfile.TemporaryDirectory() as temp:
            buffer = RollingBuffer()
            buffer.index = Path(temp) / "index.json"
            atomic_json(buffer.index, {"health": {"upstream_age": 0, "capture_write_age": 0}})
            with patch("satip_controller.rolling.time.monotonic", return_value=100) as clock:
                buffer.refresh()
                self.assertEqual(buffer.capture_health_at, 100)
                clock.return_value = 220
                buffer.refresh()
                self.assertEqual(buffer.capture_health_at, 100)

    async def test_new_channel_allows_retry_after_exhaustion(self):
        ts = Timeshift()
        ts.upstream = "old"
        ts.retry_delay = 180
        ts.failed_uuid = CONFIG["uuid"]
        buffer = Mock(config=CONFIG, start=AsyncMock(return_value=True), close=AsyncMock())
        with patch("satip_controller.timeshift.storage", return_value=CONFIG), \
                patch("satip_controller.rolling.RollingBuffer", return_value=buffer):
            await ts.prepare("new", "header", Path("/unused"), AsyncMock())
            self.assertIs(ts.buffer, buffer)
            self.assertEqual(ts.retry_delay, 30)

    async def test_different_storage_gets_a_fresh_retry_budget(self):
        ts = Timeshift()
        ts.retry_delay = 180
        ts.failed_uuid = "old-storage"
        buffer = Mock(config=CONFIG, start=AsyncMock(return_value=True), close=AsyncMock())
        with patch("satip_controller.timeshift.storage", return_value=CONFIG), \
                patch("satip_controller.rolling.RollingBuffer", return_value=buffer):
            await ts.prepare("live", "header", Path("/unused"), AsyncMock())
            self.assertIs(ts.buffer, buffer)
            self.assertEqual(ts.retry_delay, 30)

    async def test_failed_capture_has_bounded_backoff_then_stays_live(self):
        timeshift = Timeshift()
        with patch("satip_controller.timeshift.storage", return_value=CONFIG), patch("satip_controller.rolling.RollingBuffer") as rolling, patch("satip_controller.timeshift.time.monotonic", return_value=100) as clock:
            rolling.return_value.start = AsyncMock(side_effect=OSError("Capture failed"))
            rolling.return_value.close = AsyncMock()
            for delay in (30, 60, 120, None):
                now = clock.return_value
                self.assertEqual(await timeshift.prepare("live", "header", Path("/unused"), AsyncMock()), "live")
                self.assertEqual(timeshift.retry_after, now + delay if delay else None)
                if delay:
                    clock.return_value = timeshift.retry_after + 1
            clock.return_value = 100
            text = await timeshift.action(Mock(), "pause")
            self.assertIn("Preparing", text)
            self.assertEqual(timeshift.retry_after, 100)
            self.assertEqual(timeshift.retry_delay, 30)


class OnDemandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ts = Timeshift()
        self.ts.config = copy.deepcopy(CONFIG)
        items = [segment(0, 0, 10), segment(1, 10, 20)]
        self.ts.buffer = Mock(error="", disk_error="", origin=0, path="local-stream",
            segments=items, ram_segments=items, disk_segments=items, disk_process=None,
            activate=AsyncMock(), deactivate=AsyncMock(), disconnect=AsyncMock(), close=AsyncMock())
        self.ts.buffer.raw_error = ""
        self.ts.buffer.live_reader = True
        self.ts.buffer.live.return_value = "local-stream"
        def playback(*_):
            self.ts.buffer.live_reader = False
            return ("local-stream", 0)
        self.ts.buffer.playback.side_effect = playback
        self.ts.reload = AsyncMock()
        self.ipc = Mock(closed=False, command=AsyncMock())
        self.ipc.get = AsyncMock(side_effect=lambda name: {
            "pause": self.ts.paused, "time-pos": 5, "path": "local-stream", "seeking": False,
            "demuxer-cache-state": {"seekable-ranges": [{"start": 0, "end": 20}]}}[name])

    async def test_live_is_ram_only_and_seeking_does_not_start_usb(self):
        status = self.ts.status()
        self.assertEqual(status["mode"], "ram")
        self.assertTrue(status["available"])
        self.assertFalse(status["enabled"])
        self.assertFalse(status["usb_writing"])
        self.assertEqual(status["bytes"], 0)
        self.assertIn("inactive", await self.ts.action(self.ipc, "seek_back"))
        self.ts.buffer.activate.assert_not_awaited()
        self.ipc.command.assert_not_awaited()

    async def test_archive_cached_seek_does_not_reload_or_disconnect(self):
        self.ts.buffer.live_reader = False
        self.ts.buffer.origin = 10000
        await self.ts.jump(10015, self.ipc, precise=True)
        self.ipc.command.assert_awaited_once_with("seek", 15, "absolute+exact")
        self.ts.reload.assert_not_awaited()
        self.ts.buffer.disconnect.assert_not_awaited()

    async def test_archive_cache_miss_reopens_with_backward_cushion(self):
        self.ts.buffer.live_reader = False
        await self.ts.jump(30, self.ipc)
        self.ts.buffer.playback.assert_called_once_with(30)
        self.ts.reload.assert_awaited_once()

    async def test_raw_cache_does_not_bypass_archive_handover(self):
        await self.ts.jump(15, self.ipc, precise=True)
        self.ts.reload.assert_awaited_once()
        self.ts.buffer.playback.assert_called_once_with(0)

    async def test_first_pause_freezes_before_copy_and_does_not_reload(self):
        async def activate(position):
            self.ipc.command.assert_any_await("set_property", "pause", True)
            self.assertEqual(position, 5)
        self.ts.buffer.activate.side_effect = activate
        with patch("satip_controller.timeshift.storage", return_value=CONFIG):
            await self.ts.action(self.ipc, "pause")
            self.ts.reload.assert_not_awaited()
            self.ts.buffer.disconnect.assert_not_awaited()
            await self.ts.action(self.ipc, "pause")
            await self.ts.action(self.ipc, "pause")
        self.assertTrue(self.ts.active)
        self.assertTrue(self.ts.paused)
        self.ts.buffer.activate.assert_awaited_once()
        self.ts.reload.assert_awaited_once()

    async def test_return_live_stops_usb_before_reloading_ram(self):
        self.ts.active = self.ts.paused = True
        async def reload(*_):
            self.ts.buffer.deactivate.assert_awaited_once()
            self.assertFalse(self.ts.active)
        self.ts.reload.side_effect = reload
        await self.ts.action(self.ipc, "live")
        self.assertFalse(self.ts.active)
        self.assertFalse(self.ts.paused)
        self.assertEqual(self.ts.status()["mode"], "ram")
        self.assertEqual(self.ts.status()["bytes"], 0)
        self.ts.buffer.activate.assert_not_awaited()

    async def test_missing_usb_does_not_pause_or_promote(self):
        with patch("satip_controller.timeshift.storage", return_value=None):
            self.assertIn("USB", await self.ts.action(self.ipc, "pause"))
        self.assertFalse(self.ts.paused)
        self.ts.buffer.activate.assert_not_awaited()
        self.ipc.command.assert_not_awaited()

    async def test_failed_activation_restores_unpaused_live_stream(self):
        self.ts.buffer.activate.side_effect = OSError("USB too slow")
        with patch("satip_controller.timeshift.storage", return_value=CONFIG):
            self.assertIn("could not start", await self.ts.action(self.ipc, "pause"))
        self.assertFalse(self.ts.paused)
        self.assertFalse(self.ts.active)
        self.ipc.command.assert_any_await("set_property", "pause", False)
        self.ts.reload.assert_not_awaited()

    async def test_usb_loss_while_live_does_not_interrupt_ram_stream(self):
        with patch("satip_controller.timeshift.storage", return_value=None):
            self.assertIsNone(await self.ts.update(self.ipc, 123))
        self.assertIsNone(self.ts.config)
        self.ts.buffer.close.assert_not_awaited()
        with patch("satip_controller.timeshift.storage", return_value=CONFIG):
            self.assertIsNone(await self.ts.update(self.ipc, 123))
        self.assertEqual(self.ts.config, CONFIG)
        self.ts.buffer.activate.assert_not_awaited()

    async def test_copier_failure_keeps_specific_notice_not_usb_unavailable(self):
        self.ts.active = True
        self.ts.buffer.disk_error = "Time-shift capture fell behind – returning to live TV"
        self.ts.buffer.disk_failure = {"stage": "ram_segment", "errno": 2}
        with patch("satip_controller.timeshift.storage", return_value=CONFIG):
            self.assertEqual(await self.ts.update(self.ipc, 123), "storage")
        self.assertEqual(self.ts.notice, self.ts.buffer.disk_error)

    async def test_failed_close_retains_owned_handles(self):
        buffer = self.ts.buffer
        buffer.close.side_effect = OSError("Writer is still stopping")
        with self.assertRaises(OSError):
            await self.ts.close()
        self.assertIs(self.ts.buffer, buffer)


class RamPolicyTests(unittest.TestCase):
    def test_ram_retention_is_bounded_by_time_and_payload_bytes(self):
        from satip_controller.rolling import retained_ram, RAM_BYTES, RAM_SECONDS
        items = [segment(i, i * 2, i * 2 + 2, 1024**2) for i in range(30)]
        keep = retained_ram(items)
        self.assertLessEqual(keep[-1]["end"] - keep[0]["start"], RAM_SECONDS)
        self.assertLessEqual(sum(s["bytes"] for s in keep) + SEGMENT_BYTES, RAM_BYTES)
        large = [segment(i, i, i + 1, 16 * 1024**2) for i in range(20)]
        self.assertEqual(len(retained_ram(large)), 2)

    def test_ram_storage_rejects_an_ordinary_disk_and_normalizes_dotdot(self):
        from satip_controller.rolling import ram_storage
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp).resolve()
            with patch("satip_controller.rolling.Path.read_text", return_value="1 0 8:1 / / rw - ext4 /dev/root rw\n"):
                with self.assertRaises(OSError):
                    ram_storage(directory)
            (directory / "ram").mkdir()
            text = f"1 0 8:1 / / rw - ext4 /dev/root rw\n2 1 0:1 / {directory / 'ram'} rw - tmpfs tmpfs rw\n"
            with patch("satip_controller.rolling.Path.read_text", return_value=text):
                ram_storage(directory / "ram")
                with self.assertRaises(OSError):
                    ram_storage(directory / "ram" / "..")


class RawPassthroughTests(unittest.IsolatedAsyncioTestCase):
    def test_live_url_does_not_require_a_completed_segment(self):
        buffer = RollingBuffer()
        buffer.refresh = Mock()
        buffer.server = Mock(sockets=[Mock(getsockname=lambda: ("127.0.0.1", 12345))])
        self.assertTrue(buffer.live().endswith("/live.ts"))
        self.assertEqual(buffer.segments, [])

    async def test_start_returns_without_waiting_for_segment_index(self):
        with tempfile.TemporaryDirectory() as temp:
            buffer = RollingBuffer()
            process = Mock(pid=123456, returncode=None, wait=AsyncMock(return_value=0))
            process.stdin.drain = AsyncMock()
            with patch("satip_controller.rolling.storage", return_value=CONFIG), \
                 patch("satip_controller.rolling.ram_storage"), \
                 patch("satip_controller.rolling.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
                async with asyncio.timeout(2):
                    self.assertTrue(await buffer.start("upstream", "header", Path(temp)))
                self.assertFalse(buffer.index.exists())
                await buffer.close()

    async def test_paused_raw_queue_is_bounded_without_stalling_capture(self):
        buffer = RollingBuffer()
        buffer.live_paused = True
        buffer.raw_reader = Mock(read=AsyncMock(side_effect=[b"raw-bytes", b""]))
        writer = Mock()
        queue = asyncio.Queue(maxsize=1)
        queue.put_nowait(b"earlier")
        buffer.live_clients[writer] = queue
        await buffer.feed_raw()
        self.assertTrue(buffer.raw_overflow)
        self.assertLessEqual(queue.qsize(), 1)
        writer.transport.abort.assert_not_called()
        self.assertEqual(buffer.raw_preroll[0][1], b"raw-bytes")

    def test_absolute_timestamp_wrap_calibration(self):
        from satip_controller.rolling import PTS_WRAP, first_video_pts
        buffer = RollingBuffer()
        buffer.anchor = PTS_WRAP * 2 + 10
        buffer.calibrate(10)
        self.assertEqual(buffer.origin, PTS_WRAP * 2)
        value = int((PTS_WRAP - 1) * 90000)
        pts = bytes([0x21 | ((value >> 29) & 14), (value >> 22) & 255,
                     ((value >> 14) & 254) | 1, (value >> 7) & 255, ((value << 1) & 254) | 1])
        packet = bytearray([0x47, 0x40, 0, 0x10])
        packet.extend(b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + pts)
        packet.extend(bytes(188 - len(packet)))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "segment.ts"
            path.write_bytes(packet)
            self.assertAlmostEqual(first_video_pts(path, 1), -1, places=4)


class CaptureFeedTests(unittest.TestCase):
    def test_full_muxer_queue_never_blocks_live_pump_or_drops_silently(self):
        from satip_controller.rolling import CaptureFeed, RAW_QUEUE
        feed = CaptureFeed(Mock())
        for _ in range(RAW_QUEUE):
            self.assertTrue(feed.offer(b"transport packet"))
        self.assertFalse(feed.offer(b"overflow"))
        self.assertTrue(feed.failed.is_set())
        self.assertEqual(feed.pending.qsize(), RAW_QUEUE)
        self.assertFalse(feed.offer(b"later"))
        feed.source.write.assert_not_called()

    def test_blocked_pipe_writer_does_not_block_producer(self):
        import threading
        from satip_controller.rolling import CaptureFeed, RAW_QUEUE
        entered, release = threading.Event(), threading.Event()
        source = Mock()
        def blocked_write(data):
            entered.set()
            release.wait(2)
        source.write.side_effect = blocked_write
        feed = CaptureFeed(source)
        feed.offer(b"first")
        thread = threading.Thread(target=feed.run)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            for _ in range(RAW_QUEUE):
                self.assertTrue(feed.offer(b"next"))
            self.assertFalse(feed.offer(b"overflow"))
            self.assertTrue(feed.failed.is_set())
        finally:
            feed.closed.set()
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_writer_flushes_in_order_and_closes_its_owned_pipe(self):
        from satip_controller.rolling import CaptureFeed
        source = Mock()
        feed = CaptureFeed(source)
        feed.offer(b"first")
        feed.offer(b"second")
        source.flush.side_effect = [None, lambda: None]
        def flush():
            if source.write.call_count == 2:
                feed.closed.set()
        source.flush.side_effect = flush
        feed.run()
        self.assertEqual([c.args[0] for c in source.write.call_args_list], [b"first", b"second"])
        self.assertFalse(feed.failed.is_set())
        source.close.assert_called_once()

    def test_broken_muxer_pipe_marks_failure(self):
        from satip_controller.rolling import CaptureFeed
        source = Mock()
        source.write.side_effect = BrokenPipeError()
        feed = CaptureFeed(source)
        feed.offer(b"data")
        feed.run()
        self.assertTrue(feed.failed.is_set())
        source.close.assert_called_once()

    def test_relay_diagnostics_are_bounded_and_secret_free(self):
        buffer = RollingBuffer()
        buffer.capture_health = {"upstream_bytes": 10, "upstream_age": float("nan"),
                                 "capture_queue_chunks": "private", "path": "secret"}
        health = buffer.diagnostics()
        self.assertEqual(health["upstream_bytes"], 10)
        self.assertIsNone(health["upstream_age"])
        self.assertIsNone(health["capture_queue_chunks"])
        self.assertNotIn("path", health)


class AudioHandoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_reload_preselects_audio_without_discarding_its_cache(self):
        player = Player(SimpleNamespace(runtime_dir=Path("/unused")), SimpleNamespace())
        player.audio_id = 2
        player.current_channel = object()
        player.ipc = Mock(command=AsyncMock())
        player.select_audio = AsyncMock(return_value=True)
        await player.reload_buffer("local-archive", 0, True)
        calls = [c.args for c in player.ipc.command.await_args_list]
        self.assertNotIn(("set_property", "aid", "no"), calls)
        load = next(c for c in calls if c[0] == "loadfile")
        self.assertEqual(load[-1]["aid"], "2")
        self.assertEqual(calls[0], ("set_property", "pause", True))
        player.select_audio.assert_awaited_once_with(player.current_channel)
