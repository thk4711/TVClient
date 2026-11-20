import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from satip_appliance.tuning import TuneMonitor
from satip_controller.backend import BackendError
from satip_controller.state import atomic_json
from satip_controller.tuning import TuneFeedback


class TuneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        runtime = self.root / "run/satip-player"
        runtime.mkdir(parents=True)
        self.generation = "g" * 32
        self.response = self.root / "run/satip-tuning/status.json"
        with patch.dict(os.environ, {"SATIP_GENERATION": self.generation,
                                     "SATIP_TUNE_STATUS": str(self.response)}):
            self.feedback = TuneFeedback(runtime)
        with patch("satip_controller.tuning.time.monotonic", return_value=100):
            self.options = self.feedback.begin()
        self.monitor = TuneMonitor(os.getpid(), self.generation, 99, self.root)
        self.row = {"id": 7, "client": self.options["user-agent"], "hostname": "127.0.0.1",
                    "title": "HTTP", "profile": "pass", "state": "Bad", "errors": 0}

    def check(self, rows=None, now=101):
        backend = Mock()
        backend._request.return_value = {"entries": rows if rows is not None else [self.row],
                                         "totalCount": len(rows) if rows is not None else 1}
        with patch("satip_appliance.tuning.credentials", return_value={"username": "admin", "password": "private"}), \
                patch("satip_appliance.tuning.Backend", return_value=backend), \
                patch("satip_appliance.tuning.time.monotonic", return_value=now):
            self.monitor.check(now)
        return backend

    def test_current_bad_subscription_reaches_only_current_load(self):
        backend = self.check()
        self.assertTrue(all(call.args[2] is False for call in backend._request.call_args_list))
        with patch("satip_controller.tuning.time.monotonic", return_value=101.2):
            self.assertTrue(self.feedback.failed())
        self.assertNotIn("private", self.response.read_text())
        self.assertEqual(self.response.stat().st_mode & 0o777, 0o644)
        with patch("satip_controller.tuning.time.monotonic", return_value=104):
            self.assertFalse(self.feedback.failed())

    def test_slow_or_healthy_stream_and_packet_errors_are_not_failed_tunes(self):
        for state in ("Testing", "Idle", "Running", "unknown", None):
            with self.subTest(state=state):
                self.row.update(state=state, errors=1000)
                self.monitor.next_probe = 0
                self.check()
                self.assertFalse(self.response.exists())

    def test_wrong_client_or_ambiguous_subscription_is_ignored(self):
        for rows in ([], [{**self.row, "client": "libmpv"}],
                     [{**self.row, "client": "satip-player/" + "0" * 32}],
                     [{**self.row, "hostname": "remote"}], [self.row, self.row]):
            with self.subTest(rows=rows):
                self.monitor.next_probe = 0
                self.check(rows)
                self.assertFalse(self.response.exists())

    def test_stale_process_generation_or_bad_timestamp_never_queries_backend(self):
        original = json.loads(self.feedback.request.read_text())
        for update in ({"pid": os.getpid()+1}, {"pid": True}, {"generation": "old"},
                       {"monotonic": 98}, {"monotonic": 102}, {"monotonic": True},
                       {"token": "bad"}):
            atomic_json(self.feedback.request, {**original, **update})
            self.monitor.next_probe = 0
            self.check()._request.assert_not_called()
        atomic_json(self.feedback.request, original)
        self.monitor.next_probe = 0
        self.check(now=161)._request.assert_not_called()

    def test_old_response_cannot_fail_retry_or_cancelled_load(self):
        self.check()
        previous = self.options["user-agent"]
        with patch("satip_controller.tuning.time.monotonic", return_value=101.1):
            self.assertNotEqual(self.feedback.begin()["user-agent"], previous)
            self.assertFalse(self.feedback.failed())
        self.feedback.cancel()
        self.assertFalse(self.feedback.request.exists())
        self.assertFalse(self.feedback.failed())

    def test_changed_request_during_probe_drops_response(self):
        original = json.loads(self.feedback.request.read_text())
        with patch("satip_appliance.tuning.small_json", side_effect=[original, {**original, "token": "0"*32}]):
            self.check()
        self.assertFalse(self.response.exists())

    def test_missing_api_is_not_a_failure(self):
        with patch("satip_appliance.tuning.credentials", side_effect=BackendError):
            self.monitor.check(101)
        self.assertFalse(self.response.exists())

    def test_linked_request_is_not_read_as_root(self):
        contents = self.feedback.request.read_bytes()
        self.feedback.request.unlink()
        other = self.root / "other"
        other.write_bytes(contents)
        self.feedback.request.symlink_to(other)
        self.check()._request.assert_not_called()

    def test_unsupervised_player_keeps_normal_load_options(self):
        with patch.dict(os.environ, {}, clear=True):
            feedback = TuneFeedback(self.root)
        self.assertEqual(feedback.begin(), {})
        self.assertFalse(feedback.failed())


if __name__ == "__main__":
    unittest.main()
