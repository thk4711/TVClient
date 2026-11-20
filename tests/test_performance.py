import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from satip_controller.performance import Performance
from satip_controller.telemetry import Telemetry


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 0
        self.monitor = Performance(self.root, lambda: self.now)
        self.monitor.ticks, self.monitor.page_size = 100, 4096
        self.write('proc/meminfo', 'MemTotal: 1000 kB\nMemAvailable: 400 kB\nCached: 200 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n')
        self.write('proc/loadavg', '1.0 2.0 3.0 1/20 42')
        (self.root / 'sys/block/mmcblk0/device').mkdir(parents=True)
        self.write('sys/block/loop0/stat', '0 0 100 0 0 0 100 0 0 0 0')
        self.sample(100, 50, 10, 20, 100)

    def write(self, path, value):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)

    def sample(self, total, idle, read, write, ticks, start=1):
        self.write('proc/stat', f'cpu {total-idle} 0 0 {idle} 0 0 0 0\ncpu0 {total-idle} 0 0 {idle} 0 0 0 0\n')
        self.write('sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq', '1200000')
        self.write('sys/block/mmcblk0/stat', f'0 0 {read} 0 0 0 {write} 0 0 0 0')
        fields = ['0'] * 22
        fields[0], fields[11], fields[19], fields[21] = 'S', str(ticks), str(start), '10'
        self.write('proc/42/stat', '42 (mpv ) worker) ' + ' '.join(fields))
        self.write('proc/42/cmdline', 'secret password')
        self.write('proc/43/stat', 'disappearing malformed process')

    def test_two_samples_units_and_allowlist(self):
        first = self.monitor.collect()
        self.assertIsNone(first['cpu_percent'])
        self.assertIsNone(first['processes'][0]['cpu_percent'])
        self.assertEqual(first['memory_used'], 600 * 1024)
        self.assertEqual(first['swap_used'], 0)
        self.assertEqual(first['cores'][0]['frequency_mhz'], 1200)
        self.now = 5
        self.sample(600, 300, 20, 40, 350)
        second = self.monitor.collect()
        self.assertEqual(second['cpu_percent'], 50)
        self.assertEqual(second['processes'][0]['cpu_percent'], 50)
        self.assertEqual(second['processes'][0]['name'], 'mpv ) worker')
        self.assertEqual(second['processes'][0]['memory_bytes'], 40960)
        self.assertEqual(second['disks'], [{'name': 'mmcblk0', 'read_bytes_per_second': 1024, 'write_bytes_per_second': 2048}])
        self.assertNotIn('secret', str(second))

    def test_reset_pid_reuse_and_long_gap(self):
        self.monitor.collect()
        self.now = 5
        self.sample(50, 20, 1, 2, 500, start=2)
        reset = self.monitor.collect()
        self.assertIsNone(reset['cpu_percent'])
        self.assertIsNone(reset['processes'][0]['cpu_percent'])
        self.assertIsNone(reset['disks'][0]['read_bytes_per_second'])
        self.now = 100
        self.sample(600, 300, 20, 40, 600, start=2)
        gap = self.monitor.collect()
        self.assertIsNone(gap['cpu_percent'])
        self.assertIsNone(gap['processes'][0]['cpu_percent'])
        self.assertIsNone(gap['disks'][0]['write_bytes_per_second'])

    def test_missing_cpu_and_optional_memory(self):
        (self.root / 'proc/stat').unlink()
        with self.assertRaises(OSError):
            self.monitor.collect()
        self.sample(100, 50, 10, 20, 100)
        (self.root / 'proc/meminfo').unlink()
        result = self.monitor.collect()
        self.assertIsNone(result['memory_used'])
        self.assertIsNone(result['swap_used'])

    def test_bounded_process_output(self):
        for pid in range(100, 650):
            self.write(f'proc/{pid}/stat', (self.root / 'proc/42/stat').read_text())
        result = self.monitor.collect()
        self.assertTrue(result['processes_truncated'])
        self.assertEqual(len(result['processes']), 12)
        self.assertLessEqual(len(self.monitor.processes), 512)


class PerformanceCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_cache_and_independent_failure(self):
        telemetry = Telemetry(AsyncMock(), '.')
        telemetry.backend.grid.return_value = []
        with patch.object(telemetry, 'system', return_value={}), patch.object(telemetry, 'network', return_value={}), \
                patch.object(telemetry.performance, 'collect', return_value={'cpu_percent': 20}) as collect:
            first, second = await asyncio.gather(telemetry.snapshot(), telemetry.snapshot())
            collect.assert_called_once()
            self.assertEqual(first, second)
            telemetry.cache['performance'] = (0, first['performance'])
            collect.side_effect = OSError('private details')
            failed = await telemetry.snapshot()
            self.assertEqual(failed['performance']['state'], 'stale')
            self.assertEqual(failed['performance']['sampled_at'], first['performance']['sampled_at'])
            self.assertEqual(failed['system']['state'], 'ok')
            self.assertNotIn('private', str(failed))
