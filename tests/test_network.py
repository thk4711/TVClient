import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from satip_appliance import network as n
from satip_controller.state import atomic_json


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('STORE', 'RUN'):
            directory = self.root / name
            directory.mkdir()
            p = patch.object(n, name, directory)
            p.start(); self.addCleanup(p.stop)
        self.cache = {'version': 2, 'ip': '192.168.1.10'}

    def test_legacy_cache_becomes_address_hint_only(self):
        legacy = {**self.cache, 'version': 1, 'lease': 3600, 'gateway': '192.168.1.1',
                  'expires_at': None, 'identity': 'a'*64}
        self.assertEqual(n.validate_cache(legacy), self.cache)

    def test_malformed_and_unsuitable_caches(self):
        for bad in ({}, [], {'version': True, 'ip': self.cache['ip']},
                    {**self.cache, 'extra': 1}, {**self.cache, 'version': 3}):
            with self.subTest(bad=bad), self.assertRaises(ValueError): n.validate_cache(bad)
        for ip in (None, 1234, 'bad', '0.0.0.0', '127.0.0.1', '169.254.1.1', '224.0.0.1', '255.255.255.255'):
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                n.validate_cache({**self.cache, 'ip': ip})

    def launch(self, interface='wlan0'):
        with patch.object(n.os, 'execv') as execute, patch('subprocess.run') as commands, \
                patch('subprocess.Popen') as processes, patch.object(n.time, 'sleep') as sleep:
            n.start(interface)
        commands.assert_not_called(); processes.assert_not_called(); sleep.assert_not_called()
        return execute.call_args.args[1]

    def test_safe_start_only_requests_previous_address(self):
        atomic_json(n.STORE / 'wlan0.json', self.cache)
        args = self.launch()
        self.assertEqual(args[args.index('-r') + 1], self.cache['ip'])
        self.assertNotIn('-s', args)
        self.assertEqual(n.read(n.RUN / 'wlan0.json')['state'], 'dhcp')

    def test_missing_and_corrupt_cache_do_not_delay_dhcp(self):
        self.assertNotIn('-r', self.launch())
        (n.STORE / 'wlan0.json').write_text('{broken')
        self.assertNotIn('-r', self.launch())
        atomic_json(n.STORE / 'wlan0.json', ['not a cache'])
        self.assertNotIn('-r', self.launch())

    def test_interfaces_keep_separate_hints_and_retry_options(self):
        atomic_json(n.STORE / 'eth0.json', {'version': 2, 'ip': '192.168.1.20'})
        atomic_json(n.STORE / 'wlan0.json', self.cache)
        wired, wifi = self.launch('eth0'), self.launch()
        self.assertEqual(wired[wired.index('-r')+1], '192.168.1.20')
        self.assertEqual(wired[wired.index('-t')+1], '3')
        self.assertNotIn('-S', wired)
        self.assertEqual(wifi[wifi.index('-t')+1], '10')
        self.assertIn('-S', wifi)

    def test_binding_stores_only_confirmed_ip_without_network_commands(self):
        with patch.dict(os.environ, {'ip': self.cache['ip']}, clear=True), \
                patch('subprocess.run') as commands:
            n.cache_event('wlan0', 'bound')
        commands.assert_not_called()
        self.assertEqual(n.read(n.STORE / 'wlan0.json'), self.cache)
        self.assertEqual(n.read(n.RUN / 'wlan0.json')['state'], 'confirmed')
        self.assertEqual((n.STORE / 'wlan0.json').stat().st_mode & 0o777, 0o600)

    def test_deconfig_and_nak_invalidate_hint_without_configuring_address(self):
        for event in ('deconfig', 'nak'):
            atomic_json(n.STORE / 'wlan0.json', self.cache)
            with patch('subprocess.run') as commands: n.cache_event('wlan0', event)
            commands.assert_not_called()
            self.assertFalse((n.STORE / 'wlan0.json').exists())

    def test_removed_actions_cannot_activate_address_reuse(self):
        for action in ('monitor', 'hook', 'stop'):
            with patch.object(n.sys, 'argv', ['network', action, 'wlan0', '30']), \
                    patch.object(n.os, 'geteuid', return_value=0), self.assertRaises(SystemExit):
                n.main()
