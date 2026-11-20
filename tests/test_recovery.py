import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from satip_appliance.health import Health, small_json
from satip_appliance.operations import record
from satip_appliance.watchdog import configure
from satip_controller.recovery import LocalHealth, Progress


class HealthTests(unittest.TestCase):
    def test_player_progress_and_timeout(self):
        health = Health("player", 42, "new", 100)
        with patch("satip_appliance.health.small_json", return_value={"pid": 42, "generation": "new", "monotonic": 105}):
            self.assertIsNone(health.check(105))
            self.assertIsNone(health.check(149))
            self.assertEqual(health.check(154), "heartbeat_timeout")

    def test_wrong_generation_and_never_started_grace(self):
        health = Health("player", 42, "new", 100)
        with patch("satip_appliance.health.small_json", return_value={"pid": 42, "generation": "old", "monotonic": 219}):
            self.assertIsNone(health.check(219))
            self.assertEqual(health.check(224), "heartbeat_timeout")

    def test_invalid_timestamps_are_not_health(self):
        for stamp in (float("nan"), float("inf"), True, "125", 300, 99):
            with self.subTest(stamp=stamp), patch("satip_appliance.health.small_json", return_value={"pid": 42, "generation": "new", "monotonic": stamp}):
                self.assertEqual(Health("player", 42, "new", 100).check(220), "heartbeat_timeout")

    def test_web_failure_threshold_and_reset(self):
        health = Health("web", 42, "new", 100)
        with patch("satip_appliance.health.http_json", return_value={"pid": 42, "generation": "new"}):
            self.assertIsNone(health.check(100))
        with patch("satip_appliance.health.http_json", side_effect=OSError):
            for now in range(105, 130, 5):
                self.assertIsNone(health.check(now))
            self.assertEqual(health.check(130), "local_api_timeout")
        with patch("satip_appliance.health.http_json", return_value={"pid": 42, "generation": "new"}):
            self.assertIsNone(health.check(135))
            self.assertEqual(health.failures, 0)

    def test_malformed_service_records_do_not_crash_supervisor(self):
        for service, probe in (("player", "small_json"), ("web", "http_json")):
            for node in ([], None, "bad", 1):
                with self.subTest(service=service, node=node), patch("satip_appliance.health." + probe, return_value=node):
                    health = Health(service, 42, "new", 100)
                    for now in range(100, 225, 5):
                        result = health.check(now)
                    self.assertIsNotNone(result)

    def test_web_rejects_different_generation(self):
        health = Health("web", 42, "new", 100)
        with patch("satip_appliance.health.http_json", return_value={"pid": 42, "generation": "old"}):
            for now in range(100, 225, 5):
                result = health.check(now)
            self.assertEqual(result, "local_api_timeout")

    def test_backend_check_only_reads_local_serverinfo(self):
        health = Health("tvheadend", 42, "new", 100)
        with patch("satip_appliance.health.credentials", return_value={"username": "test", "password": "private"}), patch("satip_appliance.health.http_json", return_value={"sw_version": "test"}) as probe:
            self.assertIsNone(health.check(100))
            self.assertEqual(probe.call_args.args[:2], (9981, "/api/serverinfo"))

    def test_small_json_refuses_links_and_large_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health"
            path.write_text(" " * 4097)
            with self.assertRaises(ValueError):
                small_json(path)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                small_json(link)


class ProgressTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True), patch("satip_controller.recovery.atomic_json") as write:
            Progress().pulse()
            write.assert_not_called()

    def test_throttled_ram_generation_record(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SATIP_PROGRESS_FILE": str(Path(directory) / "progress"), "SATIP_GENERATION": "new"}):
            progress = Progress()
            with patch("satip_controller.recovery.time.monotonic", return_value=100):
                progress.pulse()
            data = json.loads(Path(progress.path).read_text())
            self.assertEqual(data, {"pid": os.getpid(), "generation": "new", "monotonic": 100})
            with patch("satip_controller.recovery.time.monotonic", return_value=101), patch("satip_controller.recovery.atomic_json") as write:
                progress.pulse()
                write.assert_not_called()


class LocalHealthTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, client, method="GET", path="/healthz", headers=None):
        calls = []
        async def app(scope, receive, send):
            calls.append("delegate")
        async def receive():
            return {"type": "http.request", "body": b""}
        async def send(message):
            calls.append(message)
        await LocalHealth(app)({"type": "http", "path": path, "method": method,
                              "client": client, "headers": headers or []}, receive, send)
        return calls

    async def test_loopback_only(self):
        self.assertEqual((await self.call(("127.0.0.1", 1234)))[0]["status"], 200)
        self.assertEqual((await self.call(("::1", 1234)))[0]["status"], 200)
        self.assertEqual((await self.call(("192.168.1.2", 1234)))[0]["status"], 404)
        self.assertEqual((await self.call(None))[0]["status"], 404)

    async def test_forwarded_headers_do_not_grant_access(self):
        result = await self.call(("192.168.1.2", 1234), headers=[(b"x-forwarded-for", b"127.0.0.1")])
        self.assertEqual(result[0]["status"], 404)

    async def test_other_routes_delegate_and_post_denied(self):
        self.assertEqual(await self.call(("127.0.0.1", 1234), path="/api/status"), ["delegate"])
        self.assertEqual((await self.call(("127.0.0.1", 1234), method="POST"))[0]["status"], 404)


class OperationsTests(unittest.TestCase):
    def test_bounded_allowlisted_history(self):
        with tempfile.TemporaryDirectory() as directory, patch("satip_appliance.operations.boot_id", return_value="boot1"):
            root = Path(directory)
            for _ in range(40):
                record("player", "heartbeat_timeout", root)
            summary = json.loads((root / "recovery.json").read_text())
            self.assertEqual(len(summary["events"]), 32)
            self.assertEqual(summary["counters"]["player:heartbeat_timeout"], 40)
            self.assertLess((root / "recovery.json").stat().st_size, 16384)
            self.assertEqual((root / "recovery.json").stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                record("player", "private URL", root)

    def test_boot_classification_and_shutdown_watchdog_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("satip_appliance.operations.boot_id", return_value="boot1"):
                record("appliance", "boot", root)
                record("appliance", "shutdown", root)
                record("watchdog", "watchdog_stopped", root)
            with patch("satip_appliance.operations.boot_id", return_value="boot2"):
                record("appliance", "boot", root)
                summary = json.loads((root / "recovery.json").read_text())
                self.assertEqual(summary["previous_shutdown"], "clean")
                record("appliance", "boot", root)
                self.assertEqual(json.loads((root / "recovery.json").read_text())["counters"]["appliance:boot"], 2)
            with patch("satip_appliance.operations.boot_id", return_value="boot3"):
                record("appliance", "boot", root)
                self.assertEqual(json.loads((root / "recovery.json").read_text())["previous_shutdown"], "unclean")


class WatchdogTests(unittest.TestCase):
    def test_uses_actual_timeout_not_requested_timeout(self):
        def ioctl(fd, operation, value, mutate):
            value[0] = 9
        with patch("satip_appliance.watchdog.fcntl.ioctl", side_effect=ioctl):
            self.assertEqual(configure(1, requested=60), (9, 3))

    def test_refuses_unsafe_timeout(self):
        def ioctl(fd, operation, value, mutate):
            value[0] = 1
        with patch("satip_appliance.watchdog.fcntl.ioctl", side_effect=ioctl), self.assertRaises(ValueError):
            configure(1)


if __name__ == "__main__":
    unittest.main()
