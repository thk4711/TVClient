import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

from satip_controller.audio import pcm_channels, output_layouts, choose_track, layout_label
from satip_controller.backend import candidate
from satip_controller.config import Channel
from satip_controller.controller import Controller
from satip_controller.player import Player, arguments
from satip_controller.osd import banner
from tests.helpers import Fixture


def edid(channels=8, rates=4, bits=1, codec=1):
    base = bytearray(128)
    base[:8] = bytes.fromhex("00ffffffffffff00")
    base[126] = 1
    base[127] = -sum(base) % 256
    block = bytearray(128)
    block[:8] = bytes([2, 3, 8, 64, 35, codec * 8 + channels - 1, rates, bits])
    block[127] = -sum(block) % 256
    return bytes(base + block)


def track(pid=42, **changes):
    return {"type": "audio", "id": pid, "src-id": pid, "codec": "mp2", "lang": "deu",
            "visual-impaired": False, "hearing-impaired": False, "external": False,
            "demux-channel-count": 2, "selected": False, **changes}


class CapabilityTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup_files()

    def test_pcm_format_and_invalid_data(self):
        self.assertEqual(pcm_channels(edid()), 8)
        self.assertEqual(pcm_channels(edid(6)), 6)
        for data in (b"", edid()[:-1], edid() + bytes(128), edid(rates=2),
                     edid(bits=2), edid(codec=2), bytes(256)):
            self.assertEqual(pcm_channels(data), 2)
        invalid = bytearray(edid())
        invalid[130] = 5  # data block exceeds CTA data collection
        invalid[255] = 0
        invalid[255] = -sum(invalid[128:]) % 256
        self.assertEqual(pcm_channels(invalid), 2)

    def test_selected_connector_and_custom_route(self):
        device = self.root / "card3"
        device.touch()
        port = self.root / "card3-HDMI-A-2"
        port.mkdir()
        (port / "status").write_text("connected")
        (port / "edid").write_bytes(edid())
        config = replace(self.config, drm_device=str(device))
        route = "alsa/hdmi:CARD=vc4hdmi1,DEV=0"
        layouts = output_layouts(config, "HDMI-A-2", route, self.root)
        self.assertIn("7.1", layouts)
        self.assertIn("5.1", layouts)
        self.assertIn("stereo", layouts)
        self.assertEqual(output_layouts(config, "HDMI-A-1", route, self.root), "stereo")
        self.assertEqual(output_layouts(config, "HDMI-A-2", "alsa/usb", self.root), "stereo")
        (port / "status").write_text("disconnected")
        self.assertEqual(output_layouts(config, "HDMI-A-2", route, self.root), "stereo")

    def test_player_whitelist_and_volume_policy(self):
        with patch("satip_controller.player.output_layouts", return_value="7.1,5.1,stereo"):
            argv = arguments(self.config, self.state.data)
        for option in ("--audio-channels=7.1,5.1,stereo", "--audio-spdif=",
                       "--ad-lavc-downmix=no", "--aid=no", "--volume-max=100"):
            self.assertIn(option, argv)

    def test_channel_catalogue_accepts_normal_german_dolby(self):
        service = {"uuid": "a" * 32, "multiplex_uuid": "b" * 32,
                   "enabled": True, "encrypted": False}
        video = {"type": "H264"}
        dolby = {"type": "AC3", "pid": 43, "language": "ger", "audio_type": 0}
        self.assertEqual(candidate(service, [video, dolby])["audio_pid"], 43)
        for change in ({"language": "eng"}, {"audio_type": 3}):
            self.assertIsNone(candidate(service, [video, {**dolby, **change}]))


class SelectionTests(unittest.TestCase):
    def test_surround_preference_and_stereo_saved_preference(self):
        main = track()
        dolby = track(43, codec="ac3", **{"demux-channel-count": 6})
        self.assertIs(choose_track([main, dolby], 42, True), dolby)
        self.assertIs(choose_track([main, dolby], 42, False), main)
        self.assertIs(choose_track([dolby], 42, False), dolby)
        dolby["demux-channel-count"] = 2
        self.assertIs(choose_track([main, dolby], 42, True), dolby)

    def test_saved_pid_does_not_bypass_language_or_accessibility(self):
        for changes in ({"lang": "eng"}, {"lang": None}, {"visual-impaired": True},
                        {"hearing-impaired": True}, {"external": True}, {"codec": "flac"}):
            self.assertIsNone(choose_track([track(**changes)], 42, True))
        self.assertIsNone(choose_track([track(43), track(44)], 42, False))
        for language in ("de", "deu", "ger"):
            self.assertIsNotNone(choose_track([track(lang=language)], 42, False))

    def test_audio_labels(self):
        self.assertEqual(layout_label({"channel-count": 6, "channels": "5.1(side)"}), "5.1")
        self.assertEqual(layout_label({"channel-count": 2}), "Stereo")
        self.assertIsNone(layout_label({"channel-count": "6"}))
        panel = banner("Channel\nNow — 20:00\nNext — 21:00", audio="Deutsch · 5.1 → Stereo")
        self.assertIn("Deutsch · 5.1 → Stereo", panel)
        self.assertIn("pos(1820,873)", panel)
        self.assertIn("pos(1160,897)", panel)


class PlayerAudioTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        storage = patch('satip_controller.timeshift.storage', return_value=None)
        storage.start()
        self.addCleanup(storage.stop)
        self.setup_files()
        self.real = Player(self.config, self.state)
        self.real.ipc = self.player.ipc
        self.real.ipc.begin_load = Mock()
        self.channel = Channel.parse(self.data["channels"][0])

    async def asyncTearDown(self):
        await self.state.close()

    async def test_no_foreign_audio_even_at_saved_pid(self):
        self.real.ipc.get = AsyncMock(return_value=[track(self.channel.audio_pid, lang="eng", selected=True)])
        self.assertFalse(await self.real.select_audio(self.channel))
        self.real.ipc.command.assert_awaited_once_with("set_property", "aid", "no")

    async def test_surround_transition_and_selected_track_not_reset(self):
        self.real.audio_layouts = "5.1,stereo"
        dolby = track(43, codec="ac3", **{"demux-channel-count": 6})
        self.real.ipc.get = AsyncMock(return_value=[track(self.channel.audio_pid), dolby])
        self.assertTrue(await self.real.select_audio(self.channel))
        self.real.ipc.command.assert_awaited_once_with("set_property", "aid", 43)
        dolby["selected"] = True
        self.real.ipc.command.reset_mock()
        self.assertTrue(await self.real.select_audio(self.channel))
        self.real.ipc.command.assert_not_awaited()

    async def test_actual_decoded_and_output_layouts_in_overlay(self):
        self.real.ipc.get = AsyncMock(side_effect=[
            [track(selected=True)], {"channel-count": 6, "channels": "5.1"}, {"channel-count": 2}])
        await self.real.refresh_audio_info()
        self.assertEqual(self.real.audio_label(), "Deutsch · 5.1 → Stereo")
        self.real.ipc.get = AsyncMock(side_effect=[
            [track(selected=True)], {"channel-count": 6, "channels": "5.1"},
            {"channel-count": 6, "channels": "5.1(side)"}])
        await self.real.refresh_audio_info()
        self.assertEqual(self.real.audio_label(), "Deutsch · 5.1")

    async def test_tv_load_probes_audio_while_paused_until_policy_verification(self):
        self.real.stop_stream = AsyncMock()
        with patch("satip_controller.player.output_layouts", return_value="5.1,stereo"):
            await self.real.load(self.channel)
        calls = [c.args for c in self.real.ipc.command.await_args_list]
        self.assertLess(calls.index(("set_property", "pause", True)), calls.index(("set_property", "aid", "auto")))
        self.assertNotIn(("set_property", "pause", False), calls)
        self.assertTrue(self.real.initial_audio_pending)
        self.real.ipc.command.assert_any_await("set_property", "audio-channels", "5.1,stereo")

    async def test_initial_gate_only_unpauses_after_normal_german_track(self):
        self.real.initial_audio_pending = True
        self.real.ipc.get = AsyncMock(return_value=[track(lang="eng", selected=True)])
        self.assertFalse(await self.real.select_audio(self.channel))
        self.assertTrue(self.real.initial_audio_pending)
        self.assertNotIn(("set_property", "pause", False), [c.args for c in self.real.ipc.command.await_args_list])
        self.real.ipc.get = AsyncMock(side_effect=lambda name:
            1.0 if name == 'demuxer-cache-duration' else [track(selected=True)])
        self.assertTrue(await self.real.select_audio(self.channel))
        self.assertFalse(self.real.initial_audio_pending)
        self.real.ipc.command.assert_awaited_with("set_property", "pause", False)
        self.real.ipc.command.reset_mock()
        self.assertTrue(await self.real.select_audio(self.channel))
        self.real.ipc.command.assert_not_awaited()

    async def test_initial_gate_waits_for_selected_audio_cushion(self):
        self.real.initial_audio_pending = True
        normal = track(self.channel.audio_pid, selected=True)
        for duration in (None, False, '1', float('nan'), float('inf'), -1, 0, .999):
            self.real.ipc.get = AsyncMock(side_effect=[ [normal], duration ])
            self.assertFalse(await self.real.select_audio(self.channel))
            self.assertTrue(self.real.initial_audio_pending)
            self.assertNotIn(('set_property', 'pause', False),
                             [c.args for c in self.real.ipc.command.await_args_list])
        self.real.ipc.get = AsyncMock(side_effect=[[normal], 1.0])
        self.assertTrue(await self.real.select_audio(self.channel))
        self.assertFalse(self.real.initial_audio_pending)
        self.real.ipc.command.assert_awaited_once_with('set_property', 'pause', False)

    async def test_audio_reselection_checks_new_queue_before_unpause(self):
        self.real.initial_audio_pending = True
        normal = track(self.channel.audio_pid, selected=False)
        self.real.ipc.get = AsyncMock(side_effect=[[normal], .2])
        self.assertFalse(await self.real.select_audio(self.channel))
        self.real.ipc.command.assert_awaited_once_with('set_property', 'aid', normal['id'])
        normal['selected'] = True
        self.real.ipc.get = AsyncMock(side_effect=[[normal], 1.2])
        self.assertTrue(await self.real.select_audio(self.channel))
        self.assertEqual(self.real.ipc.command.await_count, 2)
        self.real.ipc.command.assert_awaited_with('set_property', 'pause', False)

    async def test_late_audio_updates_banner_without_extending_timeout(self):
        controller = Controller(self.config, self.player, self.state)
        controller.playing = self.channel.uuid
        self.player.audio_label = Mock(return_value="")
        try:
            with patch("satip_controller.controller.time.monotonic", return_value=100):
                await controller.show_programme(self.channel)
            self.player.audio_label.return_value = "Deutsch · 5.1"
            with patch("satip_controller.controller.time.monotonic", return_value=102):
                await controller.refresh_programme()
            self.assertEqual(controller.epg_window[2], 105)
            self.assertEqual(controller.osd.durations[1], 3000)
            self.assertIn("Deutsch · 5.1", self.player.ipc.command.call_args.args[0]["data"])
            await controller.osd.volume(50, False)
            count = self.player.ipc.command.await_count
            self.player.audio_label.return_value = "Deutsch · Stereo"
            await controller.refresh_programme()
            self.assertEqual(self.player.ipc.command.await_count, count)
        finally:
            await controller.osd.close()
