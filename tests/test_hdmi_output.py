import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from satip_controller.player import arguments, output_devices


class HDMIOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        card = self.root / "card7"
        card.touch()
        device = self.root / "platform-gpu-card"
        device.symlink_to(card)
        self.config = SimpleNamespace(
            drm_device=str(device), drm_connector="HDMI-A-2",
            audio_device="alsa/hdmi:CARD=vc4hdmi1,DEV=0",
            drm_mode="1920x1080@50", runtime_dir=self.root)
        self.status("HDMI-A-1", "connected")
        self.status("HDMI-A-2", "disconnected")

    def status(self, connector, status):
        directory = self.root / f"card7-{connector}"
        directory.mkdir(exist_ok=True)
        (directory / "status").write_text(status + "\n")

    def test_fallback_follows_resolved_card_and_audio(self):
        with self.assertLogs("satip_controller.player", level="WARNING"):
            self.assertEqual(output_devices(self.config, self.root),
                             ("HDMI-A-1", "alsa/hdmi:CARD=vc4hdmi0,DEV=0"))
        self.assertEqual(self.config.drm_connector, "HDMI-A-2")
        self.assertEqual(self.config.audio_device, "alsa/hdmi:CARD=vc4hdmi1,DEV=0")

    def test_reverse_fallback(self):
        self.config.drm_connector = "HDMI-A-1"
        self.config.audio_device = "alsa/hdmi:CARD=vc4hdmi0,DEV=0"
        self.status("HDMI-A-1", "disconnected")
        self.status("HDMI-A-2", "connected")
        with self.assertLogs("satip_controller.player", level="WARNING"):
            self.assertEqual(output_devices(self.config, self.root),
                             ("HDMI-A-2", "alsa/hdmi:CARD=vc4hdmi1,DEV=0"))

    def test_preferred_connected_both_disconnected_or_unknown(self):
        for first, second in [("connected", "connected"),
                              ("disconnected", "disconnected"),
                              ("connected", "unknown")]:
            with self.subTest(first=first, second=second):
                self.status("HDMI-A-1", first)
                self.status("HDMI-A-2", second)
                self.assertEqual(output_devices(self.config, self.root),
                                 (self.config.drm_connector, self.config.audio_device))

    def test_missing_sysfs_or_device_preserves_configuration(self):
        self.assertEqual(output_devices(self.config, self.root / "missing"),
                         (self.config.drm_connector, self.config.audio_device))
        Path(self.config.drm_device).unlink()
        self.assertEqual(output_devices(self.config, self.root),
                         (self.config.drm_connector, self.config.audio_device))

    def test_custom_audio_preserved(self):
        for audio in ["alsa/default", "alsa/hdmi:CARD=vc4hdmi0,DEV=0"]:
            self.config.audio_device = audio
            with self.assertLogs("satip_controller.player", level="WARNING"):
                self.assertEqual(output_devices(self.config, self.root), ("HDMI-A-1", audio))

    def test_other_connectors_not_changed(self):
        self.config.drm_connector = "DP-1"
        self.assertEqual(output_devices(self.config, self.root),
                         ("DP-1", self.config.audio_device))

    def test_mpv_arguments_use_selection_without_changing_mode(self):
        with patch("satip_controller.player.output_devices",
                   return_value=("HDMI-A-1", "alsa/hdmi:CARD=vc4hdmi0,DEV=0")):
            argv = arguments(self.config, {"volume": 35, "mute": False})
        self.assertIn("--drm-connector=HDMI-A-1", argv)
        self.assertIn("--audio-device=alsa/hdmi:CARD=vc4hdmi0,DEV=0", argv)
        self.assertIn("--drm-mode=1920x1080@50", argv)
