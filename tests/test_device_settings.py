"""Device tests use a fake root and commands, never real host networking."""
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from satip_appliance.device_api import install
from satip_appliance.device_helper import Device, DeviceError, TRIAL_SECONDS
from satip_appliance import device_settings as settings
from satip_appliance.provision import apply_devices, DEFAULTS
from satip_controller.http import check_action
from satip_controller.state import atomic_json

PARSER = Path("buildroot/external/board/satip/rootfs-overlay/usr/sbin/satip-dev-provision")
HASH = "$6$salt$" + "A" * 86


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("etc", "run", "boot", "usr/sbin", "usr/share/zoneinfo/Europe",
                     "usr/share/zoneinfo/Etc", "data/store/identity", "data/store/network",
                     "data/store/config/web", "data/store/management", "data/store/web"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        shutil.copy(PARSER, self.root / "usr/sbin/satip-dev-provision")
        self.write("etc/hostname", "test-receiver")
        self.write("etc/timezone", "Europe/Berlin")
        self.write("etc/satip-ssh-enabled", "yes")
        self.write("etc/shadow", "root:*:0:0:99999:7:::")
        self.write("etc/wpa_supplicant.conf", "original Wi-Fi config")
        self.write("boot/satip-config.txt", "ssh_enabled=yes\nroot_password=old-password")
        self.write("usr/share/zoneinfo/zone.tab", "DE\t+5230+01322\tEurope/Berlin")
        for zone in ("Europe/Berlin", "Etc/UTC"):
            (self.root / "usr/share/zoneinfo" / zone).write_bytes(b"TZif-test")
        self.now, self.link, self.ethernet = 100, "home", False
        self.calls = []
        self.device = Device(self.root, self.fake_run, lambda: self.now)
        self.values = {"hostname": "test-receiver", "ssh_enabled": "yes",
                       "wifi_ssid": "home", "wifi_psk": "old-wifi-password", "wifi_country": "DE"}
        settings.save_preferences(self.root, self.device.parser, self.values)

    def write(self, name, text):
        (self.root / name).write_text(text + "\n")

    def fake_run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        output = ""
        if "mkpasswd" in argv:
            output = HASH
        elif "link" in argv:
            output = "Connected\n\tSSID: " + self.link
        elif "scan" in argv:
            output = "signal: -60 dBm\nSSID: home\nsignal: -40 dBm\nSSID: home\nsignal: -50 dBm\nSSID: guest"
        elif "addr" in argv and (argv[-1] == "wlan0" or self.ethernet):
            output = "inet 192.168.1.2/24"
        return SimpleNamespace(stdout=output)

    def start_trial(self, ssid="new-home", password="new-wifi-password"):
        result = self.device.handle("wifi", {"ssid": ssid, "password": password, "country": "de"})
        self.now += 2
        self.device.tick()
        return result["trial_id"]

    def test_tuner_selection_is_validated_and_deferred(self):
        with patch('satip_appliance.tv_source.public_status', return_value={'active': 'satip', 'available': ['satip']}):
            with self.assertRaises(ValueError):
                self.device.handle('satip_tuner', {'frontend': '../bad'})
            result = self.device.handle('satip_tuner', {'frontend': 'a'*32})
        self.assertIn('restart', result['message'])
        self.assertFalse(any('satip_appliance.tv_source' in c[0] for c in self.calls))
        self.now += 3
        self.device.tick()
        self.assertTrue(any(c[0] == ['/usr/bin/python3', '-m', 'satip_appliance.tv_source',
                                    'tuner', 'a'*32, '--apply'] for c in self.calls))
        self.assertEqual(self.device.last_result, 'SAT-IP tuner applied.')

    def test_tuner_selection_refuses_inactive_satip_and_pending_trial(self):
        with patch('satip_appliance.tv_source.public_status', return_value={'active': 'enigma2', 'available': ['satip', 'enigma2']}):
            with self.assertRaises(DeviceError):
                self.device.handle('satip_tuner', {'frontend': 'a'*32})
        self.start_trial()
        with self.assertRaises(DeviceError):
            self.device.handle('satip_tuner', {'frontend': 'a'*32})

    def test_source_selection_is_allowlisted_and_deferred_until_after_response(self):
        with patch('satip_appliance.tv_source.public_status', return_value={'active': 'satip', 'available': ['satip', 'enigma2']}):
            result = self.device.handle('source_select', {'source': 'enigma2'})
        self.assertIn('restart', result['message'])
        self.assertFalse(any('satip_appliance.tv_source' in call[0] for call in self.calls))
        self.now += 3
        self.device.tick()
        self.assertTrue(any(call[0] == ['/usr/bin/python3', '-m', 'satip_appliance.tv_source',
                                       'select', 'enigma2', '--apply'] for call in self.calls))
        self.assertEqual(self.device.last_result, 'TV source selected.')

    def test_source_selection_rejects_unimported_sources_and_pending_wifi(self):
        with self.assertRaises(DeviceError):
            self.device.handle('source_select', {'source': 'enigma2'})
        self.start_trial()
        with self.assertRaises(DeviceError):
            self.device.handle('source_select', {'source': 'satip'})

    def test_status_never_returns_secrets(self):
        result = self.device.handle("status", {})
        self.assertEqual(result["wifi_ssid"], "home")
        self.assertNotIn("old-wifi-password", json.dumps(result))
        self.assertNotIn("root_password", json.dumps(result))

    def test_hostname_timezone_save_and_boot_reapply(self):
        self.device.handle("device", {"hostname": "living-room", "timezone": "Etc/UTC"})
        self.assertEqual(settings.saved_preferences(self.root, self.device.parser)["hostname"], "living-room")
        apply_devices(self.root, DEFAULTS)
        self.assertEqual((self.root / "etc/timezone").read_text().strip(), "Etc/UTC")
        self.assertEqual((self.root / "etc/hostname").read_text().strip(), "test-receiver")

    def test_invalid_hostname_and_timezone_do_not_write(self):
        for host, zone in (("foo\nssh_enabled=no", "Etc/UTC"), ("foo", "../../etc/shadow"), ("UPPER", "Etc/UTC")):
            with self.assertRaises(ValueError):
                self.device.handle("device", {"hostname": host, "timezone": zone})
        self.assertEqual(settings.saved_preferences(self.root, self.device.parser), self.values)

    def test_trial_commits_only_after_ready_confirmation(self):
        identifier = self.start_trial()
        self.assertEqual(settings.saved_preferences(self.root, self.device.parser), self.values)
        self.assertIn("new-home", (self.root / "etc/wpa_supplicant.conf").read_text())
        self.assertEqual(self.device.trial_path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(DeviceError):
            self.device.handle("wifi_confirm", {"id": identifier})
        self.link = "new-home"
        self.device.handle("wifi_confirm", {"id": identifier})
        self.assertFalse(self.device.trial_path.exists())
        self.assertEqual(settings.saved_preferences(self.root, self.device.parser)["wifi_ssid"], "new-home")

    def test_unconfirmed_trial_expires(self):
        self.start_trial()
        self.now += TRIAL_SECONDS
        self.device.tick()
        self.assertEqual((self.root / "etc/wpa_supplicant.conf").read_text().strip(), "original Wi-Fi config")
        self.assertEqual(settings.saved_preferences(self.root, self.device.parser), self.values)
        self.assertFalse(self.device.trial_path.exists())

    def test_helper_restart_or_reboot_restores_trial(self):
        self.start_trial()
        Device(self.root, self.fake_run, lambda: self.now).boot_prepare()
        self.assertEqual((self.root / "etc/wpa_supplicant.conf").read_text().strip(), "original Wi-Fi config")
        self.assertFalse(self.device.trial_path.exists())

    def test_manual_revert_and_stale_confirmation(self):
        identifier = self.start_trial()
        with self.assertRaises(DeviceError):
            self.device.handle("wifi_confirm", {"id": "wrong"})
        self.device.handle("wifi_revert", {"id": identifier})
        with self.assertRaises(DeviceError):
            self.device.handle("wifi_confirm", {"id": identifier})

    def test_blank_password_retains_only_same_network_password(self):
        self.start_trial("home", "")
        self.assertEqual(self.device.trial()["candidate"]["wifi_psk"], "old-wifi-password")
        self.device.revert()
        with self.assertRaises(DeviceError):
            self.start_trial("elsewhere", "")

    def test_wifi_escaping_and_validation(self):
        self.start_trial('quoted"network')
        self.assertIn(chr(92) + '"', (self.root / "etc/wpa_supplicant.conf").read_text())
        self.device.revert()
        for name in ("bad\nroot_password=oops", "x" * 33, ""):
            with self.assertRaises(DeviceError):
                self.start_trial(name)

    def test_trial_rejects_parallel_edits(self):
        self.start_trial()
        with self.assertRaises(DeviceError):
            self.device.handle("device", {"hostname": "new", "timezone": "Etc/UTC"})

    def test_wifi_disable_requires_ethernet(self):
        with self.assertRaisesRegex(DeviceError, "Ethernet"):
            self.device.handle("wifi_enabled", {"enabled": False})
        self.ethernet = True
        self.device.handle("wifi_enabled", {"enabled": False})
        self.device.boot_prepare()
        self.assertEqual((self.root / "etc/satip-wifi-enabled").read_text().strip(), "no")

    def test_scan_deduplicates_and_sorts(self):
        self.assertEqual(self.device.handle("scan_wifi", {})["networks"],
                         [{"ssid": "home", "signal": -40}, {"ssid": "guest", "signal": -50}])

    def test_root_password_uses_stdin_and_only_hash_persists(self):
        self.device.handle("root_password", {"password": "new-root-secret", "confirm": "new-root-secret"})
        self.assertNotIn("new-root-secret", (self.root / "data/store/identity/development.txt").read_text())
        self.assertIn(HASH, (self.root / "etc/shadow").read_text())
        argv, options = next(call for call in self.calls if "mkpasswd" in call[0])
        self.assertNotIn("new-root-secret", argv)
        self.assertEqual(options["input"], "new-root-secret\n")
        current = self.device.parser["read_config"](self.root / "boot/satip-config.txt")
        self.assertNotIn("root_password", settings.recovery_values(self.root, current))
        current["root_password"] = "new-recovery-edit"
        self.assertEqual(settings.recovery_values(self.root, current)["root_password"], "new-recovery-edit")

    def test_password_mismatch_and_linked_shadow_rejected(self):
        with self.assertRaises(DeviceError):
            self.device.handle("root_password", {"password": "one", "confirm": "two"})
        (self.root / "etc/shadow").unlink()
        (self.root / "etc/shadow").symlink_to(self.root / "etc/hostname")
        with self.assertRaises(DeviceError):
            self.device.handle("root_password", {"password": "one", "confirm": "one"})
        self.assertEqual(settings.saved_preferences(self.root, self.device.parser), self.values)

    def test_web_password_refreshes_factory_seed(self):
        path = self.root / "data/store/config/web/login.json"
        atomic_json(path, {"username": "satip", "password": "old"})
        factory = self.root / "data/store/identity/factory.json"
        atomic_json(factory, {"login": {"username": "satip", "password": "old"}})
        with patch("satip_appliance.device_helper.os.chown"):
            self.device.handle("web_password", {"password": "new", "confirm": "new"})
        self.assertEqual(settings.read_optional(path)["password"], "new")
        self.assertEqual(settings.read_optional(factory)["login"]["password"], "new")

    def test_ssh_requires_password_or_key_and_suppresses_old_seed(self):
        with self.assertRaisesRegex(DeviceError, "root password"):
            self.device.handle("ssh", {"enabled": True})
        self.device.handle("ssh", {"enabled": False})
        current = self.device.parser["read_config"](self.root / "boot/satip-config.txt")
        self.assertNotIn("ssh_enabled", settings.recovery_values(self.root, current))
        self.assertEqual(settings.saved_preferences(self.root, self.device.parser)["ssh_enabled"], "no")

    def test_unknown_actions_fields_and_invalid_toggle_rejected(self):
        for action, args in (("exec", {}), ("device", {"path": "/etc/shadow"}), ("ssh", {"enabled": 1})):
            with self.assertRaises(DeviceError):
                self.device.handle(action, args)

    def test_maintenance_allows_inactive_recovery_marker(self):
        atomic_json(self.root / "data/store/management/recovery.json", {"pending": False})
        self.device.handle("restart_player", {})
        self.now += 3
        self.device.tick()
        self.assertEqual([call[0][-1] for call in self.calls], ["stop", "start"])
        atomic_json(self.root / "data/store/management/recovery.json", {"pending": True})
        with self.assertRaises(DeviceError):
            self.device.handle("reboot", {})


class DeviceAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.login = Path(self.temp.name) / "login.json"
        atomic_json(self.login, {"username": "satip", "password": "web-secret"})
        self.calls = []
        async def helper(action, args=None):
            self.calls.append((action, args))
            return {"message": "Saved."}
        async def guard(request: Request):
            check_action(request)
        app = FastAPI(dependencies=[Depends(guard)])
        install(app, self.login, helper)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.auth = ("satip", "web-secret")
        self.headers = {"X-Satip-Action": "1"}

    def post(self, payload):
        return self.client.post("/api/device", json=payload, auth=self.auth, headers=self.headers)

    def test_always_requires_authentication(self):
        self.assertEqual(self.client.get("/api/device").status_code, 401)
        self.assertEqual(self.client.get("/api/device", auth=self.auth).status_code, 200)

    def test_password_changes_require_login_but_not_password_reentry(self):
        for action in ("web_password", "root_password"):
            payload = {"action": action, "args": {"password": "new-secret", "confirm": "new-secret"}}
            self.assertEqual(self.client.post("/api/device", json=payload, headers=self.headers).status_code, 401)
            self.assertEqual(self.client.post("/api/device", json=payload, auth=("satip", "wrong"),
                                             headers=self.headers).status_code, 401)
            self.assertEqual(self.post(payload).status_code, 200)
            result = self.client.post("/api/device", json=payload, auth=self.auth,
                                      headers={**self.headers, "Origin": "http://evil.example"})
            self.assertEqual(result.status_code, 403)
        self.assertEqual([action for action, _ in self.calls], ["web_password", "root_password"])

    def test_sensitive_changes_require_current_web_password(self):
        self.assertEqual(self.post({"action": "reboot"}).status_code, 403)
        self.assertEqual(self.post({"action": "reboot", "current_password": "wrong"}).status_code, 403)
        self.assertEqual(self.post({"action": "reboot", "current_password": "web-secret"}).status_code, 200)
        self.assertEqual(self.calls, [("reboot", {})])

    def test_non_sensitive_change_and_origin_protection(self):
        self.assertEqual(self.post({"action": "scan_wifi"}).status_code, 200)
        result = self.client.post("/api/device", json={"action": "scan_wifi"}, auth=self.auth,
                                  headers={**self.headers, "Origin": "http://evil.example"})
        self.assertEqual(result.status_code, 403)
        self.assertEqual(self.client.post("/api/device", json={"action": "scan_wifi"}, auth=self.auth).status_code, 403)

    def test_storage_is_authenticated_and_eject_reconfirms_password(self):
        self.assertEqual(self.client.get("/api/device/storage").status_code, 401)
        self.assertEqual(self.client.get("/api/device/storage", auth=self.auth).status_code, 200)
        for action in ("storage_eject",):
            self.assertEqual(self.post({"action": action}).status_code, 403)
            self.assertEqual(self.post({"action": action, "current_password": "wrong"}).status_code, 403)
            self.assertEqual(self.post({"action": action, "current_password": "web-secret"}).status_code, 200)
        self.assertEqual(self.post({"action": "storage_mount"}).status_code, 200)
        self.assertEqual(self.post({"action": "storage_format"}).status_code, 200)
        self.assertEqual(self.post({"action": "storage_limit"}).status_code, 200)
        result = self.client.post("/api/device", json={"action": "storage_format", "current_password": "web-secret"},
            auth=self.auth, headers={**self.headers, "Origin": "http://evil.example"})
        self.assertEqual(result.status_code, 403)

    def test_password_reload_invalidates_old_login(self):
        atomic_json(self.login, {"username": "satip", "password": "changed"})
        self.assertEqual(self.client.get("/api/device", auth=self.auth).status_code, 401)
        self.assertEqual(self.client.get("/api/device", auth=("satip", "changed")).status_code, 200)

    def test_invalid_requests_do_not_echo_secrets(self):
        result = self.post({"action": "exec", "args": {"password": "never-echo-this"}, "current_password": "web-secret"})
        self.assertEqual(result.status_code, 422)
        self.assertNotIn("never-echo-this", result.text)
        self.assertNotIn("web-secret", result.text)
