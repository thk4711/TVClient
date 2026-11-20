import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from satip_appliance import supervisor, wifi_startup
from satip_controller.state import atomic_json, boot_id


class BootTests(unittest.TestCase):
    def test_synchronous_boot_helpers_do_not_import_asyncio(self):
        code = "import satip_controller.state, satip_appliance.supervisor, satip_appliance.device_helper; import sys; assert 'asyncio' not in sys.modules"
        result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_batch_start_preserves_order_and_failure_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "run").mkdir()
            with patch.object(supervisor, "control", side_effect=[None, RuntimeError("failed")]) as control, patch("satip_appliance.operations.best_effort"):
                with self.assertRaises(RuntimeError):
                    supervisor.control_all("start", root)
            self.assertEqual([c.args[:2] for c in control.call_args_list], [("tvheadend", "start"), ("web", "start")])
            self.assertEqual((root / "run/satip-boot-error").read_text(), "service-web\n")
            with patch.object(supervisor, "control") as control, patch("satip_appliance.operations.best_effort"):
                supervisor.control_all("stop", root)
            self.assertEqual([c.args[:2] for c in control.call_args_list], [("player", "stop"), ("web", "stop"), ("tvheadend", "stop")])


class WifiStartupTests(unittest.TestCase):
    def test_status_whitelist_excludes_network_identity(self):
        result = wifi_startup.parse_status(b"ssid=private\nbssid=00:11:22:33:44:55\npsk=secret\nwpa_state=COMPLETED\nfreq=5180\n")
        self.assertEqual(result, {"state": "COMPLETED", "frequency_mhz": 5180})
        self.assertEqual(wifi_startup.parse_status(b"wpa_state=private\nfreq=99999"), {"state": "unavailable"})

    def test_real_control_socket_only_reads_status_and_waits_for_dhcp(self):
        # Short /tmp path avoids macOS Unix-domain path limits for this fixture.
        with tempfile.TemporaryDirectory(prefix="satip-wifi-", dir="/tmp") as temporary:
            root = Path(temporary)
            control = str(root / "wpa")
            seen = []
            errors = []
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
                server.bind(control)
                server.settimeout(3)
                def reply():
                    try:
                        for state in ("SCANNING", "COMPLETED", "COMPLETED"):
                            command, peer = server.recvfrom(128)
                            seen.append(command)
                            if len(seen) == 3:
                                atomic_json(root / "wlan0.json", {"boot_id": boot_id(), "state": "confirmed",
                                            "history": [{"uptime": time.monotonic()}]}, durable=False)
                            server.sendto(("ssid=private\nwpa_state="+state+"\nfreq=5180\n").encode(), peer)
                    except Exception as error:
                        errors.append(error)
                thread = threading.Thread(target=reply)
                thread.start()
                try:
                    wifi_startup.observe(root, duration=2, control=control)
                finally:
                    thread.join(timeout=4)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(seen, [b"STATUS"] * 3)
            data = json.loads((root / "wifi-startup.json").read_text())
            self.assertEqual([e["state"] for e in data["events"]], ["SCANNING", "COMPLETED"])
            self.assertGreaterEqual(data["dhcp_confirmed"], data["events"][-1]["uptime"])
            self.assertIn("finished", data)
            self.assertNotIn("private", json.dumps(data))
            self.assertEqual(list(root.glob("*.sock")), [])

    def test_missing_supplicant_is_bounded(self):
        with tempfile.TemporaryDirectory(prefix="satip-wifi-", dir="/tmp") as temporary:
            root = Path(temporary)
            wifi_startup.observe(root, duration=.05, control=str(root / "absent"))
            data = json.loads((root / "wifi-startup.json").read_text())
            self.assertTrue(data["timed_out"])
            self.assertEqual(data["events"][0]["state"], "unavailable")


if __name__ == "__main__":
    unittest.main()
