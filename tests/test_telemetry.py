import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from satip_controller.backend import BackendError
from satip_controller.telemetry import Telemetry, signal, wifi


class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    def test_signal_units_missing_and_zero(self):
        self.assertEqual(signal(65535, 1, 'dBm'), {'value': 100.0, 'unit': '%'})
        self.assertEqual(signal(0, 1, 'dBm'), {'value': 0.0, 'unit': '%'})
        self.assertEqual(signal(-47500, 2, 'dBm'), {'value': -47.5, 'unit': 'dBm'})
        self.assertEqual(signal(12500, 2, 'dB'), {'value': 12.5, 'unit': 'dB'})
        for value, scale in ((0, 0), (None, 1), (float('nan'), 2), (12, 99)):
            self.assertIsNone(signal(value, scale, 'dB')['value'])

    async def test_cache_single_flight_and_partial_failure(self):
        backend = AsyncMock()
        backend.grid.return_value = []
        telemetry = Telemetry(backend, '.')
        with patch.object(telemetry, 'system', return_value={'hostname': 'receiver'}), \
                patch.object(telemetry, 'network', side_effect=OSError):
            first, second = await asyncio.gather(telemetry.snapshot(), telemetry.snapshot())
            backend.grid.assert_awaited_once_with('status/inputs')
            self.assertEqual(first, second)
            self.assertEqual(first['network']['state'], 'unavailable')
            self.assertEqual(first['reception']['activity'], 'idle')
            self.assertEqual(first['system']['hostname'], 'receiver')
            telemetry.cache['reception'] = (0, first['reception'])
            backend.grid.side_effect = BackendError('private backend details')
            failed = await telemetry.snapshot()
            self.assertEqual(failed['reception']['state'], 'stale')
            self.assertEqual(failed['reception']['sampled_at'], first['reception']['sampled_at'])
            self.assertNotIn('private', str(failed))

    async def test_error_deltas_reset_and_secret_allowlist(self):
        backend = AsyncMock()
        row = {'uuid': 'a', 'signal': 0, 'signal_scale': 0, 'cc': 5, 'te': 0, 'username': 'private'}
        backend.grid.return_value = [row]
        telemetry = Telemetry(backend, '.')
        first = (await telemetry.reception())['streams'][0]
        self.assertIsNone(first['signal']['value'])
        self.assertIsNone(first['error_changes']['cc'])
        self.assertNotIn('private', str(first))
        row['cc'] = 7
        self.assertEqual((await telemetry.reception())['streams'][0]['error_changes']['cc'], 2)
        row['cc'] = 1
        self.assertIsNone((await telemetry.reception())['streams'][0]['error_changes']['cc'])

    def test_network_link_speed(self):
        cases = [
            # Ethernet keeps its sysfs speed and does not call iw.
            ('eth0', 1000, '', 1000),
            ('eth0', -1, '', None),
            # TX is the representative Wi-Fi rate; RX is a fallback only.
            ('wlan0', None, 'Connected to ap\n tx bitrate: 72.2 MBit/s\n rx bitrate: 65.0 MBit/s', 72.2),
            ('wlan0', -1, 'Connected to ap\n rx bitrate: 65.0 MBit/s', 65.0),
            ('wlan0', 0, 'Connected to ap\n tx bitrate: 0.0 MBit/s\n rx bitrate: 65.0 MBit/s', 65.0),
            ('wlan0', None, 'Connected to ap\n tx bitrate: 0.0 MBit/s', None),
            ('wlan0', None, 'Connected to ap', None),
            ('wlan0', None, 'Not connected.\n tx bitrate: 72.2 MBit/s', None),
            ('wlan0', None, '', None),
            ('wlan0', 150, 'Connected to ap\n tx bitrate: 72.2 MBit/s', 150),
        ]
        for name, sysfs_speed, output, expected in cases:
            with self.subTest(name=name, speed=sysfs_speed, output=output):
                telemetry = Telemetry(AsyncMock(), '.')
                with patch('satip_controller.telemetry.socket.if_nameindex', return_value=[(1, 'lo'), (2, name)]), \
                        patch('satip_controller.telemetry.fcntl.ioctl', side_effect=OSError), \
                        patch('satip_controller.telemetry.Path.exists', return_value=name == 'wlan0'), \
                        patch('satip_controller.telemetry.text', return_value=''), \
                        patch('satip_controller.telemetry.numeric', side_effect=lambda path: sysfs_speed if path.name == 'speed' else 0), \
                        patch('satip_controller.telemetry.command', return_value=output) as command:
                    interface, = telemetry.network()['interfaces']
                self.assertEqual(interface['speed_mbps'], expected)
                if name == 'wlan0':
                    command.assert_called_once_with('iw', 'dev', name, 'link')
                    self.assertIsNotNone(interface['wifi'])
                else:
                    command.assert_not_called()
                    self.assertIsNone(interface['wifi'])

    def test_wifi_readback_and_unsupported_driver(self):
        with patch('satip_controller.telemetry.command', return_value='Connected to 00:00:00:00:00:00\n\tSSID: Living room\n\tfreq: 5180\n\tsignal: -51 dBm\n\ttx bitrate: 300.0 MBit/s'):
            result = wifi('wlan0')
            self.assertEqual(result['ssid'], 'Living room')
            self.assertEqual(result['signal_dbm'], -51)
            self.assertEqual(result['tx_rate_mbps'], 300)
        with patch('satip_controller.telemetry.command', return_value=''):
            self.assertEqual(wifi('wlan0')['state'], 'unavailable')
