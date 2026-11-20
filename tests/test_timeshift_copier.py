"""Exercise the real copier loop with isolated files and controlled lifetime."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from satip_controller.rolling import copy_usb
from satip_controller.state import atomic_json, read_json
from tests.test_timeshift import CONFIG


class CopierTests(unittest.TestCase):
    def run_copier(self, segments, position=5, missing=(), stale_clock=False):
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
