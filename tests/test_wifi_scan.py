import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from satip_appliance.wifi_scan import ScanHints, ScanRecovery, enabled_frequencies, frequencies
from satip_controller.state import atomic_json, boot_id


class ScanHintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "etc").mkdir()
        (self.root / "run").mkdir()
        self.source = self.root / "etc/wpa_supplicant.conf"
        self.original = b'country=DE\nctrl_interface=/run/wpa_supplicant\nnetwork={\n ssid="private"\n psk="secret"\n}\n'
        self.source.write_bytes(self.original)
        self.hints = ScanHints(self.root)
        radio = patch.object(ScanHints, "available", return_value={2412, 2437, 2462, 5180})
        radio.start()
        self.addCleanup(radio.stop)
        observer = patch.object(ScanHints, "observer_active", return_value=True)
        observer.start()
        self.addCleanup(observer.stop)

    def test_first_scan_only_private_copy_leaves_original_unchanged(self):
        self.hints.remember(5180)
        self.hints.remember(2462)
        path = self.hints.prepare()
        self.assertEqual(path, self.hints.runtime)
        self.assertEqual(path.read_bytes(), b"initial_freq_list=2462 5180\n" + self.original)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.hints.cache.stat().st_mode & 0o777, 0o600)
        public = (self.hints.directory / "wifi-scan.json").read_text()
        self.assertNotIn("private", public)
        self.assertNotIn("secret", public)
        self.assertNotIn(hashlib.sha256(self.original).hexdigest(), public)

    def test_mru_is_unique_and_bounded(self):
        for frequency in (2412, 2437, 2462, 5180, 2462):
            self.hints.remember(frequency)
        self.assertEqual(self.hints.saved(self.hints.identity), [2462, 5180, 2437])

    def test_missing_malformed_or_changed_network_uses_normal_config(self):
        self.assertEqual(self.hints.prepare(), self.source)
        self.hints.remember(5180)
        self.hints.cache.write_text("{bad")
        self.assertEqual(self.hints.prepare(), self.source)
        self.hints.remember(5180)
        self.assertEqual(self.hints.saved(self.hints.identity), [5180])
        self.source.write_bytes(self.original.replace(b"private", b"different"))
        self.assertEqual(ScanHints(self.root).prepare(), self.source)
        self.hints.remember(2462)
        self.assertEqual(self.hints.saved(self.hints.identity), [5180])
        fresh = ScanHints(self.root)
        fresh.remember(2462)
        self.assertEqual(fresh.saved(fresh.identity), [2462])

    def test_existing_explicit_scan_policy_is_preserved(self):
        for setting in (b"initial_freq_list=5180\n", b"freq_list=5180\n", b" scan_freq=5180\n"):
            self.source.write_bytes(setting + self.original)
            hints = ScanHints(self.root)
            hints.remember(2462)
            self.assertEqual(hints.prepare(), self.source)
            self.assertEqual(self.source.read_bytes(), setting + self.original)

    def test_incomplete_previous_copy_never_becomes_active(self):
        self.hints.remember(5180)
        self.hints.prepare()
        self.hints.runtime.with_suffix(".next").write_text("interrupted")
        self.assertEqual(self.hints.prepare(), self.source)
        self.assertFalse(self.hints.runtime.exists())

    def test_invalid_hints_and_versions_are_ignored(self):
        for value in ([], [True], [5180, 5180], [5180]*4, [0], [99999], [2413], ["5180"]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError): frequencies(value)
                atomic_json(self.hints.cache, {"version": 1, "config_sha256": self.hints.identity, "frequencies": value})
                self.assertEqual(self.hints.prepare(), self.source)
        for version in (True, 2):
            atomic_json(self.hints.cache, {"version": version, "config_sha256": self.hints.identity, "frequencies": [5180]})
            self.assertEqual(self.hints.prepare(), self.source)

    def test_symlinked_cache_cannot_supply_or_receive_hints(self):
        self.hints.remember(5180)
        other = self.root / "other"
        original = self.hints.cache.read_bytes()
        other.write_bytes(original)
        self.hints.cache.unlink()
        self.hints.cache.symlink_to(other)
        self.assertEqual(self.hints.prepare(), self.source)
        self.hints.remember(2462)
        self.assertEqual(other.read_bytes(), original)

    def test_old_runtime_copy_is_not_used_when_cache_is_absent(self):
        self.hints.remember(5180)
        self.hints.prepare()
        self.hints.cache.unlink()
        self.assertEqual(self.hints.prepare(), self.source)
        self.assertFalse(self.hints.runtime.exists())

    def test_disabled_radio_frequency_is_never_passed_to_supplicant(self):
        radio = "* 5180.0 MHz [36] (20.0 dBm)\n* 5825.0 MHz [165] (disabled)\n"
        self.assertEqual(enabled_frequencies(radio), {5180})
        self.hints.remember(5825)
        self.assertEqual(self.hints.prepare(), self.source)
        self.assertEqual(json.loads((self.hints.directory / "wifi-scan.json").read_text())["mode"], "full")
        self.hints.remember(5180)
        self.assertEqual(self.hints.prepare().read_bytes(), b"initial_freq_list=5180\n" + self.original)

    def test_unknown_radio_capabilities_use_normal_configuration(self):
        self.hints.remember(5180)
        with patch.object(self.hints, "available", side_effect=OSError):
            self.assertEqual(self.hints.prepare(), self.source)

    def test_missing_recovery_worker_uses_normal_configuration(self):
        self.hints.remember(5180)
        with patch.object(self.hints, "observer_active", return_value=False):
            self.assertEqual(self.hints.prepare(), self.source)


class ScanRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "scan.json"
        atomic_json(self.path, {"boot_id": boot_id(), "mode": "initial", "prepared": 100})
        self.recovery = ScanRecovery(self.path)

    def test_failed_scan_is_nudged_once_after_grace(self):
        send = Mock(return_value=b"OK\n")
        self.assertFalse(self.recovery.retry({"state": "DISCONNECTED"}, 102, send))
        self.assertTrue(self.recovery.retry({"state": "DISCONNECTED"}, 103, send))
        self.assertFalse(self.recovery.retry({"state": "DISCONNECTED"}, 104, send))
        send.assert_called_once_with(b"SCAN")

    def test_active_scan_association_and_authentication_are_not_interrupted(self):
        send = Mock()
        for state in ("SCANNING", "ASSOCIATING", "ASSOCIATED", "AUTHENTICATING",
                      "4WAY_HANDSHAKE", "GROUP_HANDSHAKE", "COMPLETED", "unavailable"):
            self.assertFalse(self.recovery.retry({"state": state}, 105, send))
        send.assert_not_called()

    def test_unavailable_control_is_bounded_and_old_boot_ignored(self):
        send = Mock(side_effect=OSError)
        for now in range(103, 120):
            self.assertFalse(self.recovery.retry({"state": "DISCONNECTED"}, now, send))
        self.assertEqual(send.call_count, 3)
        atomic_json(self.path, {"boot_id": "old", "mode": "initial", "prepared": 100})
        send.reset_mock()
        self.assertFalse(ScanRecovery(self.path).retry({"state": "DISCONNECTED"}, 105, send))
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
