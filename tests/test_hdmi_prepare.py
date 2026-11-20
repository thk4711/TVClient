import ctypes
import fcntl
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from satip_appliance.hdmi import before_splash, configure, valid_edid
from satip_controller.drm import Connector, Mode, hd_rates, modes, refresh
from satip_controller.player import arguments
from tests.test_hdmi_edid import hdmi
from tests.helpers import Fixture


class HDMIPrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.parameter = self.root / "parameter"
        self.parameter.write_text("")
        self.firmware = self.root / "fallback.bin"
        self.firmware.write_bytes(hdmi.edid())

    def output(self, name, data, rates, connected=True):
        output = Mock(card="card7", name=name)
        output.name = name
        output.connected.return_value = connected
        output.probe.return_value = (data, rates)
        return output

    def run_policy(self, outputs):
        return configure(outputs, self.parameter, self.firmware)

    def test_valid_real_edid_is_never_overridden_even_if_only_60hz(self):
        for rates in ({50, 60}, {60}):
            output = self.output("HDMI-A-1", hdmi.edid(), rates)
            report = self.run_policy([output])
            self.assertEqual(self.parameter.read_text(), "")
            self.assertEqual(report["outputs"][0]["edid"], "real")
            output.probe.assert_called_once()

    def test_only_missing_invalid_or_non_hd_output_gets_fallback(self):
        for data, rates in [(b"", set()), (b"invalid", {50}), (hdmi.edid(), set())]:
            with self.subTest(data_length=len(data), rates=rates):
                self.parameter.write_text("")
                real = self.output("HDMI-A-1", hdmi.edid(), {60})
                missing = self.output("HDMI-A-2", data, rates)
                missing.probe.side_effect = [(data, rates), (hdmi.edid(), {50, 60})]
                with self.assertLogs("satip_appliance.hdmi", level="WARNING"):
                    report = self.run_policy([real, missing])
                self.assertEqual(self.parameter.read_text(), "HDMI-A-2:edid/satip-1080p.bin")
                self.assertEqual([o["edid"] for o in report["outputs"]], ["real", "fallback"])
                real.probe.assert_called_once()

    def test_disconnected_ports_are_not_probed_or_forced(self):
        output = self.output("HDMI-A-1", b"", set(), connected=False)
        self.assertEqual(self.run_policy([output])["outputs"], [])
        self.assertEqual(self.parameter.read_text(), "")
        output.probe.assert_not_called()

    def test_handoff_marker_forbids_late_edid_changes(self):
        directory = self.root / "splash"
        directory.mkdir()
        (directory / "finished").touch()
        operation = Mock()
        before_splash(operation, directory)
        operation.assert_not_called()

    def test_preparation_shares_exclusive_renderer_lock(self):
        directory = self.root / "splash"
        directory.mkdir()
        operation = Mock()
        with (directory / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                before_splash(operation, directory)
        operation.assert_not_called()
        before_splash(operation, directory)
        operation.assert_called_once()

    def test_explicit_admin_policy_is_preserved(self):
        self.parameter.write_text("HDMI-A-1:custom.bin")
        output = self.output("HDMI-A-1", b"", set())
        with self.assertLogs("satip_appliance.hdmi", level="WARNING"):
            self.assertEqual(self.run_policy([output])["policy"], "explicit")
        self.assertEqual(self.parameter.read_text(), "HDMI-A-1:custom.bin")
        output.probe.assert_not_called()

    def test_failed_fallback_resets_parameter_and_reprobes(self):
        output = self.output("HDMI-A-1", b"", set())
        with self.assertRaises(ValueError):
            self.run_policy([output])
        self.assertEqual(self.parameter.read_bytes(), bytes([0]))
        self.assertEqual(output.probe.call_count, 3)

    def test_invalid_firmware_does_not_change_policy(self):
        self.firmware.write_bytes(b"bad")
        with self.assertRaises(ValueError):
            self.run_policy([self.output("HDMI-A-1", b"", set())])
        self.assertEqual(self.parameter.read_text(), "")

    def test_edid_checks_all_blocks_and_declared_extension_length(self):
        data = hdmi.edid()
        self.assertTrue(valid_edid(data))
        for bad in (b"", data[:128], data + bytes(128), bytes(256),
                    data[:150] + bytes([data[150] ^ 1]) + data[151:]):
            self.assertFalse(valid_edid(bad))

    def test_hd_rates_excludes_interlaced_and_non_1080p_modes(self):
        self.assertEqual(hd_rates([(1920, 1080, 50, 0), (1920, 1080, 60, 0),
                                   (1920, 1080, 50, 16), (1920, 1080, 30, 0),
                                   (1280, 720, 50, 0)]), {50, 60})

    def test_fractional_refresh_uses_pixel_clock_not_rounded_field(self):
        mode = Mode(clock=148352, htotal=2200, vtotal=1125, vrefresh=60)
        self.assertAlmostEqual(refresh(mode), 59.9402, places=3)
        self.assertEqual(hd_rates([(1920, 1080, refresh(mode), 0)]), {refresh(mode)})

    def test_libdrm_query_reads_correct_connector_and_frees_all_resources(self):
        card = self.root / "card7"
        card.touch()
        directory = self.root / "card7-HDMI-A-1"
        directory.mkdir()
        (directory / "connector_id").write_text("33")
        array = (Mode * 1)()
        array[0].hdisplay, array[0].vdisplay, array[0].vrefresh = 1920, 1080, 60
        pointer = ctypes.pointer(Connector(connection=1, count_modes=1, modes=array))
        drm = Mock()
        drm.drmModeGetConnector.return_value = pointer
        with patch("satip_controller.drm.C.CDLL", return_value=drm):
            self.assertEqual(modes(str(card), "HDMI-A-1", self.root), [(1920, 1080, 60, 0)])
        drm.drmModeFreeConnector.assert_called_once_with(pointer)
        fd, identity = drm.drmModeGetConnector.call_args.args
        self.assertEqual(identity, 33)
        with self.assertRaises(OSError):
            os.fstat(fd)


class HDMIPlayerModeTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup_files()

    def test_1080p60_only_display_keeps_real_capabilities(self):
        with patch("satip_controller.player.modes", return_value=[(1920, 1080, 60, 0)]):
            with self.assertLogs("satip_controller.player", level="WARNING"):
                argv = arguments(self.config, self.state.data)
        self.assertIn("--drm-mode=1920x1080@60", argv)
        self.assertEqual(self.config.drm_mode, "1920x1080@50")

    def test_supported_preference_or_missing_query_is_unchanged(self):
        for available in ([], [(1920, 1080, 50, 0), (1920, 1080, 60, 0)]):
            with patch("satip_controller.player.modes", return_value=available):
                self.assertIn("--drm-mode=1920x1080@50", arguments(self.config, self.state.data))

    def test_fractional_60hz_only_display_uses_exact_mpv_refresh(self):
        with patch("satip_controller.player.modes", return_value=[(1920, 1080, 59.9402, 0)]):
            with self.assertLogs("satip_controller.player", level="WARNING"):
                self.assertIn("--drm-mode=1920x1080@59.94", arguments(self.config, self.state.data))
