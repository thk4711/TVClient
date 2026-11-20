"""Remote seeking and time-shift OSD policy; no real hardware access."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from satip_controller.controller import Controller
from satip_controller.input import binding, Policy, Inputs
from satip_controller.osd import OSD, timeshift_panel, elapsed_label
from satip_controller.timeshift import Timeshift
from tests.helpers import Fixture


class InputTimestampTests(unittest.IsolatedAsyncioTestCase):
    async def test_dropped_events_restore_actual_hold_without_replaying_presses(self):
        callback, on_press = AsyncMock(), AsyncMock(return_value=True)
        inputs = Inputs(SimpleNamespace(repeat_delay=.5, repeat_interval=.125, debounce=.08),
                        callback, on_press)

        async def stream():
            for kind, code, value, stamp in (
                (1, 115, 1, 10), (1, 115, 2, 10.6),
                (0, 3, 0, 11), (1, 164, 1, 11.1), (1, 115, 0, 11.2),
                (0, 0, 0, 11.3), (1, 115, 2, 11.5), (1, 115, 2, 11.9),
                (1, 115, 2, 13.4), (1, 115, 0, 13.5), (1, 115, 2, 14),
            ):
                yield SimpleNamespace(type=kind, code=code, value=value, timestamp=lambda t=stamp: t)

        device = SimpleNamespace(path="remote", async_read_loop=stream, active_keys=Mock(return_value=[115, 164]))
        await inputs._read(device, "remote")
        device.active_keys.assert_called_once_with()
        self.assertEqual([call.args[0] for call in callback.await_args_list],
                         ["remote_right", "remote_right", "remote_right", "remote_right_fast"])
        on_press.assert_awaited_once_with("remote_right")

    async def test_dropped_events_do_not_restore_a_released_key(self):
        callback = AsyncMock()
        inputs = Inputs(SimpleNamespace(repeat_delay=.5, repeat_interval=.125, debounce=.08), callback)

        async def stream():
            for kind, code, value, stamp in ((1, 115, 1, 10), (0, 3, 0, 11),
                                            (0, 0, 0, 11.1), (1, 115, 2, 12)):
                yield SimpleNamespace(type=kind, code=code, value=value, timestamp=lambda t=stamp: t)

        await inputs._read(SimpleNamespace(path="remote", async_read_loop=stream,
                                           active_keys=lambda: []), "remote")
        callback.assert_awaited_once_with("remote_right")

    async def test_long_native_hold_accelerates_using_kernel_time(self):
        callback = AsyncMock()
        inputs = Inputs(SimpleNamespace(repeat_delay=.5, repeat_interval=.125, debounce=.08), callback)

        async def stream():
            for value, stamp in [(1, 10), *[(2, 10.5 + i * .132) for i in range(150)], (0, 31)]:
                yield SimpleNamespace(type=1, code=114, value=value, timestamp=lambda t=stamp: t)

        with patch("satip_controller.input.time.monotonic", return_value=100):
            await inputs._read(SimpleNamespace(path="remote", async_read_loop=stream), "remote")
        self.assertEqual(callback.await_count, 151)
        self.assertEqual(callback.await_args_list[0].args, ("remote_left",))
        self.assertEqual(callback.await_args_list[-1].args, ("remote_left_fast",))

    async def test_queued_presses_use_kernel_timestamps_not_dispatch_time(self):
        callback = AsyncMock()
        inputs = Inputs(SimpleNamespace(repeat_delay=.5, repeat_interval=.125, debounce=.08), callback)
        events = []
        for i in range(5):
            for value, offset in ((1, 0), (0, .04)):
                stamp = 10 + i * .15 + offset
                events.append(SimpleNamespace(type=1, code=115, value=value, timestamp=lambda t=stamp: t))

        async def stream():
            for event in events:
                yield event

        device = SimpleNamespace(path="remote", async_read_loop=stream)
        with patch("satip_controller.input.time.monotonic", return_value=100):
            await inputs._read(device, "remote")
        self.assertEqual(callback.await_count, 5)
        self.assertTrue(all(call.args == ("remote_right",) for call in callback.await_args_list))

    async def test_queued_native_repeats_keep_event_spacing_and_filter_bounce(self):
        callback = AsyncMock()
        inputs = Inputs(SimpleNamespace(repeat_delay=.5, repeat_interval=.125, debounce=.08), callback)

        async def stream():
            for value, stamp in ((1, 10), (2, 10.2), (2, 10.6), (2, 10.75),
                                 (0, 10.8), (1, 10.85), (0, 10.86), (1, 10.87)):
                yield SimpleNamespace(type=1, code=115, value=value, timestamp=lambda t=stamp: t)

        with patch("satip_controller.input.time.monotonic", return_value=100):
            await inputs._read(SimpleNamespace(path="remote", async_read_loop=stream), "remote")
        self.assertEqual(callback.await_count, 4)


class RemoteTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_files()
        self.controller = Controller(self.config, self.player, self.state)
        self.controller.playing = self.controller.requested
        self.player.timeshift = Timeshift()
        self.player.timeshift.config = {"enabled": True}
        self.player.timeshift.active = True
        self.player.timeshift.paused = True
        self.player.timeshift.ranges = [(0, 100)]
        self.player.timeshift_action = AsyncMock(return_value="TV paused")
        self.controller.osd.timeshift = AsyncMock()
        self.player.ipc.get = AsyncMock(side_effect=lambda key: {"volume": 35, "mute": False}[key])

    async def asyncTearDown(self):
        if self.controller.seek_task:
            self.controller.seek_task.cancel()
            await asyncio.gather(self.controller.seek_task, return_exceptions=True)
        await self.controller.osd.close()
        await self.state.close()

    async def test_burst_accumulates_one_target_without_blocking_input(self):
        self.player.timeshift.position = 20
        for _ in range(5):
            await self.controller.action("remote_right")
        self.player.timeshift_action.assert_not_awaited()
        await self.controller.seek_task
        self.player.timeshift_action.assert_awaited_once_with("skip_forward", target=70)

    async def test_fast_remote_steps_are_three_times_larger_and_still_exact(self):
        self.player.timeshift.position = 40
        for action in ("remote_right_fast", "remote_left_fast", "remote_left"):
            await self.controller.action(action)
        await self.controller.seek_task
        self.player.timeshift_action.assert_awaited_once_with("skip_back", target=30)

    async def test_pending_target_survives_temporary_missing_reader_position(self):
        self.player.timeshift.position = 20
        await self.controller.action("remote_right")
        self.player.timeshift.position = None
        await self.controller.action("remote_right_fast")
        self.assertEqual(self.controller.seek_target, 60)
        await self.controller.seek_task
        self.player.timeshift_action.assert_awaited_once_with("skip_forward", target=60)

    async def test_hold_at_boundary_does_not_reload_the_same_frame(self):
        self.player.timeshift.position = 99.5
        for _ in range(20):
            await self.controller.action("remote_right_fast")
        self.assertIsNone(self.controller.seek_task)
        self.player.timeshift_action.assert_not_awaited()
        await self.controller.action("remote_left")
        await self.controller.seek_task
        self.player.timeshift_action.assert_awaited_once_with("skip_back", target=89.5)

    async def test_fast_remote_still_changes_normal_volume_when_playing(self):
        self.player.timeshift.paused = False
        await self.controller.action("remote_right_fast")
        self.player.ipc.command.assert_any_await("set_property", "volume", 40)
        self.player.timeshift_action.assert_not_awaited()

    async def test_fast_remote_still_changes_normal_volume_on_youtube(self):
        self.controller.youtube.active = True
        await self.controller.action("remote_left_fast")
        self.player.ipc.command.assert_any_await("set_property", "volume", 30)
        self.player.timeshift_action.assert_not_awaited()

    async def test_opposite_presses_accumulate_from_requested_not_stale_position(self):
        self.player.timeshift.position = 20
        for action in ("remote_right", "remote_right", "remote_left"):
            await self.controller.action(action)
        await self.controller.seek_task
        self.player.timeshift_action.assert_awaited_once_with("skip_back", target=30)

    async def test_each_pending_press_clamps_at_retained_boundary(self):
        self.player.timeshift.position = 95
        for action in ("remote_right", "remote_right", "remote_left"):
            await self.controller.action(action)
        await self.controller.seek_task
        self.player.timeshift_action.assert_awaited_once_with("skip_back", target=89.5)

    async def test_channel_change_discards_pending_seek(self):
        self.player.timeshift.position = 20
        await self.controller.action("remote_right")
        self.controller.request(self.controller.favourites[-1], force=True)
        await self.controller.seek_task
        self.player.timeshift_action.assert_not_awaited()

    async def test_return_live_discards_pending_seek(self):
        self.player.timeshift.position = 20
        await self.controller.action("remote_right")
        await self.controller.action("live")
        await self.controller.seek_task
        self.player.timeshift_action.assert_awaited_once_with("live")

    async def test_presses_during_seek_replace_one_pending_target(self):
        self.player.timeshift.position = 20
        entered, release = asyncio.Event(), asyncio.Event()

        async def seek(action, target=None):
            entered.set()
            await release.wait()

        self.player.timeshift_action.side_effect = seek
        await self.controller.action("remote_right")
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(4):
            await self.controller.action("remote_right")
        self.assertEqual(self.controller.seek_target, 70)
        release.set()
        await self.controller.seek_task
        self.assertEqual([call.kwargs["target"] for call in self.player.timeshift_action.await_args_list], [30, 70])

    async def test_seek_failure_clears_pending_work(self):
        from satip_controller.mpv_ipc import IPCError
        self.player.timeshift.position = 20
        self.player.timeshift_action.side_effect = IPCError("Unavailable")
        await self.controller.action("remote_right")
        await self.controller.seek_task
        self.assertIsNone(self.controller.seek_target)
        self.assertIsNone(self.controller.seek_requested)
        self.assertTrue(self.controller.wake.is_set())

    async def test_paused_remote_seeks_without_changing_volume(self):
        for action, target in (("remote_left", "skip_back"), ("remote_right", "skip_forward")):
            await self.controller.action(action)
            self.player.timeshift_action.assert_awaited_with(target)
        self.player.ipc.command.assert_not_awaited()
        self.assertEqual(self.state.data["volume"], 35)

    async def test_playing_and_inactive_remote_adjust_volume(self):
        for enabled, paused in ((True, False), (False, False)):
            self.player.timeshift.config = {} if not enabled else {"enabled": True}
            self.player.timeshift.active = enabled
            self.player.timeshift.paused = paused
            for action, volume in (("remote_left", 30), ("remote_right", 40)):
                await self.controller.action(action)
                self.player.ipc.command.assert_any_await("set_property", "volume", volume)
        self.player.timeshift_action.assert_not_awaited()

    async def test_keyboard_volume_stays_volume_while_paused(self):
        await self.controller.action("volume_up")
        self.player.ipc.command.assert_any_await("set_property", "volume", 40)
        self.player.timeshift_action.assert_not_awaited()

    async def test_channel_switch_clears_timeline_before_loading(self):
        self.controller.boot_splash.enabled = False
        await self.controller.action("next")
        self.controller.osd.timeshift.assert_awaited_with({"enabled": False})

    async def test_youtube_remote_does_not_stop_or_seek(self):
        self.controller.youtube.active = True
        self.controller.youtube.local_stop = AsyncMock()
        self.assertTrue(await self.controller.local_press("remote_right"))
        self.controller.youtube.local_stop.assert_not_awaited()
        await self.controller.action("remote_right")
        self.player.timeshift_action.assert_not_awaited()
        self.player.ipc.command.assert_any_await("set_property", "volume", 40)

    def test_remote_codes_and_native_repeats(self):
        for code in (105, 114):
            self.assertEqual(binding("remote", code), "remote_left")
            self.assertEqual(binding("keyboard", code), "volume_down")
        for code in (106, 115):
            self.assertEqual(binding("remote", code), "remote_right")
            self.assertEqual(binding("keyboard", code), "volume_up")
        policy = Policy()
        self.assertEqual(policy.feed("r", "remote", 115, 1, 0), "remote_right")
        self.assertIsNone(policy.feed("r", "remote", 115, 2, .3))
        self.assertEqual(policy.feed("r", "remote", 115, 2, .5), "remote_right")

    def test_acceleration_resets_on_release_and_direction_change(self):
        policy = Policy()
        policy.feed("r", "remote", 115, 1, 0)
        self.assertEqual(policy.feed("r", "remote", 115, 2, 2), "remote_right_fast")
        policy.feed("r", "remote", 115, 0, 2.1)
        self.assertEqual(policy.feed("r", "remote", 115, 1, 2.2), "remote_right")
        self.assertEqual(policy.feed("r", "remote", 115, 2, 2.8), "remote_right")
        self.assertEqual(policy.feed("r", "remote", 115, 2, 4.3), "remote_right_fast")
        self.assertEqual(policy.feed("r", "remote", 114, 1, 4.4), "remote_left")
        self.assertEqual(policy.feed("r", "remote", 114, 2, 5), "remote_left")
        self.assertIsNone(policy.feed("r", "remote", 115, 2, 5.1))

    def test_keyboard_volume_does_not_accelerate(self):
        policy = Policy()
        policy.feed("k", "keyboard", 115, 1, 0)
        self.assertEqual(policy.feed("k", "keyboard", 115, 2, 10), "volume_up")


class SeekTests(unittest.IsolatedAsyncioTestCase):
    async def test_consecutive_exact_seeks_refresh_position_and_keep_pause(self):
        ts = Timeshift()
        ts.config = {"enabled": True}
        ts.active = True
        ts.paused = True
        ts.position = 999  # Stale telemetry must not determine the target.
        position = 90
        commands = []

        async def command(*args):
            nonlocal position
            commands.append(args)
            if args[0] == "seek":
                position = args[1]

        ipc = Mock(command=AsyncMock(side_effect=command))
        ipc.get = AsyncMock(side_effect=lambda key: {
            "pause": True, "time-pos": position, "seeking": False,
            "demuxer-cache-state": {"seekable-ranges": [{"start": 0, "end": 100}]},
        }[key])
        await ts.action(ipc, "skip_back")
        await ts.action(ipc, "skip_back")
        await ts.action(ipc, "skip_forward")
        self.assertEqual(commands, [("seek", 80, "absolute+exact"),
                                    ("seek", 70, "absolute+exact"),
                                    ("seek", 80, "absolute+exact")])
        self.assertTrue(ts.paused)
        self.assertEqual(ts.position, 80)
        self.assertEqual(ts.status()["behind_live_seconds"], 20)

    async def test_short_range_clamping_and_empty_buffer(self):
        ts = Timeshift()
        ts.config = {"enabled": True}
        ts.active = True
        ipc = Mock(command=AsyncMock())
        ipc.get = AsyncMock(side_effect=lambda key: {
            "pause": True, "time-pos": 5, "seeking": False,
            "demuxer-cache-state": {"seekable-ranges": [{"start": 4, "end": 6}]},
        }[key])
        await ts.action(ipc, "skip_back")
        ipc.command.assert_awaited_with("seek", 4, "absolute+exact")
        await ts.action(ipc, "skip_forward")
        ipc.command.assert_awaited_with("seek", 5.5, "absolute+exact")
        ipc.get = AsyncMock(side_effect=lambda key: {"pause": True, "time-pos": None,
            "demuxer-cache-state": {}}[key])
        ipc.command.reset_mock()
        self.assertIn("Building", await ts.action(ipc, "skip_back"))
        ipc.command.assert_not_awaited()


class OverlayTests(unittest.IsolatedAsyncioTestCase):
    async def test_volume_priority_and_dismiss_cleanup(self):
        player = SimpleNamespace(ipc=Mock(closed=False, command=AsyncMock()))
        osd = OSD(player)
        self.addAsyncCleanup(osd.close)
        status = {"enabled": True, "paused": True, "available_seconds": 100, "behind_live_seconds": 20}
        await osd.timeshift(status, reveal=True)
        timeline = player.ipc.command.await_args.args[0]
        await osd.volume(35, False)
        volume = player.ipc.command.await_args.args[0]
        self.assertGreater(volume["z"], timeline["z"])
        self.assertIn("−00:20", osd.timeshift_data)
        await osd.dismiss()
        self.assertIsNone(osd.timeshift_data)
        self.assertEqual(player.ipc.command.await_args.args[0]["id"], 3)
        self.assertEqual(player.ipc.command.await_args.args[0]["format"], "none")

    async def test_pause_is_persistent_playing_expires_and_inactive_clears(self):
        player = SimpleNamespace(ipc=Mock(closed=False, command=AsyncMock()))
        osd = OSD(player, 3000)
        self.addAsyncCleanup(osd.close)
        status = {"enabled": True, "paused": True, "available_seconds": 180, "behind_live_seconds": 135}
        with patch("satip_controller.osd.time.monotonic", return_value=10):
            await osd.timeshift(status, reveal=True)
        with patch("satip_controller.osd.time.monotonic", return_value=100):
            await osd.timeshift(status)
        self.assertIn("−02:15", osd.timeshift_data)
        status["paused"] = False
        with patch("satip_controller.osd.time.monotonic", return_value=100):
            await osd.timeshift(status, reveal=True)
        with patch("satip_controller.osd.time.monotonic", return_value=104):
            await osd.timeshift(status)
        self.assertEqual(osd.timeshift_data, "")
        status["enabled"] = False
        status["paused"] = True
        await osd.timeshift(status)
        self.assertEqual(osd.timeshift_data, "")

    def test_panel_labels_and_buffer_building(self):
        self.assertEqual(elapsed_label(3661), "1:01:01")
        data = timeshift_panel({"paused": True, "available_seconds": 180, "behind_live_seconds": 135}, "de")
        self.assertIn("TV pausiert", data)
        self.assertIn("−02:15", data)
        self.assertIn("−03:00", data)
        self.assertIn("Live", data)
        self.assertIn("Building", timeshift_panel({"paused": True, "available_seconds": 0, "behind_live_seconds": 0}))
