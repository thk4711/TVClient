"""Browser source import contracts; isolated backend, never tune a real source."""
import asyncio
import copy
import time
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from satip_appliance import source_browser, source_api, setup, web, tv_source, startup
from satip_controller.backend import BackendError
from satip_controller.enigma2 import Enigma2
from satip_controller.scan import Manager
from satip_controller.state import atomic_json, read_json
from tests.test_setup import FreshFixture, FakeBackend, NETWORK, MUX, SERVICE
from tests.test_enigma2 import REF, BOUQUET


class IPTVBackend(FakeBackend):
    async def request(self, endpoint, write=False, **values):
        if endpoint == "mpegts/network/builders":
            return {"entries": [{"class": "iptv_network"}]}
        if endpoint == "mpegts/network/mux_create":
            self.calls.append((endpoint, values))
            self.muxes.append({"uuid": MUX, "network_uuid": values["uuid"], **values["conf"]})
            return {"uuid": MUX}
        return await super().request(endpoint, write, **values)

    async def save(self, **values):
        if self.fail_restore and values.get("enabled") is False and values["uuid"] == NETWORK:
            raise BackendError("fixture restore failure")
        await super().save(**values)
        for node in self.networks:
            if node["uuid"] == values["uuid"]:
                node.update(values)


async def scanned(manager, **kwargs):
    manager.status = {"state": "complete", "found": 1, "completed": 1, "total": 1}
    manager.scanned_muxes = kwargs["mux_ids"]
    manager.publish_candidates(await manager.candidates(muxes=kwargs["mux_ids"]))


class BrowserImportTests(FreshFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = IPTVBackend(self.factory)
        self.config = {"network_uuid": "0"*32, "private_dir": str(self.store / "web"),
                       "library": str(self.store / "management/library.json"),
                       "management_dir": str(self.store / "management"),
                       "player_state_dir": str(self.store / "player"),
                       "playback_acl_uuid": self.factory["ids"]["playback_acl"], "mux_timeout": 5}
        self.importer = source_browser.Importer(self.backend, self.config, self.store)
        self.receiver = Enigma2("192.168.1.2")
        patch.object(Enigma2, "about", return_value={"model": "Fixture", "address": self.receiver.host}).start()
        patch.object(Enigma2, "services", return_value=[{"reference": REF, "name": "Fixture TV"}]).start()
        patch.object(Manager, "quiet", new=AsyncMock()).start()
        self.original_scan = Manager.scan
        patch.object(Manager, "scan", new=scanned).start()

    async def import_channels(self):
        self.importer.start(self.receiver, ["Free"])
        await self.importer.task
        return self.importer.status

    async def test_first_source_import_has_no_satip_prerequisite(self):
        self.assertEqual((await self.import_channels())["state"], "complete")
        profile = tv_source.validate_profile(read_json(tv_source.profile_path(self.store, "enigma2")))
        self.assertEqual(profile["network_uuid"], NETWORK)
        self.assertEqual(len(profile["candidates"]["channels"]), 1)
        self.assertFalse((self.store / "config/web/config.json").exists())
        self.assertFalse(self.backend.networks[0]["enabled"])
        self.assertEqual(profile["enigma2"]["services"], {MUX: REF})
        self.assertFalse((self.store / "web/enigma2-import/network-rollback.json").exists())

    async def test_repeated_import_reuses_mux_and_favourite_identities(self):
        await self.import_channels()
        original = read_json(tv_source.profile_path(self.store, "enigma2"))
        await self.import_channels()
        result = read_json(tv_source.profile_path(self.store, "enigma2"))
        self.assertEqual(result["library"], original["library"])
        self.assertEqual(len(self.backend.networks), 1)
        self.assertEqual(len(self.backend.muxes), 1)

    async def test_empty_validation_retains_profile(self):
        await self.import_channels()
        path = tv_source.profile_path(self.store, "enigma2")
        before = path.read_bytes()
        with patch.object(Manager, "candidates", new=AsyncMock(return_value=[])):
            self.assertEqual((await self.import_channels())["state"], "error")
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.backend.networks[0]["enabled"])

    async def test_failed_reprobe_does_not_publish_cached_stream_metadata(self):
        await self.import_channels()
        path = tv_source.profile_path(self.store, "enigma2")
        before = path.read_bytes()
        async def failed(manager, **kwargs):
            manager.scanned_muxes = []
            manager.status = {"state": "complete", "found": 0, "failed_muxes": 1}
        with patch.object(Manager, "scan", new=failed):
            self.assertEqual((await self.import_channels())["state"], "error")
        self.assertEqual(path.read_bytes(), before)

    async def test_regular_scan_and_refresh_stay_within_selected_bouquets(self):
        await self.import_channels()
        self.config["network_uuid"] = NETWORK
        other_mux, other_service = "9" * 32, "a" * 32
        self.backend.muxes.append({"uuid": other_mux, "network_uuid": NETWORK, "enabled": 0, "epg": 0, "scan_state": 0})
        original_grid, original_save = self.backend.grid, self.backend.save
        async def grid(endpoint):
            rows = await original_grid(endpoint)
            if endpoint == "mpegts/service/grid":
                rows.append({"uuid": other_service, "multiplex_uuid": other_mux, "encrypted": False,
                             "enabled": True, "svcname": "Deselected bouquet channel"})
            return rows
        async def save(**values):
            await original_save(**values)
            if values.get("scan_state") == 1:
                mux = next(m for m in self.backend.muxes if m["uuid"] == values["uuid"])
                mux.update(scan_state=0, scan_last=int(time.time()), scan_result=1)
        manager = Manager(self.backend, self.config, setup=True)
        with patch.object(self.backend, "grid", new=grid), patch.object(self.backend, "save", new=save), \
                patch("satip_controller.astra.supplement", new=AsyncMock(return_value=0)), \
                patch.object(Manager, "scan", new=self.original_scan):
            self.assertEqual(len(await manager.candidates()), 1)
            # ACL expansion still revalidates all retained managed mappings.
            self.assertEqual(len(await manager.candidates(networks={NETWORK})), 2)
            await manager.scan()
        self.assertEqual(manager.status["total"], 1)
        self.assertEqual(manager.scanned_muxes, [MUX])

    async def test_selected_scope_rejects_unknown_or_duplicate_muxes(self):
        await self.import_channels()
        profile = read_json(tv_source.profile_path(self.store, "enigma2"))
        for selected in ([], [MUX, MUX], ["e" * 32], [[MUX]]):
            node = copy.deepcopy(profile)
            node["enigma2"]["selected_muxes"] = selected
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                tv_source.validate_profile(node)

    async def test_cancel_restores_network_and_keeps_lineup(self):
        await self.import_channels()
        path = tv_source.profile_path(self.store, "enigma2")
        before = path.read_bytes()
        started = asyncio.Event()
        async def wait(manager, **kwargs):
            started.set()
            await asyncio.Event().wait()
        with patch.object(Manager, "scan", new=wait):
            self.importer.start(self.receiver, ["Free"])
            await started.wait()
            await self.importer.close()
        self.assertEqual(self.importer.status["state"], "cancelled")
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.backend.networks[0]["enabled"])

    async def test_restore_failure_keeps_journal_and_recovery_gate(self):
        self.backend.fail_restore = True
        status = await self.import_channels()
        self.assertEqual(status["state"], "error")
        self.assertTrue(status["recovery_pending"])
        self.assertTrue((self.store / "web/enigma2-import/network-rollback.json").exists())
        self.assertEqual(read_json(self.store / "management/recovery.json"), {"pending": True})

    async def test_receiver_change_is_rejected_without_second_network(self):
        await self.import_channels()
        self.importer.start(Enigma2("192.168.1.3"), ["Free"])
        await self.importer.task
        self.assertEqual(self.importer.status["state"], "error")
        self.assertEqual(len(self.backend.networks), 1)

    async def test_active_import_snapshots_live_favourites_and_publishes(self):
        await self.import_channels()
        profile = read_json(tv_source.profile_path(self.store, "enigma2"))
        self.config["network_uuid"] = NETWORK
        atomic_json(Path(self.config["library"]), profile["library"])
        atomic_json(self.store / "web/candidates.json", profile["candidates"])
        owner = Manager(self.backend, self.config)
        self.importer.owner = owner
        await self.import_channels()
        self.assertEqual(read_json(Path(self.config["library"])), profile["library"])
        self.assertEqual(owner.catalogue_revision, 1)
        self.assertFalse((self.store / "web/enigma2-import/publish-rollback.json").exists())

    async def test_first_setup_accepts_import_then_finishes_without_tuners(self):
        await self.import_channels()
        preferences = read_json(self.store / "config/web/setup.json")
        wizard = setup.Setup(self.backend, self.store, preferences, ["testserver"])
        wizard.source_importer = self.importer
        wizard.accept_enigma2()
        self.assertEqual(wizard.state["source"], "enigma2")
        await wizard.finish([SERVICE])
        self.assertTrue(self.backend.networks[0]["enabled"])
        self.assertEqual(read_json(self.store / "config/web/config.json")["network_uuid"], NETWORK)
        profile = read_json(tv_source.profile_path(self.store, "enigma2"))
        self.assertEqual(profile["library"], read_json(wizard.manager.library_path))
        self.assertEqual(tv_source.public_status(self.store), {"active": "enigma2", "available": ["enigma2"]})
        gate = startup.StartupGate(self.root)
        with patch.object(startup, "address", return_value="192.168.1.9"), \
                patch.object(startup, "route_available", return_value=True) as route, \
                patch.object(startup.socket, "create_connection", return_value=Mock(__enter__=Mock(), __exit__=Mock())) as connect, \
                patch.object(startup.Backend, "_request", return_value={"entries": self.backend.networks}) as request:
            self.assertTrue(gate.backend("http://192.168.1.99/old-satip.xml"))
            self.assertTrue(gate.player("http://192.168.1.99/old-satip.xml"))
            self.assertEqual(route.call_args.args[0], "http://192.168.1.2:80")
            self.assertEqual(connect.call_args.args[0], ("192.168.1.2", 80))
            self.assertEqual(request.call_args.args[0], "mpegts/network/grid")
            self.assertEqual(gate.previous, "ready")
            self.backend.networks[0]["enabled"] = False
            self.assertFalse(gate.player(None))

    async def test_publication_journal_restores_old_profile_and_lineup(self):
        await self.import_channels()
        profile = read_json(tv_source.profile_path(self.store, "enigma2"))
        atomic_json(self.store / "management/library.json", profile["library"])
        atomic_json(self.store / "web/candidates.json", profile["candidates"])
        atomic_json(self.store / "web/enigma2-import/publish-rollback.json", {
            "profile": profile, "library": profile["library"], "candidates": profile["candidates"]})
        changed = copy.deepcopy(profile)
        changed["candidates"]["channels"] = []
        atomic_json(tv_source.profile_path(self.store, "enigma2"), changed)
        source_browser.recover_publication(self.store)
        self.assertEqual(read_json(tv_source.profile_path(self.store, "enigma2")), profile)

    async def test_failed_active_publication_rolls_back_both_profile_and_library(self):
        await self.import_channels()
        path = tv_source.profile_path(self.store, "enigma2")
        profile = read_json(path)
        self.config["network_uuid"] = NETWORK
        atomic_json(Path(self.config["library"]), profile["library"])
        atomic_json(self.store / "web/candidates.json", profile["candidates"])
        self.importer.owner = Manager(self.backend, self.config)
        failed = False
        def write(path, value, *args, **kwargs):
            nonlocal failed
            if Path(path) == Path(self.config["library"]) and not failed:
                failed = True
                raise OSError("fixture publication failure")
            return atomic_json(path, value, *args, **kwargs)
        with patch.object(source_browser, "atomic_json", side_effect=write):
            self.assertEqual((await self.import_channels())["state"], "error")
        self.assertTrue(failed)
        self.assertEqual(read_json(path), profile)
        self.assertEqual(read_json(Path(self.config["library"])), profile["library"])
        self.assertFalse((self.store / "web/enigma2-import/publish-rollback.json").exists())

    async def test_retry_during_first_setup_does_not_create_a_satip_profile(self):
        await self.import_channels()
        wizard = setup.Setup(self.backend, self.store, read_json(self.store / "config/web/setup.json"), ["testserver"])
        wizard.source_importer = self.importer
        wizard.accept_enigma2()
        self.importer.owner = wizard.manager
        self.assertEqual((await self.import_channels())["state"], "complete")
        wizard.accept_enigma2()
        self.assertFalse(tv_source.profile_path(self.store, "satip").exists())

    async def test_cancelled_initial_import_keeps_receiver_identity_for_retry(self):
        preferences = read_json(self.store / "config/web/setup.json")
        wizard = setup.Setup(self.backend, self.store, preferences, ["testserver"])
        self.importer.prepare = wizard.prepare_enigma2
        entered = asyncio.Event()
        async def wait(manager, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(Manager, "scan", new=wait):
            self.importer.start(self.receiver, ["Free"])
            await entered.wait()
            await self.importer.close()
        self.assertEqual(wizard.state["source"], "enigma2")
        self.assertFalse(wizard.state["prepared"])
        resumed = setup.Setup(self.backend, self.store, preferences, ["testserver"])
        self.assertEqual(resumed.state["receiver"]["address"], self.receiver.host)
        self.assertFalse(resumed.state["prepared"])
        with self.assertRaises(BackendError):
            await resumed.finish([SERVICE])


class SourceAPITests(FreshFixture):
    def test_appliance_alias_resolves_to_persistent_source_store(self):
        from satip_controller.web import create_app
        from tests.helpers import Fixture
        fixture = Fixture()
        fixture.addCleanup = self.addCleanup
        fixture.setup_files()
        original = Path(fixture.web_config["private_dir"])
        original.mkdir()
        alias = fixture.root / "appliance-web-alias"
        alias.symlink_to(original, target_is_directory=True)
        fixture.web_config["private_dir"] = str(alias)
        app = create_app(fixture.web_config, backend=AsyncMock(), appliance=True)
        self.assertEqual(app.state.source_importer.store, fixture.root.resolve())
        client = TestClient(app)
        self.assertEqual(client.get("/api/sources").status_code, 401)
        self.assertEqual(client.post("/api/sources/discover", json={"source": "enigma2"}).status_code, 401)

    def test_interrupted_import_is_visible_after_web_restart(self):
        atomic_json(self.store / "web/enigma2-import/network-rollback.json", {"uuid": NETWORK, "enabled": False})
        app = web.create_setup_app(self.store, IPTVBackend(self.factory), hosts=["testserver"])
        with TestClient(app) as client:
            status = client.get("/api/sources").json()["import"]
            self.assertTrue(status["recovery_pending"])
            self.assertEqual(status["state"], "error")
            response = client.post("/api/sources/import", json={"address": "192.168.1.2",
                "bouquets": ["Free"], "confirm_pause": True}, headers={"X-Satip-Action": "1"})
            self.assertEqual(response.status_code, 409)

    def test_setup_source_api_is_read_only_until_confirmed_import(self):
        backend = IPTVBackend(self.factory)
        app = web.create_setup_app(self.store, backend, hosts=["testserver"])
        with TestClient(app) as client, patch.object(Enigma2, "about", return_value={"model": "Fixture"}), \
                patch.object(Enigma2, "bouquets", return_value=[{"name": "Free", "reference": BOUQUET}]):
            response = client.post("/api/sources/bouquets", json={"address": "192.168.1.2"}, headers={"X-Satip-Action": "1"})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(backend.calls)
            self.assertEqual(client.post("/api/sources/import", json={"address": "192.168.1.2", "bouquets": ["Free"]},
                headers={"X-Satip-Action": "1"}).status_code, 400)
            self.assertEqual(client.post("/api/sources/bouquets", json={"address": "127.0.0.1"},
                headers={"X-Satip-Action": "1"}).status_code, 400)
            self.assertEqual(client.post("/api/sources/import", json={"address": "192.168.1.2", "bouquets": ["Free", "Free"], "confirm_pause": True},
                headers={"X-Satip-Action": "1"}).status_code, 400)
            self.assertEqual(client.get("/sources.js").status_code, 200)

    def test_discovery_dispatches_source_type(self):
        app = web.create_setup_app(self.store, IPTVBackend(self.factory), hosts=["testserver"])
        result = {"servers": [{"address": "192.168.1.2", "model": "Fixture"}], "interfaces": ["eth0"], "warnings": []}
        with TestClient(app) as client, patch.object(source_api, "search_enigma2", return_value=result) as enigma, \
                patch.object(source_api.discovery, "search_servers", return_value=result) as satip:
            for source in ("enigma2", "satip"):
                response = client.post("/api/sources/discover", json={"source": source}, headers={"X-Satip-Action": "1"})
                self.assertEqual(response.status_code, 202)
            client.get("/api/sources")
        enigma.assert_called_once()
        satip.assert_called_once()

    def test_discovery_probes_only_bounded_local_targets(self):
        with patch.object(source_browser.discovery, "interfaces", return_value=[("eth0", "192.168.1.9")]), \
                patch.object(source_browser.discovery, "local_targets", return_value=["192.168.1.2"]), \
                patch.object(source_browser.socket, "create_connection", return_value=Mock(__enter__=Mock(), __exit__=Mock())), \
                patch.object(Enigma2, "about", return_value={"model": "Fixture"}) as about:
            self.assertEqual(source_browser.search_enigma2()["servers"], [{"model": "Fixture"}])
            about.assert_called_once()


if __name__ == "__main__":
    unittest.main()
