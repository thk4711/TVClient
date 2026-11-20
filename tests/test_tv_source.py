"""Source persistence/recovery tests; never connect to or tune a receiver."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from satip_appliance import tv_source
from satip_controller.state import atomic_json
from satip_controller.enigma2 import profile_networks


class SourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name)
        self.data = json.loads((Path(__file__).parent / "fixtures/library.json").read_text())
        self.profile = {"version": 1, "network_uuid": "a"*32, "library": self.data, "candidates": {"channels": []}}
        self.backend = AsyncMock()

    def write(self, relative, data):
        atomic_json(self.store / relative, data)

    def test_targeted_import_appends_without_replacing_existing_lineup(self):
        previous = {**self.profile, 'candidates': {'channels': [dict(c) for c in self.data['channels']]}}
        original = json.dumps(previous, sort_keys=True)
        found = [{'name': 'RTL Television', 'service_uuid': 'd'*32}, {'name': 'SAT.1', 'service_uuid': 'e'*32}]
        chosen, catalogue = tv_source.import_selection(previous, found, ['RTL Television', 'SAT.1'], self.data)
        channels = {c['uuid']: c for c in self.data['channels']}
        self.assertEqual(chosen, [channels[c]['service_uuid'] for c in self.data['favourites']] + ['d'*32, 'e'*32])
        self.assertEqual(len(catalogue), len(self.data['channels']) + 2)
        self.assertEqual(json.dumps(previous, sort_keys=True), original)

    def test_targeted_import_requires_every_requested_channel_to_pass(self):
        from satip_controller.backend import BackendError
        with self.assertRaises(BackendError):
            tv_source.import_selection(self.profile, [{'name': 'RTL Television', 'service_uuid': 'd'*32}],
                                       ['RTL Television', 'SAT.1'], self.data)

    def test_source_status_uses_network_identity_not_presence(self):
        self.write("config/web/config.json", {"network_uuid": "b"*32})
        self.write("web/tv-source-enigma2.json", self.profile)
        self.assertEqual(tv_source.public_status(self.store), {"active": "satip", "available": ["satip", "enigma2"]})
        self.write("config/web/config.json", {"network_uuid": "a"*32})
        self.assertEqual(tv_source.public_status(self.store)["active"], "enigma2")

    def test_acl_validation_networks_require_valid_profiles(self):
        self.write("web/tv-source-satip.json", self.profile)
        self.assertEqual(profile_networks(self.store / "web", "b"*32), {"a"*32, "b"*32})
        self.write("web/tv-source-satip.json", {**self.profile, "network_uuid": "../escape"})
        with self.assertRaises(ValueError):
            profile_networks(self.store / "web", "b"*32)

    async def test_selection_journal_restores_exact_old_fixed_paths(self):
        old = {"config": {"network_uuid": "a"*32}, "library": self.data, "candidates": {"channels": []},
               "networks": [{"uuid": "a"*32, "enabled": True}, {"uuid": "b"*32, "enabled": False}]}
        self.write("web/tv-source-transaction.json", old)
        with patch.object(tv_source, "owned_write", side_effect=lambda path, node: atomic_json(path, node)):
            await tv_source.recover(self.backend, self.store)
        self.backend.save.assert_any_await(uuid="a"*32, enabled=True)
        self.backend.save.assert_any_await(uuid="b"*32, enabled=False)
        self.assertEqual(json.loads((self.store / "management/library.json").read_text()), self.data)
        self.assertFalse((self.store / "web/tv-source-transaction.json").exists())

    async def test_failed_restore_keeps_journal_for_retry(self):
        self.write("web/tv-source-transaction.json", {"config": {"network_uuid": "a"*32}, "library": self.data,
                   "candidates": {"channels": []}, "networks": [{"uuid": "a"*32, "enabled": True}]})
        self.backend.save.side_effect = OSError("unavailable")
        with self.assertRaises(OSError):
            await tv_source.recover(self.backend, self.store)
        self.assertTrue((self.store / "web/tv-source-transaction.json").exists())

    async def test_import_recovery_restores_mux_and_network_without_paths_from_input(self):
        mux = {"uuid": "c"*32, "enabled": 0, "epg": 0, "scan_state": 0}
        self.write("web/enigma2-import/mux-rollback.json", mux)
        self.write("web/enigma2-import/network-rollback.json", {"uuid": "a"*32, "enabled": False})
        with patch.object(tv_source, "owned_write", side_effect=lambda path, node: atomic_json(path, node)), \
                patch.object(tv_source, "runtime_path", side_effect=lambda path, name: self.store / name):
            await tv_source.recover(self.backend, self.store)
        self.backend.save.assert_any_await(**mux)
        self.backend.save.assert_any_await(uuid="a"*32, enabled=False)
        self.assertFalse((self.store / "web/enigma2-import/mux-rollback.json").exists())
        self.assertFalse((self.store / "web/enigma2-import/network-rollback.json").exists())
        self.assertEqual(json.loads((self.store / "management/recovery.json").read_text()), {"pending": False})


if __name__ == "__main__":
    unittest.main()
