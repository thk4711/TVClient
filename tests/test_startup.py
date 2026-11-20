import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from satip_appliance import startup, splash


class StartupGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "run").mkdir()
        self.gate = startup.StartupGate(self.root)
        self.url = "http://192.168.1.2:38400/description.xml"

    def test_backend_does_not_register_before_route_exists(self):
        with patch.object(startup, "route_available", return_value=False):
            self.assertFalse(self.gate.backend(self.url))
        self.assertEqual((self.root / "run/satip-reception-stage").read_text(), "network\n")
        with patch.object(startup, "route_available", return_value=True):
            self.assertTrue(self.gate.backend(self.url))
        self.assertTrue(self.gate.backend(None))

    def test_no_network_does_not_probe_server_or_backend(self):
        with patch.object(startup, "address", return_value=None), \
                patch.object(startup, "Backend") as backend:
            self.assertFalse(self.gate.player(self.url))
        backend.assert_not_called()

    def test_unreachable_server_is_not_called_a_signal_fault(self):
        with patch.object(startup, "address", return_value="192.168.1.3"), \
                patch.object(startup, "route_available", return_value=True), \
                patch.object(startup.socket, "create_connection", side_effect=OSError):
            self.assertFalse(self.gate.player(self.url))
        self.assertEqual(self.gate.previous, "server")

    def test_only_active_matching_server_with_enabled_tuner_is_ready(self):
        server = {"class": "satip_client", "uuid": "server", "params": [
            {"id": "addr", "value": "192.168.1.2"}, {"id": "active", "value": True}]}
        tuner = {"class": "satip_frontend_dvbs", "params": [{"id": "enabled", "value": False}]}
        backend = Mock()
        backend._request.side_effect = lambda endpoint, values, write: [server] if values["uuid"] == "root" else [tuner]
        with patch.object(startup, "address", return_value="192.168.1.3"), \
                patch.object(startup, "route_available", return_value=True), \
                patch.object(startup.socket, "create_connection"), \
                patch.object(startup, "read_json", return_value={"username": "admin", "password": "secret"}), \
                patch.object(startup, "Backend", return_value=backend):
            self.assertFalse(self.gate.player(self.url))
            tuner["params"][0]["value"] = True
            self.assertTrue(self.gate.player(self.url))
            server["params"][0]["value"] = "192.168.1.4"
            self.assertFalse(self.gate.player(self.url))
        self.assertNotIn("secret", (self.root / "run/satip-reception-stage").read_text())
        self.assertTrue(all(call.args[2] is False for call in backend._request.call_args_list))

    def test_server_and_tuner_wait_have_separate_recoverable_deadlines(self):
        self.assertEqual(splash.timed_state((27, ""), 5), (27, ""))
        self.assertEqual(splash.timed_state((27, ""), 61), (28, ""))
        self.assertEqual(splash.timed_state((29, ""), 5), (29, ""))
        self.assertEqual(splash.timed_state((29, ""), 61), (30, ""))

    def test_link_state_distinguishes_wifi_association_from_dhcp(self):
        self.assertEqual(splash.network_state(self.root), 4)
        (self.root / "run/satip-wpa.pid").touch()
        self.assertEqual(splash.network_state(self.root), 32)
        network = self.root / "sys/class/net/wlan0"
        network.mkdir(parents=True)
        (network / "carrier").write_text("1\n")
        self.assertEqual(splash.network_state(self.root), 33)
        self.assertEqual(splash.timed_state((32, ""), 61), (34, ""))
        self.assertEqual(splash.timed_state((33, ""), 61), (19, ""))

    def test_network_launcher_does_not_require_unbuilt_wpa_syslog_support(self):
        board = Path(__file__).resolve().parents[1] / "buildroot/external"
        script = (board / "board/satip/rootfs-overlay/etc/init.d/S40network").read_text()
        config = (board / "configs/satip_rpi4_64_defconfig").read_text()
        launcher = next(line for line in script.splitlines() if "wpa_supplicant -B" in line)
        if "-s" in launcher.split():
            self.assertIn("BR2_PACKAGE_WPA_SUPPLICANT_DEBUG_SYSLOG=y", config)
