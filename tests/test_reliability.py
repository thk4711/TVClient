import asyncio
import copy
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from satip_controller.backend import Backend
from satip_controller.coordination import preview_held
from satip_controller.lifecycle import complete
from satip_controller.scan import Manager, ConcurrentEdit
from satip_controller.state import (JSONCache, JSONWriter, atomic_json, read_json,
                                    lease_data, lease_active, library_revision)
from satip_controller.web import create_app
from tests.helpers import Fixture


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_cancellation_cannot_overtake_backend_mutation(self):
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        writes = []
        backend = Backend({"username": "fake", "password": "fake"})
        def request(endpoint, values, write):
            value = values["node"]["enabled"]
            if value:
                loop.call_soon_threadsafe(entered.set)
                release.wait(5)
            writes.append(value)
            return {}
        backend._request = request
        async def transaction():
            try:
                await backend.save(enabled=1)
            finally:
                await backend.save(enabled=0)
        manager = object.__new__(Manager)
        manager.task = asyncio.create_task(transaction())
        await entered.wait()
        first = asyncio.create_task(manager.close())
        await asyncio.sleep(.01)
        second = asyncio.create_task(manager.close())
        manager.task.cancel()  # Independent shutdown during the first cancel.
        first.cancel()         # An interrupted HTTP cancellation request.
        await asyncio.sleep(.01)
        try:
            self.assertFalse(manager.task.done())
            self.assertEqual(writes, [])
        finally:
            release.set()
            await asyncio.gather(first, second, return_exceptions=True)
        self.assertEqual(writes, [1, 0])

    async def test_completion_retains_owned_failure(self):
        async def fail():
            raise OSError("fixture")
        with self.assertRaises(OSError):
            await complete(asyncio.create_task(fail()))


class StorageTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_files()

    async def asyncTearDown(self):
        await self.state.close()

    async def test_cancelled_write_finishes_before_following_snapshot(self):
        writer = JSONWriter()
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        snapshots = []
        def write(path, data, mode):
            if data["number"] == 1:
                loop.call_soon_threadsafe(entered.set)
                release.wait(5)
            snapshots.append(data["number"])
        original = {"number": 1}
        with patch("satip_controller.state.atomic_json", side_effect=write):
            first = asyncio.create_task(writer.write(self.root / "data", original))
            await entered.wait()
            original["number"] = 9
            first.cancel()
            second = asyncio.create_task(writer.write(self.root / "data", {"number": 2}))
            await asyncio.sleep(.01)
            try:
                self.assertEqual(snapshots, [])
                self.assertFalse(first.done())
            finally:
                release.set()
                await asyncio.gather(first, second, return_exceptions=True)
        self.assertEqual(snapshots, [1, 2])

    async def test_failed_state_write_is_visible_and_retried_on_close(self):
        with patch.object(self.state.writer, "write", new=AsyncMock(side_effect=OSError("disk full"))):
            self.state.update(volume=60)
            self.state.changed_at = time.monotonic() - 3
            async with asyncio.timeout(3):
                while self.state.error is None:
                    await asyncio.sleep(.01)
            self.assertTrue(self.state.dirty)
            self.assertEqual(self.state.error, "state_write_failed")
        await self.state.close()
        self.assertEqual(read_json(self.state.path)["volume"], 60)
        self.assertIsNone(self.state.error)

    async def test_cache_detects_atomic_replacement_and_recovers_from_bad_json(self):
        cache = JSONCache()
        path = self.root / "cache.json"
        atomic_json(path, {"value": 1})
        self.assertEqual(cache.read(path), {"value": 1})
        atomic_json(path, {"value": 2})
        self.assertEqual(cache.read(path), {"value": 2})
        path.write_text("{")
        with self.assertRaises(ValueError):
            cache.read(path)
        atomic_json(path, {"value": 3})
        self.assertEqual(cache.read(path), {"value": 3})


class LeaseTests(unittest.TestCase):
    def test_wallclock_corrections_do_not_change_lease_validity(self):
        with patch("satip_controller.state.time.monotonic", return_value=100):
            lease = lease_data(15)
        for wallclock in (0, 99999999999):
            with patch("satip_controller.state.time.time", return_value=wallclock), \
                 patch("satip_controller.state.time.monotonic", return_value=110):
                self.assertTrue(lease_active(lease))
        with patch("satip_controller.state.time.monotonic", return_value=116):
            self.assertFalse(lease_active(lease))
        self.assertFalse(lease_active({**lease, "boot_id": "another boot"}))
        self.assertFalse(lease_active({"until": 99999999999}))

    def test_expiry_cannot_revoke_encoder_still_cleaning_up(self):
        lease = lease_data(-1, active=True, owners=[{"pid": 123, "start": "42", "boot_id": "test"}])
        with patch("satip_controller.coordination.process_identity", return_value=lease["owners"][0]):
            self.assertTrue(preview_held(lease))
            self.assertFalse(preview_held({**lease, "active": False}))
        with patch("satip_controller.coordination.process_identity", return_value=None):
            self.assertFalse(preview_held(lease))


class WebReliabilityTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup_files()
        self.backend = AsyncMock()
        self.hosts = ["testserver"]
        self.app = create_app(self.web_config, self.backend, hosts_provider=lambda: self.hosts)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.client.auth = ("web", "private-web-password")
        self.client.headers["x-satip-action"] = "1"

    def test_delayed_dhcp_and_address_change_without_restart(self):
        def get(host):
            return self.client.get("/", headers={"host": host + ":8080"}).status_code
        self.assertEqual(get("192.0.2.5"), 400)
        self.hosts.append("192.0.2.5")
        self.assertEqual(get("192.0.2.5"), 200)
        self.hosts.remove("192.0.2.5")
        self.hosts.append("192.0.2.6")
        self.assertEqual(get("192.0.2.5"), 400)
        self.assertEqual(get("192.0.2.6"), 200)
        self.assertEqual(get("evil.example"), 400)

    def test_body_limits_precede_parsing_for_declared_and_chunked_input(self):
        for content, headers in ((b"x" * 32769, {}),
                                 (iter([b"x" * 16000] * 3), {}),
                                 (b"x" * 32769, {"content-length": "1"})):
            response = self.client.put("/api/favourites", content=content, headers=headers)
            self.assertEqual(response.status_code, 413)
        self.backend.save.assert_not_awaited()

    def test_malformed_host_authorities_are_rejected(self):
        for host in ("testserver:8080/api/device?ignored=", "testserver/path",
                     "testserver?query", "testserver#fragment",
                     "user@testserver", "testserver:", "testserver:0",
                     "testserver:65536", "testserver:abc", "testserver "):
            with self.subTest(host=host):
                self.assertEqual(self.client.get("/", headers={"host": host}).status_code, 400)
        for host in ("testserver", "testserver:8080", "TESTSERVER:8080"):
            self.assertEqual(self.client.get("/", headers={"host": host}).status_code, 200)
        self.assertEqual(self.client.get("/", headers=[("host", "testserver"),
                                                       ("host", "evil.example")]).status_code, 400)

    def test_strict_requests_and_revision_precondition(self):
        response = self.client.post("/api/scan", json={"confirm_pause": "true"})
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/api/scan", json={"confirm_pause": True, "extra": True})
        self.assertEqual(response.status_code, 422)
        response = self.client.put("/api/favourites", json={"services": ["a" * 32]})
        self.assertEqual(response.status_code, 428)
        self.assertEqual(self.client.get("/api/channels").json()["library_revision"], library_revision(self.data))

    def test_stale_save_rejected_without_backend_mutation(self):
        response = self.client.put("/api/favourites", json={"services": ["a" * 32], "base_revision": "0" * 64})
        self.assertEqual(response.status_code, 409)
        self.backend.grid.assert_not_awaited()
        self.assertEqual(read_json(self.config.library), self.data)

    def test_revision_survives_service_restart(self):
        old = self.client.get("/api/channels").json()["library_revision"]
        changed = copy.deepcopy(self.data)
        changed["favourites"].reverse()
        atomic_json(self.config.library, changed)
        with TestClient(create_app(self.web_config, self.backend)) as restarted:
            response = restarted.get("/api/channels", auth=self.client.auth)
            self.assertNotEqual(response.json()["library_revision"], old)
            self.assertEqual(response.json()["library_revision"], library_revision(changed))
