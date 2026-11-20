import unittest
from unittest.mock import Mock

from verify_apple_remote import Keys, summarize


def event(value, timestamp, code=115):
    return {"code": code, "value": value, "time": timestamp, "key": "KEY_VOLUMEUP"}


class KeyVerificationTests(unittest.TestCase):
    def test_single_tap_summary(self):
        summary = summarize([event(1, 0), event(0, 0.15)])
        self.assertEqual(summary, {"presses": 1, "releases": 1, "repeats": 0, "duration_ms": 150.0})

    def test_hold_timing(self):
        events = [event(1, 0), event(2, 0.5), event(2, 0.625), event(2, 0.75), event(0, 3)]
        summary = summarize(events)
        self.assertEqual(summary["repeat_delay_ms"], 500.0)
        self.assertEqual(summary["repeat_period_ms"], 125.0)
        self.assertEqual(summary["repeats"], 3)

    def test_duplicate_press_pairs_are_preserved(self):
        capture = Keys.__new__(Keys)
        capture.next = Mock(side_effect=[event(1, 0), event(0, 0.05), event(1, 0.1), event(0, 0.15), None])
        summary = summarize(capture.press(115, 1))
        self.assertEqual(summary["presses"], 2)
        self.assertEqual(summary["releases"], 2)

    def test_wrong_key_and_incomplete_press_are_rejected(self):
        capture = Keys.__new__(Keys)
        capture.next = Mock(side_effect=[event(1, 0, code=113)])
        with self.assertRaises(ValueError):
            capture.press(115, 1)
        with self.assertRaises(ValueError):
            summarize([event(1, 0)])


if __name__ == "__main__":
    unittest.main()
