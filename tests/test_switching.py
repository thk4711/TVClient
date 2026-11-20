import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from satip_controller.switching import SwitchExperiments
from satip_controller.controller import Controller
from satip_controller.state import atomic_json
from tests.helpers import Fixture


class SwitchExperimentTests(unittest.TestCase):
    def test_comparisons_are_independent_and_combined_excludes_failed_probe(self):
        self.assertFalse(any(SwitchExperiments.comparison_flags('baseline').values()))
        for name in SwitchExperiments.__dataclass_fields__:
            flags = SwitchExperiments.comparison_flags(name)
            self.assertTrue(flags[name])
            self.assertEqual(sum(flags.values()), 1)
        combined = SwitchExperiments.comparison_flags('combined')
        self.assertFalse(combined['bounded_probe'])
        self.assertEqual(sum(combined.values()), 3)

    def test_runtime_flags_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'switch-experiments.json'
            self.assertEqual(SwitchExperiments.load(directory), SwitchExperiments())
            for value in ('broken', '[]', '{"saved_audio":1}',
                          '{"unknown":true}', ' ' * 1025):
                path.write_text(value)
                self.assertEqual(SwitchExperiments.load(directory), SwitchExperiments())
            path.write_text(json.dumps({'saved_audio': True, 'bounded_probe': True}))
            flags = SwitchExperiments.load(directory)
            self.assertTrue(flags.saved_audio)
            self.assertEqual(flags.probe_options(), {
                'demuxer-lavf-analyzeduration': '0.5',
                'demuxer-lavf-probesize': '131072'})

    def test_relay_is_permanent_default_with_explicit_legacy_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'switch-experiments.json'
            self.assertTrue(SwitchExperiments.load(directory).persistent_relay)
            path.write_text('{"saved_audio":true}')
            self.assertTrue(SwitchExperiments.load(directory).persistent_relay)
            path.write_text('{"persistent_relay":false}')
            self.assertFalse(SwitchExperiments.load(directory).persistent_relay)
            path.unlink()
            self.assertTrue(SwitchExperiments.load(directory).persistent_relay)
            path.write_text('broken')
            self.assertTrue(SwitchExperiments.load(directory).persistent_relay)

    def test_adaptive_coalescing_is_bounded_and_accounts_for_elapsed_work(self):
        self.assertEqual(SwitchExperiments().coalescing_delay(10, False), .08)
        flags = SwitchExperiments(adaptive_coalescing=True)
        self.assertEqual(flags.coalescing_delay(0, False), .03)
        self.assertEqual(flags.coalescing_delay(0, True), .08)
        self.assertAlmostEqual(flags.coalescing_delay(.02, True), .06)
        self.assertEqual(flags.coalescing_delay(2, True), 0)
        self.assertEqual(flags.coalescing_delay(-1, False), .03)


class CoalescingTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_files()
        self.controller = Controller(self.config, self.player, self.state)

    async def asyncTearDown(self):
        await self.state.close()

    async def test_second_request_during_short_grace_gets_burst_wait(self):
        atomic_json(self.config.runtime_dir / 'switch-experiments.json',
                    {'adaptive_coalescing': True})
        now = 100.
        waits = []
        self.controller.requested_at = now
        self.controller.request_committed = True
        async def sleep(delay):
            nonlocal now
            waits.append(delay)
            now += delay
            if len(waits) == 1:
                self.controller.request(self.data['favourites'][1])
        with patch('satip_controller.controller.time.monotonic', side_effect=lambda: now), \
                patch('satip_controller.controller.asyncio.sleep', side_effect=sleep):
            await self.controller.coalesce_requests()
        self.assertEqual(waits, [.03, .08])

    async def test_ordinary_channel_keys_keep_full_grace_even_when_isolated(self):
        atomic_json(self.config.runtime_dir / 'switch-experiments.json',
                    {'adaptive_coalescing': True})
        self.controller.requested_at = 100
        with patch('satip_controller.controller.time.monotonic', return_value=100), \
                patch('satip_controller.controller.asyncio.sleep') as sleep:
            await self.controller.coalesce_requests()
        sleep.assert_awaited_once_with(.08)
        # Input/OSD processing time must not erode ordinary key coalescing.
        with patch('satip_controller.controller.time.monotonic', return_value=100.02), \
                patch('satip_controller.controller.asyncio.sleep') as sleep:
            await self.controller.coalesce_requests()
        sleep.assert_awaited_once_with(.08)

    async def test_request_already_waiting_during_old_tune_has_no_extra_grace(self):
        atomic_json(self.config.runtime_dir / 'switch-experiments.json',
                    {'adaptive_coalescing': True})
        self.controller.requested_at = 100
        with patch('satip_controller.controller.time.monotonic', return_value=101), \
                patch('satip_controller.controller.asyncio.sleep') as sleep:
            await self.controller.coalesce_requests()
        sleep.assert_awaited_once_with(0)
