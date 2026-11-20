"""Exercise the real copier loop with isolated files and controlled lifetime."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from satip_controller.rolling import copy_usb, removed_segments
from satip_controller.state import atomic_json, read_json
from tests.test_timeshift import CONFIG


class CopierTests(unittest.TestCase):
    def run_copier(self, segments, position=5, missing=(), stale_clock=False, max_segments=4096):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            usb = root / "usb"
            usb.mkdir()
            ram = root / ("rolling-" + "a" * 32)
            ram.mkdir()
            ram_index = root / "ram.json"
            index = root / "usb.json"
            for item in segments:
                if item["name"] not in missing:
                    (ram / item["name"]).write_bytes(b"abc")
            atomic_json(ram_index, {"segments": segments, "error": ""})
            if stale_clock:
                os.utime(ram_index, (1, 1))
            read_fd, write_fd = os.pipe()
            os.close(write_fd)
            passes = 0
            def select(read, write, error, timeout):
                nonlocal passes
                if timeout == .25:
                    passes += 1
                return ([read_fd] if passes > 3 else [], [], [])
            records = []
            def publish(path, data, *args, **kwargs):
                if path == index:
                    records.append(data.copy())
                atomic_json(path, data, *args, **kwargs)
            with patch("satip_controller.rolling.DIRECTORY", usb), \
                    patch("satip_controller.rolling.storage", return_value=CONFIG), \
                    patch("satip_controller.rolling.ram_storage"), \
                    patch("satip_controller.rolling.signal.signal"), \
                    patch("satip_controller.rolling.resource.setrlimit"), \
                    patch("satip_controller.rolling.MAX_SEGMENTS", max_segments), \
                    patch("satip_controller.rolling.select.select", side_effect=select), \
                    patch("satip_controller.rolling.atomic_json", side_effect=publish):
                copy_usb(usb / ("rolling-" + "b" * 32), ram, ram_index, index, read_fd, position)
            failure_path = root / "last-usb-failure.json"
            return records, read_json(failure_path) if failure_path.exists() else None

    def segment(self, number, start, end):
        return {"name": f"segment-{number:09d}.ts", "start": start, "end": end, "bytes": 3}

    def test_idle_history_is_not_republished_and_clock_step_does_not_fail(self):
        records, failure = self.run_copier([self.segment(0, 4, 6)], stale_clock=True)
        self.assertIsNone(failure)
        self.assertEqual(len(records), 2)  # Initial readiness and final shutdown only.
        self.assertTrue(records[0]["ready"])
        self.assertEqual(records[-1]["error"], "")

    def test_expired_unneeded_activation_history_is_skipped(self):
        old, current = self.segment(0, 0, 2), self.segment(1, 4, 6)
        records, failure = self.run_copier([old, current], missing=[old["name"]])
        self.assertIsNone(failure)
        self.assertEqual(records[0]["segments"], [current])

    def test_missing_pause_segment_is_explicit_failure_with_retained_diagnostics(self):
        item = self.segment(0, 4, 6)
        records, failure = self.run_copier([item], missing=[item["name"]])
        self.assertEqual(failure["stage"], "ram_segment")
        self.assertEqual(failure["errno"], 2)
        self.assertEqual(failure["exception"], "FileNotFoundError")
        self.assertIn("fell behind", records[-1]["error"])
        self.assertNotIn("usb", str(failure).lower())

    def test_missing_new_content_is_never_silently_skipped(self):
        first, lost = self.segment(0, 4, 6), self.segment(1, 6, 8)
        records, failure = self.run_copier([first, lost], missing=[lost["name"]])
        self.assertEqual(failure["last_end"], 6)
        self.assertIn("fell behind", records[-1]["error"])

    def test_segment_cap_trims_without_failing_or_republishing_idle_history(self):
        segments = [self.segment(i, 4 + i * 2, 6 + i * 2) for i in range(12)]
        records, failure = self.run_copier(segments, max_segments=4)
        self.assertIsNone(failure)
        self.assertEqual(records[-2]["segments"], segments[-4:])
        self.assertTrue(records[-2]["ready"])
        self.assertEqual(len(records), 3)  # Readiness, completed batch, shutdown.


class EvictionWorkTests(unittest.TestCase):
    def test_large_history_eviction_work_is_linear_even_without_removals(self):
        class CountedSegment(dict):
            reads = 0

            def __getitem__(self, key):
                if key == "name":
                    type(self).reads += 1
                return super().__getitem__(key)

        for count in (1426, 4096):
            items = [CountedSegment(name=f"segment-{i:09d}.ts") for i in range(count)]
            for dropped in (0, 1, count - 1):
                with self.subTest(count=count, dropped=dropped):
                    keep = items[dropped:]
                    CountedSegment.reads = 0
                    removed = removed_segments(items, keep)
                    self.assertEqual(removed, items[:dropped])
                    self.assertEqual(CountedSegment.reads, len(items) + len(keep))
                    self.assertEqual(len(items), count)
                    self.assertEqual(keep, items[dropped:])
