import asyncio
import contextlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from validation.measure_switching_on_pi import cache_summary, measure, phase_schedule, timing_summary
from satip_controller.config import library
from satip_controller.player import SD_FILTERS, url
from satip_controller.state import atomic_json
from tests.helpers import Fixture


class SwitchingBenchmarkTests(unittest.TestCase):
    def test_cache_summary_counts_episodes_and_sampled_duration(self):
        samples = [(0, False, .2), (.1, True, 0), (.2, True, .4),
                   (.9, False, 1.1), (1, True, .1), (1.3, False, 1)]
        self.assertEqual(cache_summary(samples), {"cache_pause_count": 2,
                         "sampled_cache_pause_seconds": 1.1, "min_cache_seconds": 0})

    def test_cache_summary_missing_values_are_not_healthy_zeroes(self):
        self.assertEqual(cache_summary([(0, None, None), (.1, False, float('nan'))]),
                         {"cache_pause_count": 0, "sampled_cache_pause_seconds": 0,
                          "min_cache_seconds": None})

    def setUp(self):
        self.phases = [('baseline', {'probe': 0}), ('saved_audio', {'probe': 0})]

    def test_repeated_pairs_alternate_without_changing_options(self):
        schedule = phase_schedule(self.phases, 4)
        self.assertEqual([(b, p, name) for b, p, name, _ in schedule], [
            (1, 1, 'baseline'), (1, 2, 'saved_audio'),
            (2, 1, 'saved_audio'), (2, 2, 'baseline'),
            (3, 1, 'baseline'), (3, 2, 'saved_audio'),
            (4, 1, 'saved_audio'), (4, 2, 'baseline')])
        self.assertTrue(all(options == {'probe': 0} for _, _, _, options in schedule))

    def test_candidate_first_reverses_each_pair(self):
        schedule = phase_schedule(self.phases, 2, 'candidate-first')
        self.assertEqual([name for _, _, name, _ in schedule],
                         ['saved_audio', 'baseline', 'baseline', 'saved_audio'])

    def test_legacy_single_block_retains_phase_order(self):
        phases = self.phases + [('redundant_ipc', {})]
        self.assertEqual([name for _, _, name, _ in phase_schedule(phases)],
                         [name for name, _ in phases])

    def test_repeated_blocks_reject_missing_or_multiple_candidates(self):
        for phases in ([], self.phases[:1], self.phases + [('combined', {})]):
            with self.assertRaisesRegex(ValueError, 'exactly one candidate'):
                phase_schedule(phases, 2)
        with self.assertRaises(ValueError):
            phase_schedule(self.phases, 0)
        with self.assertRaises(ValueError):
            phase_schedule(self.phases, 2, 'random')

    def test_summary_separates_blocks_and_transponders_and_excludes_skips(self):
        rows = [
            {'phase': 'baseline', 'block': 1, 'transition': 'same-transponder', 'advancing_seconds': 4.},
            {'phase': 'baseline', 'block': 1, 'transition': 'different-transponder', 'advancing_seconds': 6.},
            {'phase': 'baseline', 'block': 2, 'transition': 'same-transponder', 'advancing_seconds': 3.},
            {'phase': 'saved_audio', 'block': 1, 'transition': 'same-transponder', 'advancing_seconds': 4.5},
            {'phase': 'baseline', 'already_playing': True},
            {'failed_phase': 'saved_audio', 'failure': 'TimeoutError'},
        ]
        summary = timing_summary(rows)
        self.assertEqual(summary['baseline']['overall'],
                         {'samples': 3, 'median_seconds': 4., 'min_seconds': 3., 'max_seconds': 6.})
        self.assertEqual(summary['baseline']['by_block']['1']['median_seconds'], 5.)
        self.assertEqual(summary['baseline']['by_transition']['same-transponder']['median_seconds'], 3.5)
        self.assertEqual(summary['saved_audio']['overall']['samples'], 1)
        self.assertEqual(timing_summary([]), {})


class RunningBenchmarkTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_files()
        self.config.runtime_dir.mkdir()
        self.channels, self.favourites = library(self.data)
        self.clock = 100.
        self.playing = self.favourites[0]
        self.requested = self.playing
        self.owner = 123
        self.menu = False
        self.index = 0
        self.loads = []
        self.stale_request = None
        self.stale_reads = 0
        self.on_load = lambda: None
        self.codes = SimpleNamespace(EV_KEY=1, BUS_USB=3, KEY_ENTER=10, KEY_ESC=11,
                                     KEY_UP=12, KEY_DOWN=13, KEY_Q=14, KEY_A=15,
                                     KEY_Z=16, KEY_SPACE=17)
        self.ui = SimpleNamespace(write=self.write_key, syn=Mock(), close=Mock(),
                                  device=SimpleNamespace(path=str(self.root / "virtual-input")))
        self.ipc = SimpleNamespace(get=AsyncMock(side_effect=self.get_property),
                                   command=AsyncMock(), close=AsyncMock())
        self.flags = self.config.runtime_dir / "switch-experiments.json"
        self.args = SimpleNamespace(confirm_interrupt=True, small_experiments=["saved_audio"],
                                    compare=False, probe_size=None, compare_initial_buffer=False,
                                    compare_timeshift=False, phase_blocks=2, phase_order="baseline-first",
                                    config=self.root / "config.json", channels=["Das Erste HD", "ZDF HD"],
                                    restore_channel=None, admin_credentials=self.root / "admin.json",
                                    rounds=2, settle_seconds=1, navigation_delay=.75, rapid_zap=False)

    async def asyncTearDown(self):
        await self.state.close()

    def write_key(self, kind, code, pressed):
        if not pressed:
            return
        if code == self.codes.KEY_ESC:
            self.menu = False
        elif code == self.codes.KEY_ENTER:
            if self.menu:
                self.playing = self.requested = self.favourites[self.index]
                self.menu = False
                self.loads.append((self.playing, json.loads(self.flags.read_text())
                                   if self.flags.exists() else None))
                self.on_load()
            else:
                self.menu = True
                self.index = self.favourites.index(self.requested)
        elif self.menu:
            direction = 1 if code == self.codes.KEY_DOWN else -1
            self.index = (self.index + direction) % len(self.favourites)

    async def get_property(self, name):
        channel = self.channels[self.playing]
        if name.startswith("options/"):
            return 0
        return {"path": url(channel), "time-pos": self.clock, "audio-pts": self.clock,
                "track-list": [{"type": "audio", "id": 1, "src-id": channel.audio_pid,
                                "lang": "deu", "codec": "mp2", "visual-impaired": False,
                                "hearing-impaired": False, "external": False}],
                "aid": 1, "audio-channels": "stereo", "hwdec-current": "test",
                "vf": SD_FILTERS if channel.video == "mpeg2" else [],
                "frame-drop-count": 0, "avsync": 0, "paused-for-cache": False,
                "demuxer-cache-duration": 1, "cache-pause-initial": False}[name]

    async def run_benchmark(self):
        import satip_controller.state as storage
        read = storage.read_json
        def read_json(path):
            if path.name == "status.json":
                requested = self.requested
                if self.stale_reads:
                    self.stale_reads -= 1
                    requested = self.stale_request
                return {"owner_pid": self.owner, "playing": self.playing,
                        "requested": requested, "loading": None, "paused": False}
            return read(path)
        real_sleep = asyncio.sleep
        async def sleep(delay):
            self.clock += delay
            await real_sleep(0)
        reader = asyncio.StreamReader()
        reader.feed_eof()
        writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        with contextlib.ExitStack() as stack:
            for name, value in {
                "read_json": read_json, "os.geteuid": lambda: 0, "os.chown": Mock(),
                "os.chmod": Mock(), "time": SimpleNamespace(monotonic=lambda: self.clock),
                "asyncio.sleep": sleep, "MPVIPC.connect": AsyncMock(return_value=self.ipc),
                "Backend": Mock(return_value=SimpleNamespace(grid=AsyncMock(return_value=[{}]))),
                "asyncio.open_unix_connection": AsyncMock(return_value=(reader, writer)),
            }.items():
                stack.enter_context(patch("validation.measure_switching_on_pi." + name, value))
            stack.enter_context(patch.dict("sys.modules", {"evdev": SimpleNamespace(
                UInput=Mock(return_value=self.ui), ecodes=self.codes)}))
            stack.enter_context(patch("grp.getgrnam", return_value=SimpleNamespace(gr_gid=1)))
            stack.enter_context(patch.dict("os.environ", {"SATIP_RUNTIME_ROOT": ""}))
            output = stack.enter_context(patch("builtins.print"))
            try:
                await measure(self.args)
            finally:
                self.output = [json.loads(call.args[0]) for call in output.call_args_list]

    async def test_complete_repeats_restore_absent_flags_and_original_channel(self):
        await self.run_benchmark()
        rows = [r for r in self.output if "advancing_seconds" in r]
        self.assertEqual(len(rows), 16)
        self.assertEqual([r["phase_begin"] for r in self.output if "phase_begin" in r],
                         ["baseline", "saved_audio", "saved_audio", "baseline"])
        self.assertTrue(all(r["from_channel"] != r["channel"] for r in rows))
        self.assertTrue(all(r["audio_ok"] and r["filter_ok"] for r in rows))
        self.assertFalse(self.flags.exists())
        self.assertEqual(self.playing, self.favourites[0])
        self.assertEqual(self.output[-1]["timing_summary"]["baseline"]["overall"]["samples"], 8)
        self.ui.close.assert_called_once()
        self.ipc.close.assert_awaited_once()

    async def test_initial_buffer_comparison_supports_counterbalanced_blocks(self):
        self.args.small_experiments = None
        self.args.compare_initial_buffer = True
        initial = False
        get = self.get_property
        async def setting(*args):
            nonlocal initial
            if args[:2] == ('set_property', 'options/cache-pause-initial'):
                initial = args[2]
        async def reading(name):
            return initial if name in ('cache-pause-initial', 'options/cache-pause-initial') else await get(name)
        self.ipc.command.side_effect = setting
        self.ipc.get.side_effect = reading
        await self.run_benchmark()
        self.assertEqual([r["phase_begin"] for r in self.output if "phase_begin" in r],
                         ["baseline", "initial-buffer", "initial-buffer", "baseline"])
        self.assertTrue(all(r["cache_pause_count"] == 0 for r in self.output
                            if "advancing_seconds" in r))
        self.assertFalse(self.flags.exists())
        self.assertEqual(self.playing, self.favourites[0])

    async def test_initial_buffer_comparison_rejects_file_local_override(self):
        self.args.small_experiments = None
        self.args.compare_initial_buffer = True
        get = self.get_property
        async def file_local(name):
            return True if name == 'cache-pause-initial' else await get(name)
        self.ipc.get.side_effect = file_local
        with self.assertRaisesRegex(ValueError, 'Initial buffering already enabled or file-local'):
            await self.run_benchmark()
        self.assertEqual(self.loads, [])
        self.ipc.close.assert_awaited_once()

    async def test_initial_buffer_comparison_rejects_override_on_next_load(self):
        self.args.small_experiments = None
        self.args.compare_initial_buffer = True
        get = self.get_property
        async def file_local(name):
            if name == 'cache-pause-initial' and self.loads:
                return True
            return await get(name)
        self.ipc.get.side_effect = file_local
        with self.assertRaisesRegex(ValueError, 'Live-load policy overrides'):
            await self.run_benchmark()
        self.assertFalse(any('advancing_seconds' in r for r in self.output))
        self.assertEqual(self.playing, self.favourites[0])
        self.ipc.close.assert_awaited_once()

    async def test_failure_preserves_phase_and_restores_preexisting_flags(self):
        atomic_json(self.flags, {"redundant_ipc": True})
        before = self.flags.read_bytes()
        def interrupt():
            if len(self.loads) == 6:
                self.owner += 1
        self.on_load = interrupt
        with self.assertRaisesRegex(ValueError, "restarted"):
            await self.run_benchmark()
        failures = [r for r in self.output if "failed_phase" in r]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["block"], 1)
        self.assertEqual(failures[0]["stage"], "timed")
        self.assertEqual(self.flags.read_bytes(), before)
        self.ui.close.assert_called_once()
        self.ipc.close.assert_awaited_once()

    async def test_source_status_lag_is_waited_inside_timing_not_an_intervening_request(self):
        def lag():
            self.stale_request = (self.favourites[1] if self.playing == self.favourites[0]
                                  else self.favourites[0])
            self.stale_reads = 2
        self.on_load = lag
        await self.run_benchmark()
        self.assertEqual(self.output[-1]["switches"], 16)
        self.assertFalse(any("failed_phase" in r for r in self.output))

    async def test_midrun_preview_aborts_and_does_not_emit_success_summary(self):
        def interrupt():
            if len(self.loads) == 5:
                atomic_json(self.config.management_dir / "preview-lease.json",
                            {"recovery_pending": True})
        self.on_load = interrupt
        with self.assertRaisesRegex(ValueError, "preview"):
            await self.run_benchmark()
        self.assertTrue(any(r.get("failed_phase") == "baseline" for r in self.output))
        self.assertFalse(any("timing_summary" in r for r in self.output))
        self.assertFalse(self.flags.exists())
        self.ui.close.assert_called_once()

    async def test_catalogue_edit_aborts_without_overwriting_user_edit(self):
        edited = self.config.library.read_bytes() + b"\n"
        def interrupt():
            if len(self.loads) == 5:
                self.config.library.write_bytes(edited)
        self.on_load = interrupt
        with self.assertRaisesRegex(ValueError, "catalogue changed"):
            await self.run_benchmark()
        self.assertEqual(self.config.library.read_bytes(), edited)
        self.assertFalse(self.flags.exists())
        self.assertFalse(any("timing_summary" in r for r in self.output))

    async def test_intervening_request_invalidates_timed_load(self):
        def interrupt():
            if len(self.loads) == 5:
                self.requested = self.favourites[2]
        self.on_load = interrupt
        with self.assertRaisesRegex(ValueError, "Intervening channel request"):
            await self.run_benchmark()
        self.assertEqual(self.output[-1]["completed_switches"], 0)
        self.assertFalse(self.flags.exists())

    async def test_cancellation_cleans_up_before_restoring_original_channel(self):
        def interrupt():
            if len(self.loads) == 5:
                raise asyncio.CancelledError()
        self.on_load = interrupt
        with self.assertRaises(asyncio.CancelledError):
            await self.run_benchmark()
        self.assertEqual(self.playing, self.favourites[0])
        self.assertIsNone(self.loads[-1][1])
        self.assertFalse(self.flags.exists())
        self.ui.close.assert_called_once()
        self.ipc.close.assert_awaited_once()
