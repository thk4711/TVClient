"""Startzustände, echte Sperrübergabe und Fehlergrenzen des Startbildschirms."""
import asyncio
import errno
import fcntl
import os
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from satip_appliance import splash
from satip_controller.splash import BootSplash, bar_bounds
from satip_controller.controller import Controller
from satip_controller.mpv_ipc import IPCError
from satip_controller.state import atomic_json
from tests.helpers import Fixture
from tests.test_buildroot_foundation import load, BOARD


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "run").mkdir()
        (self.root / "data/store").mkdir(parents=True)

    def state(self, installation, factory=False):
        (self.root / "run/satip-data-ready").touch()
        atomic_json(self.root / "data/store/installation.json", {"state": installation})
        if factory:
            (self.root / "data/store/factory-ready.json").touch()

    def test_setup_requires_actual_web_listener_and_address(self):
        self.state("unconfigured", True)
        self.assertEqual(splash.snapshot(self.root, 5, None, True, True), (4, ""))
        self.assertEqual(splash.snapshot(self.root, 5, "192.168.1.42", True, False), (26, ""))
        self.assertEqual(splash.snapshot(self.root, 5, "192.168.1.42", True, True),
                         (5, "http://192.168.1.42:8080/"))

    def test_failure_and_unqualified_import_never_show_ready(self):
        self.state("imported-unqualified")
        self.assertEqual(splash.snapshot(self.root, 5, "192.168.1.42", True, True)[0], 8)
        (self.root / "run/satip-data-failed").touch()
        self.assertEqual(splash.snapshot(self.root, 5, "192.168.1.42", True, True)[0], 6)

    def test_backend_wait_and_slow_boot_are_not_completion(self):
        self.assertEqual(splash.snapshot(self.root, 61, None, False, False)[0], 21)
        self.state("configured")
        self.assertEqual(splash.snapshot(self.root, 31, "192.168.1.42", False, True)[0], 9)
        self.assertEqual(splash.snapshot(self.root, 31, "192.168.1.42", True, True)[0], 27)
        stage = self.root / "run/satip-reception-stage"
        for value, expected in (("network", 27), ("server", 27), ("tuner", 29), ("ready", 35)):
            stage.write_text(value)
            self.assertEqual(splash.snapshot(self.root, 0, "192.168.1.42", True, True)[0], expected)

    def test_configuration_failure_is_not_reported_as_a_disk_problem(self):
        (self.root / "run/satip-data-failed").touch()
        (self.root / "run/satip-boot-error").write_text("config-duplicate:12\n")
        self.assertEqual(splash.snapshot(self.root, 90, None, False, False), (12, "duplicate:12"))
        (self.root / "run/satip-boot-error").write_text("config-value:12-private-password\n")
        self.assertEqual(splash.snapshot(self.root, 90, None, False, False), (6, ""))

    def test_corrupt_status_is_visible_and_does_not_freeze_progress(self):
        self.state("configured")
        (self.root / "data/store/installation.json").write_text("private broken json")
        self.assertEqual(splash.snapshot(self.root, 1, None, False, False), (23, ""))
        (self.root / "data/store/installation.json").unlink()
        self.assertEqual(splash.snapshot(self.root, 1, None, False, False), (23, ""))

    def test_timeout_deadlines_reset_per_phase_and_recover(self):
        self.state("unconfigured", True)
        status = splash.StartupStatus()
        self.assertEqual(status.update(self.root, 0, None, False, False), (4, ""))
        self.assertEqual(status.update(self.root, 61, None, False, False), (19, ""))
        # Delayed DHCP does not immediately declare a backend failure.
        self.assertEqual(status.update(self.root, 120, "192.168.1.42", False, False), (2, ""))
        self.assertEqual(status.update(self.root, 151, "192.168.1.42", False, False), (9, ""))
        self.assertEqual(status.update(self.root, 160, "192.168.1.42", True, False), (26, ""))
        self.assertEqual(status.update(self.root, 191, "192.168.1.42", True, False), (18, ""))
        self.assertEqual(status.update(self.root, 200, "192.168.1.42", True, True),
                         (5, "http://192.168.1.42:8080/"))
        # Waiting for a human to complete setup is not a boot timeout.
        self.assertEqual(status.update(self.root, 900, "192.168.1.42", True, True)[0], 5)
        self.state("configured")
        (self.root / "run/satip-reception-stage").write_text("ready")
        self.assertEqual(status.update(self.root, 901, "192.168.1.42", True, True)[0], 35)
        self.assertEqual(status.update(self.root, 962, "192.168.1.42", True, True)[0], 20)

    def test_errors_distinguish_mount_initialization_and_recovery(self):
        for code, state in (("data-mount", 13), ("data-init", 14), ("data-prepare", 15),
                            ("backend-init", 16), ("recovery", 17), ("devices", 25),
                            ("service-web", 18), ("service-tvheadend", 9), ("service-player", 20)):
            (self.root / "run/satip-boot-error").write_text(code)
            detail = "supervisor" if code.startswith("service-") else ""
            self.assertEqual(splash.snapshot(self.root, 90, None, False, False), (state, detail))

    def test_monitor_sends_error_even_when_display_config_is_invalid(self):
        directory = self.root / "run/splash"
        directory.mkdir()
        (self.root / "boot").mkdir()
        (self.root / "boot/satip-config.txt").write_bytes(b"\xffprivate")
        (self.root / "run/satip-boot-error").write_text("config-file:0")
        with patch.object(splash, "command") as command, \
                patch.object(splash, "address", return_value=None), \
                patch.object(splash, "listening", return_value=False), \
                patch.object(splash.time, "sleep", side_effect=lambda _: (directory / "finished").touch()):
            splash.monitor(self.root, directory)
        self.assertEqual(command.call_args_list[-1].args, ("STATE 12 file:0", directory))

    def test_display_preferences_exclude_secrets_and_use_actual_configuration(self):
        (self.root / "boot").mkdir()
        (self.root / "boot/satip-config.txt").write_text(
            "wifi_psk=privat\ndrm_connector=HDMI-A-1\ndrm_mode=1280x720@50\n")
        self.assertEqual(splash.display_settings(self.root), "DISPLAY HDMI-A-1 1280x720@50")
        directory = self.root / "data/store/config/player"
        directory.mkdir(parents=True)
        atomic_json(directory / "config.json", {"drm_connector": "HDMI-A-2", "drm_mode": "1920x1080@60"})
        self.state("configured")
        self.assertEqual(splash.display_settings(self.root), "DISPLAY HDMI-A-2 1920x1080@60")
        atomic_json(directory / "config.json", {"drm_connector": "HDMI-A-2\nSTOP"})
        self.assertEqual(splash.display_settings(self.root), "DISPLAY HDMI-A-2 1920x1080@50")

    def test_release_waits_until_other_process_finishes_cleanup(self):
        directory = self.root / "run/splash"
        directory.mkdir()
        code = '''
import fcntl, pathlib, sys, time
p = pathlib.Path(sys.argv[1])
with (p / 'lock').open('a') as f:
    fcntl.flock(f, fcntl.LOCK_EX)
    print('bereit', flush=True)
    while not (p / 'finished').exists(): time.sleep(.01)
    time.sleep(.1)
    (p / 'released').touch()
'''
        process = subprocess.Popen([sys.executable, "-c", code, str(directory)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "bereit")
            splash.release(directory)
            self.assertTrue((directory / "released").exists())
            self.assertEqual(process.wait(timeout=2), 0)
            splash.release(directory)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()

    def test_blocked_renderer_has_bounded_handoff(self):
        directory = self.root / "run/splash"
        directory.mkdir()
        with (directory / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaises(TimeoutError):
                splash.release(directory, timeout=.05)
        self.assertTrue((directory / "finished").exists())

    def test_retained_display_fd_is_non_inheritable_and_never_claims_master(self):
        (self.root / "device").write_text("/dev/dri/card1\n")
        for result in (None, OSError(errno.EINVAL, "Not master")):
            with patch.object(splash.os, "open", return_value=42) as opened, \
                    patch.object(splash.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFCHR)), \
                    patch.object(splash.fcntl, "ioctl", side_effect=result) as ioctl:
                self.assertEqual(splash.hold_display(self.root), 42)
                self.assertTrue(opened.call_args.args[1] & os.O_CLOEXEC)
                self.assertTrue(opened.call_args.args[1] & os.O_NOFOLLOW)
                ioctl.assert_called_once_with(42, 0x641f, 0)
        with patch.object(splash.os, "open", return_value=42), \
                patch.object(splash.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFCHR)), \
                patch.object(splash.fcntl, "ioctl", side_effect=OSError(errno.EIO, "Failed")), \
                patch.object(splash.os, "close") as closed:
            self.assertIsNone(splash.hold_display(self.root))
            closed.assert_called_once_with(42)
        (self.root / "device").write_text("/etc/passwd")
        with patch.object(splash.os, "open") as opened:
            self.assertIsNone(splash.hold_display(self.root))
            opened.assert_not_called()

    def test_handoff_timeout_closes_supervisors_hold(self):
        directory = self.root / "run/splash"
        directory.mkdir()
        held = os.open(self.root / "held", os.O_CREAT | os.O_RDWR, 0o600)
        with (directory / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with patch.object(splash, "hold_display", return_value=held):
                with self.assertRaises(TimeoutError):
                    splash.release(directory, timeout=.03, keep_open=True)
        with self.assertRaises(OSError):
            os.fstat(held)

    def test_early_init_is_idempotent_and_before_services(self):
        module = load("install_splash", BOARD / "install-splash.py")
        (self.root / "etc/init.d").mkdir(parents=True)
        (self.root / "usr/sbin").mkdir(parents=True)
        path = self.root / "etc/inittab"
        path.write_text("::sysinit:/bin/mount -a\n::sysinit:/etc/init.d/rcS\n")
        for name in ("etc/init.d/S01satip-splash", "usr/sbin/satip-boot-splash", "usr/sbin/satip-hdmi-prepare"):
            (self.root / name).touch()
        module.install(self.root)
        first = path.read_bytes()
        module.install(self.root)
        self.assertEqual(path.read_bytes(), first)
        self.assertLess(first.index(b"satip-boot-splash"), first.index(b"rcS"))
        self.assertEqual((self.root / "usr/sbin/satip-boot-splash").stat().st_mode & 0o777, 0o755)


class PlayerSplashTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ipc = SimpleNamespace(closed=False, get=AsyncMock(return_value={"w": 1280, "h": 720}), command=AsyncMock())
        self.player = SimpleNamespace(ipc=self.ipc, config=SimpleNamespace(runtime_dir=self.root, drm_mode="1920x1080@50"))
        self.splash = BootSplash(self.player, enabled=True)

    async def render(self, *args, **kwargs):
        width, height = map(int, args[2:4])
        if args[1] == "--bar":
            top, bottom = bar_bounds(width, height)
            height = bottom-top
        kwargs["stdout"].write(b"\xff" * width * height * 4)
        kwargs["stdout"].flush()
        return SimpleNamespace(returncode=0, wait=AsyncMock(return_value=0))

    async def asyncTearDown(self):
        await self.splash.hide(finished=True)

    async def test_same_renderer_uses_actual_dimensions_and_replaces_inode(self):
        with patch("satip_controller.splash.asyncio.create_subprocess_exec", side_effect=self.render):
            await self.splash.show()
            path = self.root / "startbild.bgra"
            inode = path.stat().st_ino
            with path.open("rb") as previous:
                await self.splash.show(11)
                self.assertNotEqual(path.stat().st_ino, inode)
                self.assertEqual(os.fstat(previous.fileno()).st_size, 1280*720*4)
            self.ipc.command.assert_any_await("overlay-add", 7, 0, 0, str(path), 0, "bgra", 1280, 720, 5120)
            await self.splash.hide(finished=True)
            self.assertFalse(path.exists())
            self.ipc.command.assert_awaited_with("overlay-remove", 7)
            count = self.ipc.command.await_count
            await self.splash.show()
            self.assertEqual(self.ipc.command.await_count, count)

    async def test_renderer_failure_leaves_playback_available(self):
        with patch("satip_controller.splash.asyncio.create_subprocess_exec", side_effect=OSError("fehlt")):
            await self.splash.show()
        self.assertIsNone(self.splash.shown)
        self.ipc.command.assert_not_awaited()
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_language_change_redraws_same_state_and_detail(self):
        with patch("satip_controller.splash.asyncio.create_subprocess_exec", side_effect=self.render) as render:
            await self.splash.show(11, "load_failed")
            self.assertEqual(render.call_args.args[-3], "en")
            await self.splash.set_language("de")
            self.assertEqual(render.call_args.args[-5:-2], ("11", "load_failed", "de"))
            count = render.call_count
            await self.splash.show(11, "load_failed")
            self.assertEqual(render.call_count, count)
            await self.splash.set_language("en")
            self.assertEqual(render.call_args.args[-3], "en")
            await self.splash.hide(finished=True)
            await self.splash.set_language("de")
            self.assertEqual(render.call_count, count + 1)

    async def test_cancellation_reaps_native_renderer(self):
        waiting = asyncio.Event()
        async def wait():
            waiting.set()
            await asyncio.Future()
        process = SimpleNamespace(returncode=None, wait=wait)
        def kill():
            process.returncode = -9
            process.wait = AsyncMock(return_value=-9)
        process.kill = kill
        with patch("satip_controller.splash.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            task = asyncio.create_task(self.splash.show())
            await asyncio.wait_for(waiting.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.wait.assert_awaited_once()
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_pause_hides_without_disabling_and_old_ipc_is_not_used(self):
        with patch("satip_controller.splash.asyncio.create_subprocess_exec", side_effect=self.render):
            await self.splash.show()
            await self.splash.hide()
            self.assertTrue(self.splash.enabled)
            await self.splash.show()
            self.player.ipc = SimpleNamespace(closed=False, command=AsyncMock())
            before = self.ipc.command.await_count
            await self.splash.hide(finished=True)
            self.assertEqual(self.ipc.command.await_count, before)
            self.player.ipc.command.assert_not_awaited()

    async def test_progress_waits_for_milestones_then_shows_100_before_removing(self):
        with patch("satip_controller.splash.asyncio.create_subprocess_exec", side_effect=self.render) as render:
            await self.splash.show(35)
            await asyncio.sleep(.25)
            self.assertEqual(self.splash.progress, 80)
            await self.splash.show(36)
            await asyncio.sleep(.65)
            self.assertEqual(self.splash.progress, 94)
            self.assertTrue(self.splash.enabled)
            self.assertTrue(any(c.args[1] == "--bar" for c in render.call_args_list))
            self.assertEqual((self.root / "startbalken.bgra").stat().st_size, 1280 * 3 * 4)
            await self.splash.complete()
            self.assertIsNotNone(self.splash.shown)
            self.assertFalse(self.splash.enabled)
            async with asyncio.timeout(2):
                while self.splash.shown:
                    await asyncio.sleep(.02)
            completed = [c for c in render.call_args_list if c.args[4] == "37" and c.args[-2] == "100"]
            self.assertGreaterEqual(len(completed), 2)
            self.assertEqual(list(self.root.iterdir()), [])
            self.ipc.command.assert_awaited_with("overlay-remove", 7)

    async def test_retry_keeps_progress_and_warning_freezes_it(self):
        with patch("satip_controller.splash.asyncio.create_subprocess_exec", side_effect=self.render):
            await self.splash.show(36)
            await asyncio.sleep(.65)
            await self.splash.show(31)
            await asyncio.sleep(.25)
            self.assertEqual(self.splash.progress, 94)
            await self.splash.show(11, "load_failed")
            await asyncio.sleep(.45)
            self.assertEqual(self.splash.progress, 94)
            self.assertTrue(self.splash.animation.done())
            await self.splash.show(3)
            self.assertFalse(self.splash.animation.done())
            await self.splash.hide()
            self.assertIsNone(self.splash.animation)
            self.assertEqual(list(self.root.iterdir()), [])

    async def test_completion_failure_and_new_channel_do_not_leave_an_overlay(self):
        with patch("satip_controller.splash.asyncio.create_subprocess_exec", side_effect=self.render):
            await self.splash.show()
            await self.splash.complete()
            await self.splash.hide()
            self.assertFalse(self.splash.enabled)
            self.assertIsNone(self.splash.animation)
            self.assertEqual(list(self.root.iterdir()), [])
        self.splash.enabled = True
        with patch("satip_controller.splash.asyncio.create_subprocess_exec", side_effect=self.render):
            await self.splash.show()
        with patch("satip_controller.splash.asyncio.create_subprocess_exec", side_effect=OSError("failed")):
            await self.splash.complete()
        self.assertIsNone(self.splash.shown)
        self.assertFalse(self.splash.enabled)


class StartupRetryTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_files()
        self.controller = Controller(self.config, self.player, self.state)
        self.controller.startup_began = 100
        self.controller.reason = "load_failed"

    async def asyncTearDown(self):
        await self.state.close()

    async def test_startup_cap_has_fixed_monotonic_window(self):
        controller = self.controller
        for enabled in (True, False):
            controller.boot_splash.enabled = enabled
            self.assertEqual([controller.retry_delay(delay, 120) for delay in (1, 2, 4, 8, 30)],
                             [1, 2, 3, 3, 3])
        self.assertEqual(controller.retry_delay(6, 159.99), 3)
        self.assertEqual(controller.retry_delay(6, 160), 6)
        self.assertEqual(controller.retry_delay(30, 200), 30)
        controller.request(controller.requested, force=True)
        self.assertEqual(controller.startup_began, 100)
        self.assertEqual(controller.retry_delay(30, 200), 30)

    async def test_non_reception_failures_and_post_playback_keep_normal_backoff(self):
        for reason in ("audio_pid_missing", "device_or_storage_unavailable", "playback_stalled"):
            self.controller.reason = reason
            self.assertEqual(self.controller.retry_delay(16, 120), 16)
        self.controller.reason = "load_failed"
        self.controller.first_playback = True
        self.assertEqual(self.controller.retry_delay(16, 120), 16)

    async def test_retry_attempt_keeps_connecting_artwork(self):
        controller = self.controller
        controller.boot_splash.enabled = True
        controller.boot_splash.shown = (self.player.ipc, (27, "", "en"))
        controller.boot_splash.show = AsyncMock()
        with patch("satip_controller.controller.time.monotonic", return_value=120):
            await controller.show_switching(controller.channels[controller.requested])
            await controller.show_switching(controller.channels[controller.requested])
        self.assertEqual([call.args for call in controller.boot_splash.show.await_args_list], [(27,), (27,)])

    async def test_text_fallback_waits_without_channel_reload_banner(self):
        controller = self.controller
        controller.boot_splash.enabled = False
        controller.language = "de"
        controller.osd.show = AsyncMock()
        with patch("satip_controller.controller.time.monotonic", return_value=120):
            await controller.show_switching(controller.channels[controller.requested])
        self.assertEqual(controller.osd.show.await_args.args[0], "Warten auf SAT-IP-Tuner …")
        self.assertGreater(controller.osd.show.await_args.args[1], self.config.load_timeout * 1000)

    async def test_expired_startup_wait_keeps_actionable_warning_artwork(self):
        controller = self.controller
        controller.boot_splash.enabled = True
        controller.boot_splash.shown = (self.player.ipc, (11, "load_failed", "en"))
        controller.boot_splash.show = AsyncMock()
        with patch("satip_controller.controller.time.monotonic", return_value=160):
            await controller.show_switching(controller.channels[controller.requested])
        controller.boot_splash.show.assert_awaited_once_with(11, "load_failed")

    async def test_controller_retries_with_cap_and_without_reloading_frame(self):
        controller = self.controller
        controller.startup_began = time.monotonic()
        controller.reason = None
        controller.boot_splash.enabled = True
        controller.boot_splash.shown = (self.player.ipc, (3, "", "en"))
        controller.boot_splash.show = AsyncMock()
        self.player.load = AsyncMock(side_effect=IPCError("Tuner rejected stream", reason="load_failed"))
        with self.assertLogs("satip_controller.controller", level="WARNING") as logs:
            task = asyncio.create_task(controller.run())
            try:
                async with asyncio.timeout(12):
                    while controller.retry_count < 4:
                        await asyncio.sleep(.01)
                self.assertTrue(any("retry 3 in 3 seconds" in line for line in logs.output))
                self.assertTrue(any("retry 4 in 3 seconds" in line for line in logs.output))
                states = [call.args[0] for call in controller.boot_splash.show.await_args_list if call.args]
                self.assertTrue(states)
                self.assertEqual(set(states), {27})
                self.assertIsNone(controller.error)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


class PlaybackHandoffTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_files()

    async def asyncTearDown(self):
        await self.state.close()

    async def test_window_opens_after_overlay_attempt_even_when_overlay_is_disabled(self):
        for enabled in (True, False):
            events = []
            self.player.ipc.closed = True
            async def start():
                events.append("start")
                self.player.ipc.closed = False
            self.player.start = AsyncMock(side_effect=start)
            self.player.show_window = AsyncMock(side_effect=lambda: events.append("window"))
            self.player.sample = AsyncMock(return_value=None)
            controller = Controller(self.config, self.player, self.state)
            controller.boot_splash.enabled = enabled
            controller.boot_splash.show = AsyncMock(side_effect=lambda *a: events.append("overlay"))
            task = asyncio.create_task(controller.run())
            try:
                async with asyncio.timeout(2):
                    while "window" not in events:
                        await asyncio.sleep(.01)
                self.assertEqual(events[:3], ["start", "overlay", "window"])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_overlay_remains_until_position_advances_and_audio_is_selected(self):
        controller = Controller(self.config, self.player, self.state)
        controller.boot_splash.enabled = True
        controller.boot_splash.shown = (self.player.ipc, 3)
        controller.boot_splash.show = AsyncMock()
        controller.boot_splash.hide = AsyncMock()
        controller.boot_splash.complete = AsyncMock()
        position = 0
        advance = False
        audio = False
        async def sample(channel):
            nonlocal position
            if advance:
                position += .2
            return position
        self.player.sample = AsyncMock(side_effect=sample)
        self.player.select_audio = AsyncMock(side_effect=lambda channel: audio)
        self.player.ipc.get = AsyncMock(side_effect=lambda key: 35 if key == "volume" else False)
        task = asyncio.create_task(controller.run())
        try:
            async with asyncio.timeout(3):
                while self.player.sample.await_count < 3:
                    await asyncio.sleep(.01)
                controller.boot_splash.hide.assert_not_awaited()
                controller.boot_splash.complete.assert_not_awaited()
                advance = True
                before = self.player.sample.await_count
                while self.player.sample.await_count < before + 3:
                    await asyncio.sleep(.01)
                controller.boot_splash.hide.assert_not_awaited()
                controller.boot_splash.complete.assert_not_awaited()
                audio = True
                while not controller.playing:
                    await asyncio.sleep(.01)
                controller.boot_splash.complete.assert_awaited_once_with()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
