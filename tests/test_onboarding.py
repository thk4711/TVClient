"""First-boot transitions, keyboard entry, DHCP freshness and resumable initialization."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from satip_appliance import onboarding, persistent, provision, starter
from satip_appliance.setup_device import networks
from satip_controller.state import atomic_json, boot_id, read_json


class FirstBootTests(unittest.TestCase):
    def test_infrared_and_any_usb_keyboard_coexist(self):
        from types import SimpleNamespace
        from satip_controller.input import selected
        config = SimpleNamespace(keyboard_vendor=1, keyboard_product=2)
        keyboard = SimpleNamespace(name="Another USB keyboard",
            info=SimpleNamespace(bustype=3, vendor=0x1234, product=0xabcd),
            capabilities=lambda: {1: [16, 28, 30, 44, 57, 103, 108]})
        infrared = SimpleNamespace(name="gpio_ir_recv", capabilities=lambda: {1: [103, 108, 28]})
        self.assertEqual(selected(keyboard, config), "keyboard")
        self.assertEqual(selected(infrared, config), "remote")
        keyboard.info.bustype = 0x1e  # HDMI-CEC is not a USB keyboard.
        self.assertIsNone(selected(keyboard, config))

    def test_keyboard_layouts_shift_caps_and_altgr(self):
        self.assertEqual(onboarding.character(21, set(), 'de'), 'z')
        self.assertEqual(onboarding.character(44, {42}, 'de'), 'Y')
        self.assertEqual(onboarding.character(16, {100}, 'de'), '@')
        self.assertEqual(onboarding.character(12, {100}, 'de'), chr(92))
        self.assertEqual(onboarding.character(3, {42}, 'us'), '@')
        self.assertEqual(onboarding.character(40, {42}, 'us'), '"')
        self.assertEqual(onboarding.character(53, {42}, 'de'), '_')
        self.assertEqual(onboarding.character(30, {42}, 'us', caps=True), 'a')

    def test_network_prompt_offers_both_connections_and_never_leaks_password(self):
        flow = onboarding.Onboarding()
        with patch.object(onboarding, 'confirmed_address', return_value=None), patch.object(onboarding, 'carrier', return_value=False):
            self.assertIn('Ethernet cable', flow.screen())
            self.assertIn('USB keyboard', flow.screen())
            flow.keyboard = True
            self.assertIn('Tab', flow.screen())
            flow.phase = 'password'
            flow.password = 'private-password'
            rendered = flow.screen()
            self.assertNotIn(flow.password, rendered)
            self.assertIn('*' * len(flow.password), rendered)
            flow.active = True
            flow.key(14, 1, 'keyboard')
            self.assertEqual(flow.password, 'private-passwor')
            flow.key(1, 1, 'keyboard')
            self.assertEqual(flow.password, '')
            self.assertEqual(flow.phase, 'preferences')

    def test_ready_page_qr_payload_is_exact_displayed_url_and_ethernet_wins(self):
        flow = onboarding.Onboarding()
        with patch.object(onboarding, 'confirmed_address', side_effect=lambda interface: '192.168.1.2' if interface == 'eth0' else '192.168.1.3'), \
                patch('satip_appliance.splash.listening', return_value=True), patch.object(onboarding, 'read_optional', return_value={}):
            lines = flow.screen().splitlines()
            self.assertEqual(lines[2], 'http://192.168.1.2:8080/')
            self.assertEqual(lines[-1], lines[2])
            self.assertFalse(flow.active)

    def test_dhcp_requires_current_boot_carrier_and_matching_live_address(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            link = root / 'sys/class/net/eth0/carrier'
            link.parent.mkdir(parents=True)
            link.write_text('1')
            status = root / 'run/satip-network/eth0.json'
            status.parent.mkdir(parents=True)
            response = bytes(20) + bytes([192, 168, 1, 4])
            with patch.object(onboarding.fcntl, 'ioctl', return_value=response):
                atomic_json(status, {'state': 'confirmed', 'boot_id': 'old', 'ip': '192.168.1.4'})
                self.assertIsNone(onboarding.confirmed_address('eth0', root))
                atomic_json(status, {'state': 'confirmed', 'boot_id': boot_id(), 'ip': '192.168.1.5'})
                self.assertIsNone(onboarding.confirmed_address('eth0', root))
                atomic_json(status, {'state': 'confirmed', 'boot_id': boot_id(), 'ip': '192.168.1.4'})
                self.assertEqual(onboarding.confirmed_address('eth0', root), '192.168.1.4')
                link.write_text('0')
                self.assertIsNone(onboarding.confirmed_address('eth0', root))

    def test_fresh_marker_initializes_without_preferences_and_resumes_own_stage(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(persistent.os, 'chown'):
            root = Path(directory)
            data = root / 'data'
            data.mkdir()
            marker = data / '.satip-factory-empty'
            marker.write_bytes(b'satip-fresh-data-v1\n')
            (data / '.fresh-stage/config').mkdir(parents=True)
            (data / '.fresh-stage/.schema.jsonabcd1234').write_text('{')
            fake = {'boot_stage': lambda *a: None, 'boot_failure': lambda *a: None, 'read_config': lambda *a: {}}
            with patch.object(provision, 'parser', return_value=fake), patch.object(persistent, 'validate_mount'), \
                    patch.object(persistent, 'prepare'), patch.object(provision, 'seed'), patch('satip_appliance.backup.recover_restore'):
                provision.boot(root, data)
                self.assertEqual(read_json(data / 'store/installation.json'), {'state': 'unconfigured'})
                self.assertFalse(marker.exists())
                self.assertFalse((data / '.fresh-stage').exists())
                self.assertFalse((data / 'store/.schema.jsonabcd1234').exists())
                provision.boot(root, data)  # A second boot must reuse the committed store.

    def test_marker_cannot_authorize_foreign_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / 'data'
            data.mkdir()
            (data / '.satip-factory-empty').write_bytes(b'satip-fresh-data-v1\n')
            original = data / 'family-recording.ts'
            original.write_bytes(b'keep')
            fake = {'boot_stage': lambda *a: None, 'boot_failure': lambda *a: None, 'read_config': lambda *a: {}}
            with patch.object(provision, 'parser', return_value=fake), patch.object(persistent, 'validate_mount'), \
                    patch('satip_appliance.backup.recover_restore'):
                with self.assertRaises(ValueError):
                    provision.boot(root, data)
            self.assertEqual(original.read_bytes(), b'keep')
            self.assertFalse((data / 'store').exists())

    def test_scan_deduplicates_and_associates_security_with_each_bss(self):
        text = 'BSS a\n signal: -60.0 dBm\n SSID: Home\n Authentication suites: PSK\nBSS b\n signal: -30.0 dBm\n SSID: Home\n Authentication suites: PSK\nBSS c\n signal: -20.0 dBm\n SSID: Guest\n'
        found = networks(text)
        self.assertEqual([n['ssid'] for n in found], ['Guest', 'Home'])
        self.assertFalse(found[0]['supported'])
        self.assertTrue(found[1]['supported'])
        self.assertEqual(found[1]['signal'], -30)

    def test_starter_prefers_hd_and_uses_only_discovered_services(self):
        candidates = [{'name': n, 'service_uuid': str(i)} for i, n in enumerate(['Other', 'ZDF', 'Das Erste HD', 'ZDF HD'])]
        self.assertEqual(starter.lineup(candidates), ['2', '3'])
        self.assertLess(starter.mux_order({'frequency': 11494000, 'polarisation': 'H'}), starter.mux_order({'frequency': 12000000}))


class WiFiTrialTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_only_after_selected_ssid_and_dhcp(self):
        flow = onboarding.Onboarding()
        flow.selected, flow.password = 'Home', 'private-password'
        calls = []
        async def request(action, args=None):
            calls.append((action, args))
            if action == 'wifi': return {'trial_id': 'test'}
            if action == 'status': return {'trial': {'id': 'test', 'applied': True}, 'connected_ssid': 'Home'}
            return {}
        with patch.object(onboarding, 'request', side_effect=request), patch.object(onboarding, 'confirmed_address', return_value='192.168.1.4'):
            await flow.connect()
        self.assertEqual([c[0] for c in calls], ['wifi', 'status', 'wifi_confirm'])
        self.assertEqual(flow.password, '')
        self.assertIsNone(flow.trial)

    async def test_failed_connection_reverts_and_returns_to_password(self):
        flow = onboarding.Onboarding()
        flow.selected, flow.password = 'Home', 'private-password'
        fake = AsyncMock(side_effect=[{'trial_id': 'test'}, {'trial': None}, {}])
        with patch.object(onboarding, 'request', fake):
            await flow.connect()
        self.assertEqual(fake.call_args_list[-1].args, ('wifi_revert', {'id': 'test'}))
        self.assertEqual(flow.phase, 'password')
        self.assertEqual(flow.password, '')
        self.assertNotIn('private-password', flow.message)
