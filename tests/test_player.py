import unittest
import os
from unittest.mock import AsyncMock, Mock, patch

from satip_controller.config import Channel
from satip_controller.mpv_ipc import IPCError
from satip_controller.player import Player, BWDIF, LIVE_BUFFER_OPTIONS, SD_FILTERS, url, arguments
from tests.helpers import Fixture
from satip_controller.switching import SwitchExperiments
from satip_controller.state import atomic_json


class PlayerTests(Fixture,unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Unit loads must not discover the Pi's real USB marker/open streams.
        storage = patch('satip_controller.timeshift.storage', return_value=None)
        storage.start()
        self.addCleanup(storage.stop)
        self.setup_files();self.real=Player(self.config,self.state)
        self.real.ipc=self.player.ipc
        self.real.ipc.begin_load = Mock()
        self.real.ipc.load_failure = False
        self.real.process=type('Process',(),{'returncode':None})()
    async def asyncTearDown(self): await self.state.close()

    async def test_splash_start_defers_window_until_overlay_can_be_queued(self):
        self.real.close = AsyncMock()
        with patch.dict(os.environ, {"SATIP_BOOT_SPLASH": "1"}), \
                patch("satip_controller.player.asyncio.create_subprocess_exec", AsyncMock(return_value=self.real.process)) as spawn, \
                patch("satip_controller.player.MPVIPC.connect", AsyncMock(return_value=self.real.ipc)):
            await self.real.start()
        self.assertIn("--force-window=no", spawn.call_args.args)
        self.assertIn("--drm-draw-plane=overlay", spawn.call_args.args)
        self.assertIn("--drm-drmprime-video-plane=primary", spawn.call_args.args)
        self.real.ipc.command.assert_not_awaited()
        await self.real.show_window()
        self.real.ipc.command.assert_awaited_once_with("set_property", "force-window", "immediate")
        await self.real.show_window()
        self.assertEqual(self.real.ipc.command.await_count, 1)

    async def test_normal_start_keeps_existing_window_behaviour(self):
        self.real.close = AsyncMock()
        with patch.dict(os.environ, {"SATIP_BOOT_SPLASH": "0"}), \
                patch("satip_controller.player.asyncio.create_subprocess_exec", AsyncMock(return_value=self.real.process)) as spawn, \
                patch("satip_controller.player.MPVIPC.connect", AsyncMock(return_value=self.real.ipc)):
            await self.real.start()
        self.assertIn("--force-window=yes", spawn.call_args.args)
        await self.real.show_window()
        self.real.ipc.command.assert_not_awaited()

    def test_software_fallback_only_after_unresponsive_ipc(self):
        self.real.recover("load_timeout")
        self.assertEqual(self.real.hwdec, "v4l2m2m-copy")
        self.real.recover("ipc_error")
        self.assertEqual(self.real.hwdec, "v4l2m2m-copy")
        with self.assertLogs("satip_controller.player", level="WARNING"):
            self.real.recover("ipc_timeout")
        self.assertEqual(self.real.hwdec, "no")
        self.real.recover("ipc_timeout")
        self.assertIn("--hwdec=no", arguments(self.config, self.state.data, self.real.hwdec))
        self.assertIn("--hwdec=v4l2m2m-copy", arguments(self.config, self.state.data))

    async def test_main_audio_src_id(self):
        channel=Channel.parse(self.data['channels'][0])
        self.real.ipc.get=AsyncMock(return_value=[self.live_audio(id=2, **{'src-id':5103,'visual-impaired':True}),
            self.live_audio(id=1, selected=False, **{'src-id':5102})])
        self.assertTrue(await self.real.select_audio(channel))
        self.real.ipc.command.assert_awaited_with('set_property','aid',1)

    async def test_already_selected_main_audio_does_not_reset_decoder(self):
        channel = Channel.parse(self.data["channels"][0])
        self.real.ipc.get = AsyncMock(return_value=[
            self.live_audio(id=1, **{"src-id": channel.audio_pid})])
        self.real.ipc.command.reset_mock()
        self.assertTrue(await self.real.select_audio(channel))
        self.real.ipc.command.assert_not_awaited()

    async def test_missing_audio_never_ready(self):
        self.real.ipc.get=AsyncMock(return_value=[])
        self.assertFalse(await self.real.select_audio(Channel.parse(self.data['channels'][0])))

    def experiments(self, **flags):
        atomic_json(self.config.runtime_dir / 'switch-experiments.json', flags)

    async def test_saved_audio_seed_remains_paused_until_live_verification(self):
        channel = Channel.parse(self.data['channels'][0])
        self.real.stop_stream = AsyncMock()
        self.experiments(saved_audio=True)
        await self.real.load(channel)
        self.assertNotIn('aid', self.real.ipc.command.call_args.args[-1])
        self.real.ipc.get = AsyncMock(side_effect=[
            [self.live_audio(id=7, **{'src-id': channel.audio_pid})], 1.0])
        self.assertTrue(await self.real.select_audio(channel))
        await self.real.load(channel)
        self.assertEqual(self.real.ipc.command.call_args.args[-1]['aid'], '7')
        self.assertTrue(self.real.initial_audio_pending)
        # A stale mpv ID must not be trusted even when selected.
        self.real.ipc.get = AsyncMock(side_effect=[
            [self.live_audio(id=7, lang='eng'),
             self.live_audio(id=3, selected=False, **{'src-id': channel.audio_pid})], 1.0])
        self.assertTrue(await self.real.select_audio(channel))
        self.real.ipc.command.assert_any_await('set_property', 'aid', 3)
        self.assertFalse(self.real.initial_audio_pending)
        self.assertEqual(self.real.saved_audio[self.real.audio_cache_key], 3)

    async def test_saved_audio_invalid_tracks_stay_paused_and_evict_hint(self):
        channel = Channel.parse(self.data['channels'][0])
        self.real.audio_cache_key = (channel, 'stereo', False)
        self.real.saved_audio[self.real.audio_cache_key] = 7
        self.real.initial_audio_pending = True
        self.real.ipc.get = AsyncMock(return_value=[self.live_audio(id=7, lang='eng')])
        self.assertFalse(await self.real.select_audio(channel))
        self.assertTrue(self.real.initial_audio_pending)
        self.assertNotIn(self.real.audio_cache_key, self.real.saved_audio)
        self.real.ipc.command.assert_awaited_once_with('set_property', 'aid', 'no')

    async def test_probe_options_are_live_file_local_and_opt_in(self):
        channel = Channel.parse(self.data['channels'][0])
        self.real.stop_stream = AsyncMock()
        self.experiments(bounded_probe=True)
        await self.real.load(channel)
        self.assertEqual(self.real.ipc.command.call_args.args[-1]['demuxer-lavf-probesize'], '131072')
        self.experiments()
        await self.real.load(channel)
        self.assertNotIn('demuxer-lavf-probesize', self.real.ipc.command.call_args.args[-1])

    async def test_live_load_builds_initial_cushion_without_global_option_writes(self):
        self.real.stop_stream = AsyncMock()
        for channel in self.data['channels']:
            self.real.ipc.command.reset_mock()
            await self.real.load(Channel.parse(channel))
            options = self.real.ipc.command.call_args.args[-1]
            for name, value in LIVE_BUFFER_OPTIONS.items():
                self.assertEqual(options[name], value)
            self.assertFalse(any(c.args[0] == 'set_property' and c.args[1] in LIVE_BUFFER_OPTIONS
                                 for c in self.real.ipc.command.call_args_list))
            self.assertTrue(self.real.initial_audio_pending)
            self.real.ipc.command.assert_any_await('set_property', 'pause', True)

    async def test_live_cushion_does_not_change_history_or_youtube_startup(self):
        self.real.select_audio = AsyncMock(return_value=True)
        await self.real.reload_buffer('http://127.0.0.1/reader.ts', 0, True)
        options = self.real.ipc.command.call_args_list[-2].args[-1]
        self.assertNotIn('cache-pause-initial', options)
        self.assertNotIn('cache-pause-wait', options)
        self.real.stop_stream = AsyncMock()
        await self.real.load_youtube('http://127.0.0.1:12345/' + 'a' * 48 + '/video', None)
        options = self.real.ipc.command.call_args.args[-1]
        self.assertEqual(options['cache-pause-wait'], '3')
        self.assertEqual(options['demuxer-readahead-secs'], '20')

    async def test_probe_limits_do_not_leak_into_reader_handover(self):
        self.real.switch_experiments = SwitchExperiments(bounded_probe=True)
        self.real.current_channel = Channel.parse(self.data['channels'][0])
        self.real.select_audio = AsyncMock(return_value=True)
        await self.real.reload_buffer('http://127.0.0.1/reader.ts', 0, True)
        options = next(c.args[-1] for c in self.real.ipc.command.call_args_list
                       if c.args[0] == 'loadfile')
        self.assertNotIn('demuxer-lavf-probesize', options)
        self.assertNotIn('demuxer-lavf-analyzeduration', options)

    async def test_saved_hint_is_not_reused_after_catalogue_policy_change(self):
        channel = Channel.parse(self.data['channels'][0])
        self.real.stop_stream = AsyncMock()
        self.experiments(saved_audio=True)
        await self.real.load(channel)
        self.real.saved_audio[self.real.audio_cache_key] = 7
        changed = Channel.parse({**self.data['channels'][0], 'audio_pid': channel.audio_pid + 1})
        await self.real.load(changed)
        self.assertNotIn('aid', self.real.ipc.command.call_args.args[-1])

    async def test_redundant_properties_skip_only_successful_unchanged_writes(self):
        self.real.switch_experiments = SwitchExperiments(redundant_ipc=True)
        await self.real.tv_property('vf', [])
        await self.real.tv_property('vf', [])
        self.assertEqual(self.real.ipc.command.await_count, 1)
        await self.real.tv_property('vf', SD_FILTERS)
        self.assertEqual(self.real.ipc.command.await_count, 2)
        self.real.ipc.command.side_effect = IPCError('rejected')
        with self.assertRaises(IPCError):
            await self.real.tv_property('vf', [])
        self.assertEqual(self.real.tv_properties['vf'], SD_FILTERS)

    async def test_redundant_ipc_youtube_invalidates_tv_properties(self):
        channel = Channel.parse(self.data['channels'][2])
        self.real.stop_stream = AsyncMock()
        self.experiments(redundant_ipc=True)
        await self.real.load(channel)
        self.real.ipc.command.reset_mock()
        await self.real.load(channel)
        self.assertFalse(any(c.args[:2] == ('set_property', 'vf')
                             for c in self.real.ipc.command.call_args_list))
        await self.real.load_youtube('http://127.0.0.1:12345/' + 'a' * 48 + '/video', None)
        self.real.ipc.command.reset_mock()
        await self.real.load(channel)
        self.real.ipc.command.assert_any_await('set_property', 'vf', SD_FILTERS)

    def live_audio(self, **changes):
        return {"type": "audio", "id": 2, "src-id": 5322, "codec": "mp2", "lang": "deu",
                "visual-impaired": False, "hearing-impaired": False, "external": False,
                "selected": True, **changes}

    async def test_stale_pid_uses_unique_live_normal_german_mp2_without_decoder_reset(self):
        channel = Channel.parse({**self.data["channels"][0], "audio_pid": 5332})
        self.real.ipc.get = AsyncMock(return_value=[self.live_audio(),
            self.live_audio(id=3, **{"src-id": 5323, "visual-impaired": True}),
            self.live_audio(id=4, **{"src-id": 5326, "codec": "ac3"})])
        self.real.ipc.command.reset_mock()
        with self.assertLogs("satip_controller.player", level="WARNING") as logs:
            self.assertTrue(await self.real.select_audio(channel))
        self.assertIn("5332", logs.output[0])
        self.assertIn("5322", logs.output[0])
        self.assertEqual(channel.audio_pid, 5332)  # No persistent library mutation.
        self.real.ipc.command.assert_not_awaited()

    async def test_live_fallback_selects_unselected_track(self):
        self.real.ipc.get = AsyncMock(return_value=[self.live_audio(selected=False)])
        with self.assertLogs("satip_controller.player", level="WARNING"):
            self.assertTrue(await self.real.select_audio(Channel.parse(self.data["channels"][0])))
        self.real.ipc.command.assert_awaited_with("set_property", "aid", 2)

    async def test_live_fallback_fails_closed_for_unqualified_or_ambiguous_audio(self):
        channel = Channel.parse(self.data["channels"][0])
        for changes in ({"visual-impaired": True}, {"hearing-impaired": True}, {"external": True},
                        {"codec": "flac"}, {"lang": "eng"}, {"src-id": 0}, {"src-id": "5322"},
                        {"visual-impaired": None}, {"hearing-impaired": None}, {"external": None}):
            with self.subTest(changes=changes):
                self.real.ipc.get = AsyncMock(return_value=[self.live_audio(**changes)])
                self.assertFalse(await self.real.select_audio(channel))
        self.real.ipc.get = AsyncMock(return_value=[self.live_audio(), self.live_audio(id=3, **{"src-id": 5328})])
        self.assertFalse(await self.real.select_audio(channel))
        self.real.ipc.command.assert_any_await("set_property", "aid", "no")

    async def test_saved_pid_still_wins_over_live_fallback(self):
        channel = Channel.parse(self.data["channels"][0])
        self.real.ipc.get = AsyncMock(return_value=[self.live_audio(),
            self.live_audio(id=1, **{"src-id": channel.audio_pid})])
        self.assertTrue(await self.real.select_audio(channel))
        self.real.ipc.command.assert_not_awaited()

    async def test_live_fallback_supports_demux_id_and_requires_metadata(self):
        channel = Channel.parse(self.data["channels"][0])
        track = self.live_audio()
        track["demux-id"] = track.pop("src-id")
        self.real.ipc.get = AsyncMock(return_value=[track])
        with self.assertLogs("satip_controller.player", level="WARNING"):
            self.assertTrue(await self.real.select_audio(channel))
        for field in ("codec", "lang", "visual-impaired", "hearing-impaired", "external"):
            self.real.ipc.get = AsyncMock(return_value=[{k: v for k, v in track.items() if k != field}])
            self.assertFalse(await self.real.select_audio(channel))

    async def test_filters_and_old_stream_release(self):
        self.real.stop_stream=AsyncMock()
        await self.real.load(Channel.parse(self.data['channels'][2]))
        self.real.stop_stream.assert_awaited_once()
        self.real.ipc.command.assert_any_await('set_property','vf',SD_FILTERS)
        self.real.ipc.command.assert_any_await("set_property", "hwdec", "no")
        await self.real.load(Channel.parse(self.data['channels'][0]))
        self.real.ipc.command.assert_any_await('set_property','vf',[])
        self.real.ipc.command.assert_any_await("set_property", "hwdec", "v4l2m2m-copy")

    def test_sd_picture_processing_preserves_motion_aspect_and_chroma(self):
        graph = SD_FILTERS[0]["params"]["graph"]
        self.assertEqual(graph.split(",")[0], BWDIF[0]["params"]["graph"])
        self.assertEqual(graph.split(",")[1], "unsharp=5:5:0.35:5:5:0")
        self.assertEqual(graph.split(",")[2],
                         "scale=w=trunc(720*dar/2)*2:h=720:flags=bicubic")
        self.assertEqual(graph.split(",")[3], "setsar=1")
        # Square-pixel 720p is 960 wide for 4:3 and 1280 for anamorphic 16:9.
        for dar, width in ((4 / 3, 960), (16 / 9, 1280)):
            self.assertEqual(int(720 * dar / 2) * 2, width)

    def test_sd_processing_keeps_fast_gpu_renderer(self):
        argv = arguments(self.config, self.state.data)
        self.assertIn("--gpu-dumb-mode=yes", argv)
        self.assertIn("--scale=bilinear", argv)
        self.assertIn("--interpolation=no", argv)

    async def test_sd_hd_sd_switch_reapplies_picture_processing(self):
        self.real.stop_stream = AsyncMock()
        sd = Channel.parse(self.data["channels"][2])
        hd = Channel.parse(self.data["channels"][0])
        for channel, filters in ((sd, SD_FILTERS), (hd, []), (sd, SD_FILTERS)):
            self.real.ipc.command.reset_mock()
            await self.real.load(channel)
            self.real.ipc.command.assert_any_await("set_property", "vf", filters)

    async def test_youtube_clears_sd_filters_and_tv_restores_them(self):
        self.real.stop_stream = AsyncMock()
        self.real.tv_header = "Authorization: Basic private"
        sd = Channel.parse(self.data["channels"][2])
        await self.real.load(sd)
        self.real.ipc.command.reset_mock()
        await self.real.load_youtube("http://127.0.0.1:12345/" + "a" * 48 + "/video", None)
        self.real.ipc.command.assert_any_await("set_property", "vf", [])
        self.real.ipc.command.reset_mock()
        await self.real.load(sd)
        self.real.ipc.command.assert_any_await("set_property", "vf", SD_FILTERS)

    async def test_sample_matching_path(self):
        channel=Channel.parse(self.data['channels'][0])
        self.real.ipc.get=AsyncMock(side_effect=[url(channel),False,'H264','MP2',3.5])
        self.assertEqual(await self.real.sample(channel),3.5)

    async def test_stale_path_rejected(self):
        self.real.ipc.get=AsyncMock(return_value='previous')
        self.assertIsNone(await self.real.sample(Channel.parse(self.data['channels'][0])))

    async def test_idle_not_ready(self):
        channel=Channel.parse(self.data['channels'][0])
        self.real.ipc.get=AsyncMock(side_effect=[url(channel),True])
        self.assertIsNone(await self.real.sample(channel))

    async def test_explicit_load_failure_does_not_wait_for_timeout(self):
        self.real.ipc.load_failure = True
        with self.assertRaises(IPCError) as error:
            await self.real.sample(Channel.parse(self.data['channels'][0]))
        self.assertEqual(error.exception.reason, "load_failed")

    async def test_supervised_load_identity_and_failure_feedback(self):
        from satip_controller.tuning import TuneFeedback
        with patch.dict(os.environ, {"SATIP_GENERATION": "test",
                                     "SATIP_TUNE_STATUS": str(self.config.runtime_dir / "feedback")}):
            self.real.tuning = TuneFeedback(self.config.runtime_dir)
        channel = Channel.parse(self.data["channels"][0])
        self.real.stop_stream = AsyncMock()
        await self.real.load(channel)
        first = self.real.ipc.command.call_args.args[-1]["user-agent"]
        await self.real.load(channel)
        second = self.real.ipc.command.call_args.args[-1]["user-agent"]
        self.assertNotEqual(first, second)
        with patch.object(self.real.tuning, "failed", return_value=True):
            with self.assertRaises(IPCError) as error:
                await self.real.sample(channel)
        self.assertEqual(error.exception.reason, "load_failed")
