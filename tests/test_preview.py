from satip_controller.state import lease_data, lease_active
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from satip_controller.controller import Controller
from satip_controller.config import library
from satip_controller.preview import Preview, PreviewError, ffmpeg_arguments
from satip_controller.scan import Manager
from satip_controller.state import atomic_json, read_json, lease_active, runtime_path
from satip_controller.input import Inputs
from tests.helpers import Fixture


class PreviewTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_files()
        self.backend = AsyncMock()
        self.backend.grid.return_value = []
        self.manager = Manager(self.backend, self.web_config)
        self.preview = Preview(self.manager, str(self.config.credentials))
        self.channels, _ = library(self.data)
        self.channel = next(iter(self.channels.values()))

    async def asyncSetUp(self):
        await self.preview.recover()

    async def asyncTearDown(self):
        await self.preview.stop()
        await self.state.close()

    async def test_codec_and_main_audio_policy_no_secrets(self):
        for channel in self.channels.values():
            args = ffmpeg_arguments(channel, self.root)
            self.assertIn(f'0:i:{channel.audio_pid}', args)
            self.assertIn('aac', args)
            self.assertIn('copy' if channel.video == 'h264' else 'libx264', args)
            self.assertNotIn('sensitive-test-password', ' '.join(args))
            self.assertNotIn('http', ' '.join(args))

    async def test_pause_resume_and_scan_independence(self):
        c = Controller(self.config, self.player, self.state)
        self.preview.lease(15)
        await c.sync_management()
        self.assertTrue(c.paused)
        self.player.stop_stream.assert_awaited_once()
        self.preview.lease(0)
        await c.sync_management()
        self.assertFalse(c.paused)
        atomic_json(self.config.management_dir / 'scan-lease.json', {'until': 0, 'recovery_pending': True})
        self.preview.lease(0)
        await c.sync_management()
        self.assertTrue(c.paused)

    async def test_disabled_unmapped_and_busy_rejected(self):
        disabled = Preview(self.manager)
        with self.assertRaises(PreviewError):
            await disabled.start(self.channel.service_uuid)
        with self.assertRaises(ValueError):
            await self.preview.start('a'*32)
        await self.manager.lock.acquire()
        try:
            with self.assertRaises(PreviewError):
                await self.preview.start(self.channel.service_uuid)
        finally:
            self.manager.lock.release()
        atomic_json(self.manager.journal, {'uuid': 'c'*32})
        with self.assertRaises(PreviewError):
            await self.preview.start(self.channel.service_uuid)

    async def test_spawn_failure_releases_lease_and_lock(self):
        with patch('satip_controller.preview.asyncio.create_subprocess_exec', side_effect=OSError):
            await self.preview.start(self.channel.service_uuid)
            await self.preview.task
        self.assertEqual(self.preview.status['state'], 'error')
        self.assertFalse(self.manager.lock.locked())
        self.assertFalse(lease_active(read_json(self.preview.lease_path)))
        self.assertFalse(self.preview.directory.exists())

    async def test_cancel_while_waiting_for_tuner(self):
        async def quiet():
            self.preview.lease(15)
            await asyncio.sleep(30)
        self.preview.quiet = quiet
        result = await self.preview.start(self.channel.service_uuid)
        await asyncio.sleep(.01)
        with self.assertRaises(PreviewError):
            await self.preview.stop('f'*32)
        await self.preview.stop(result['session'])
        self.assertFalse(self.manager.lock.locked())
        self.assertFalse(self.preview.directory.exists())
        self.assertFalse(lease_active(read_json(self.preview.lease_path)))

    async def test_session_and_media_allowlist(self):
        async def quiet():
            await asyncio.sleep(30)
        self.preview.quiet = quiet
        result = await self.preview.start(self.channel.service_uuid)
        await asyncio.sleep(.01)
        with self.assertRaises(PreviewError):
            self.preview.touch('f'*32)
        self.preview.touch(result['session'])
        with self.assertRaises(ValueError):
            self.preview.media(result['session'], '../login.json')
        with self.assertRaises(FileNotFoundError):
            self.preview.media(result['session'], 'index.m3u8')
        (self.preview.directory / 'index.m3u8').write_text('#EXTM3U')
        self.assertEqual(self.preview.media(result['session'], 'index.m3u8').name, 'index.m3u8')
        await self.preview.stop(result['session'])
        with self.assertRaises(PreviewError):
            self.preview.media(result['session'], 'index.m3u8')

    async def test_immediate_and_concurrent_stop(self):
        result = await self.preview.start(self.channel.service_uuid)
        await asyncio.gather(self.preview.stop(result['session']), self.preview.stop(result['session']))
        self.assertFalse(self.manager.lock.locked())

    async def test_timeout_reaps_encoder_and_releases_tuner(self):
        process = AsyncMock()
        process.returncode = None
        process.stdin = unittest.mock.Mock()
        process.terminate = unittest.mock.Mock()
        self.preview.idle_timeout = 0
        with patch('satip_controller.preview.asyncio.create_subprocess_exec', return_value=process):
            await self.preview.start(self.channel.service_uuid)
            await self.preview.task
        process.terminate.assert_called_once()
        process.wait.assert_awaited()
        self.assertFalse(self.manager.lock.locked())
        self.assertEqual(self.preview.status['state'], 'stopped')

    async def test_live_stream_survives_ten_minutes_and_media_keeps_it_alive(self):
        clock = [100.0]
        process = AsyncMock()
        process.returncode = None
        process.stdin = unittest.mock.Mock()
        process.terminate = unittest.mock.Mock()
        async def feed(*args):
            await asyncio.Event().wait()
        self.preview.feed = feed
        self.preview.quiet = AsyncMock()
        with patch('satip_controller.preview.time', SimpleNamespace(monotonic=lambda: clock[0])), \
                patch('satip_controller.preview.asyncio.create_subprocess_exec', return_value=process):
            result = await self.preview.start(self.channel.service_uuid)
            (self.preview.directory / 'index.m3u8').write_text('#EXTM3U')
            await asyncio.sleep(.05)
            clock[0] += 3600
            self.preview.media(result['session'], 'index.m3u8')
            await asyncio.sleep(.55)
            self.assertTrue(self.preview.active)
            self.assertEqual(self.preview.status['state'], 'ready')
            clock[0] += 21
            await asyncio.wait_for(asyncio.shield(self.preview.task), 2)
            self.assertFalse(self.manager.lock.locked())
            process.terminate.assert_called_once()

    async def test_physical_key_cancels_startup_then_runs_mapped_action(self):
        controller = Controller(self.config, self.player, self.state)
        async def quiet():
            await asyncio.Event().wait()
        self.preview.quiet = quiet
        await self.preview.start(self.channel.service_uuid)
        await controller.sync_management()
        self.assertTrue(controller.paused)
        self.assertTrue(await asyncio.wait_for(controller.local_press(), 2))
        self.assertFalse(controller.paused)
        self.assertFalse(self.manager.lock.locked())
        self.assertIn('used locally', self.preview.status['reason'])
        await controller.action('next')
        self.assertNotEqual(controller.requested, self.data['favourites'][0])
        # A previous request cannot terminate the next explicitly started session.
        await self.preview.start(self.channel.service_uuid)
        await asyncio.sleep(.15)
        self.assertTrue(self.preview.active)

    async def test_tuner_remains_leased_until_encoder_cleanup_finishes(self):
        process = AsyncMock()
        process.returncode = None
        process.stdin = unittest.mock.Mock()
        process.terminate = unittest.mock.Mock()
        cleanup = asyncio.Event()
        process.wait.side_effect = cleanup.wait
        async def feed(*args):
            await asyncio.Event().wait()
        self.preview.feed = feed
        self.preview.quiet = AsyncMock()
        with patch('satip_controller.preview.asyncio.create_subprocess_exec', return_value=process):
            await self.preview.start(self.channel.service_uuid)
            await asyncio.sleep(.05)
            atomic_json(self.preview.takeover_path, {'generation': self.preview.generation})
            await asyncio.sleep(.2)
            process.terminate.assert_called_once()
            self.assertTrue(lease_active(read_json(self.preview.lease_path)))
            self.assertTrue(self.manager.lock.locked())
            cleanup.set()
            await asyncio.gather(self.preview.task, return_exceptions=True)
            self.assertFalse(lease_active(read_json(self.preview.lease_path)))
            self.assertFalse(self.preview.directory.exists())

    async def test_unmapped_key_and_key_release_input_policy(self):
        events = [SimpleNamespace(type=1, code=30, value=value) for value in (1, 2, 0)]
        async def event_stream():
            for event in events:
                yield event
        device = SimpleNamespace(path='keyboard', async_read_loop=event_stream)
        action, press = AsyncMock(), AsyncMock(return_value=True)
        inputs = Inputs(self.config, action, press)
        await inputs._read(device, 'keyboard')
        press.assert_awaited_once()
        action.assert_not_awaited()

    async def test_buildroot_takeover_is_player_owned_status_not_management(self):
        with patch.dict('os.environ', {'SATIP_RUNTIME_ROOT': str(self.root / 'run')}):
            self.assertEqual(runtime_path(self.config.state_dir, 'local-takeover.json'),
                             self.root / 'run/satip-status/local-takeover.json')
