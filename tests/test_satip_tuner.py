"""Tuner changes use backend identities, preserve lineups and recover failures."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from satip_appliance import satip_tuner
from satip_controller.backend import BackendError
from satip_controller.state import atomic_json, read_json

SERVER, OLD, NEW, P1, P2, NETWORK = [str(i)*32 for i in range(1, 7)]
UDN = "uuid:28146e70-1dd2-11b2-8021-c19f52d1fd53"


def node(fields, classname):
    return {"uuid": fields["uuid"], "class": classname,
            "params": [{"id": k, "value": v} for k, v in fields.items() if k != "uuid"]}


class Backend:
    def __init__(self):
        self.frontends = {OLD: {"uuid": OLD, "enabled": True, "fe_number": 2, "tdelay": 50, "idle_grace": 1000,
                                "fe_master": 0, "initscan": True, "idlescan": False},
                          NEW: {"uuid": NEW, "enabled": False, "fe_number": 1, "tdelay": 0,
                                "idle_grace": 0, "fe_master": 0, "initscan": False, "idlescan": False}}
        self.positions = {OLD: [{"uuid": P1, "enabled": True, "position": 1, "networks": [NETWORK]}],
                          NEW: [{"uuid": P2, "enabled": False, "position": 1, "networks": []}]}
        self.fail = None
        self.subscriptions = []
        self.writes = []

    async def request(self, endpoint, **params):
        if endpoint == "idnode/load":
            return {"entries": [node(t, "satip_frontend_dvbs") for t in self.frontends.values()]}
        if params["uuid"] == "root":
            return [node({"uuid": SERVER, "device_uuid": UDN[5:], "active": True,
                          "addr": "192.168.1.2", "friendly": "Fixture Twin"}, "satip_client")]
        if params["uuid"] == SERVER:
            return [node(t, "satip_frontend_dvbs") for t in self.frontends.values()]
        return [node(p, "satip_satconf") for p in self.positions[params["uuid"]]]

    async def grid(self, endpoint):
        if endpoint == "status/subscriptions":
            return self.subscriptions
        return [{"input": "SAT>IP DVB-S Tuner #2 (192.168.1.2@UDP)", "bps": 6000000}]

    async def save(self, **params):
        self.writes.append(params)
        if self.fail and self.fail(params):
            self.fail = None
            raise BackendError("fixture write failure")
        target = self.frontends.get(params["uuid"])
        if target is None:
            target = next(p for positions in self.positions.values() for p in positions if p["uuid"] == params["uuid"])
        target.update(params)


class TunerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Path(temporary.name)
        atomic_json(self.store / "config/web/config.json", {"network_uuid": NETWORK})
        atomic_json(self.store / "web/server-selection.json", {"udn": UDN, "address": "192.168.1.2", "name": "Twin"})
        atomic_json(self.store / "web/setup-state.json", {"frontend_uuid": OLD, "network_uuid": NETWORK})
        self.backend = Backend()
        self.addCleanup(patch.stopall)
        patch('satip_appliance.tv_source.owned_write', side_effect=lambda p, n: atomic_json(p, n)).start()

    async def test_inventory_separates_configured_and_receiving_tuner(self):
        result = await satip_tuner.inventory(self.backend, self.store)
        self.assertEqual(result["name"], "Fixture Twin")
        self.assertEqual(result["configured_frontend"], OLD)
        self.assertEqual([t["number"] for t in result["tuners"]], [1, 2])
        self.assertIn("#2", result["receiving"][0])
        self.assertEqual(self.backend.writes, [])

    async def test_no_data_input_is_tuning_not_receiving(self):
        from unittest.mock import AsyncMock
        self.backend.grid = AsyncMock(return_value=[{"input": "Tuner #1 (192.168.1.2)", "bps": 0}])
        result = await satip_tuner.inventory(self.backend, self.store)
        self.assertEqual(result["receiving"], [])
        self.assertEqual(result["tuning"], ["Tuner #1 (192.168.1.2)"])

    async def test_selection_keeps_channels_and_copies_reception_policy(self):
        lineup = {"fixture": "must not change"}
        atomic_json(self.store / "management/library.json", lineup)
        await satip_tuner.select(self.backend, NEW, self.store)
        self.assertFalse(self.backend.frontends[OLD]["enabled"])
        self.assertTrue(self.backend.frontends[NEW]["enabled"])
        self.assertEqual(self.backend.frontends[NEW]["tdelay"], 50)
        self.assertEqual(self.backend.frontends[NEW]["idle_grace"], 1000)
        self.assertEqual(self.backend.frontends[NEW]["fe_number"], 1)
        self.assertEqual(self.backend.positions[NEW][0]["networks"], [NETWORK])
        self.assertEqual(read_json(self.store / "web/setup-state.json")["frontend_uuid"], NEW)
        self.assertEqual(read_json(self.store / "management/library.json"), lineup)
        self.assertFalse((self.store / satip_tuner.JOURNAL).exists())

    async def test_failed_assignment_rolls_back_exact_settings(self):
        before = copy.deepcopy((self.backend.frontends, self.backend.positions))
        self.backend.fail = lambda p: p["uuid"] == NEW and p.get("enabled")
        with self.assertRaises(BackendError):
            await satip_tuner.select(self.backend, NEW, self.store)
        self.assertEqual((self.backend.frontends, self.backend.positions), before)
        self.assertEqual(read_json(self.store / "web/setup-state.json")["frontend_uuid"], OLD)
        self.assertFalse((self.store / satip_tuner.JOURNAL).exists())

    async def test_rejects_unknown_tuner_and_active_subscription_before_writing(self):
        with self.assertRaises(ValueError):
            await satip_tuner.select(self.backend, "a"*32, self.store)
        self.backend.subscriptions = [{"fixture": "busy"}]
        with self.assertRaises(BackendError):
            await satip_tuner.select(self.backend, NEW, self.store)
        self.assertEqual(self.backend.writes, [])

    async def test_rejects_linked_wiring_and_missing_satellite_position(self):
        self.backend.frontends[NEW]["fe_master"] = 2
        with self.assertRaises(ValueError):
            await satip_tuner.select(self.backend, NEW, self.store)
        self.backend.frontends[NEW]["fe_master"] = 0
        self.backend.positions[NEW][0]["position"] = 2
        with self.assertRaises(ValueError):
            await satip_tuner.select(self.backend, NEW, self.store)
        self.assertEqual(self.backend.writes, [])

    async def test_failed_recovery_keeps_journal_and_retry_restores(self):
        journal = {"frontends": [{"uuid": OLD, "enabled": True}, {"uuid": NEW, "enabled": False}],
                   "positions": [{"uuid": P1, "enabled": True, "networks": [NETWORK]}], "selection": None}
        atomic_json(self.store / satip_tuner.JOURNAL, journal)
        self.backend.fail = lambda p: p["uuid"] == P1
        with self.assertRaises(BackendError):
            await satip_tuner.recover(self.backend, self.store)
        self.assertTrue((self.store / satip_tuner.JOURNAL).exists())
        await satip_tuner.recover(self.backend, self.store)
        self.assertTrue(self.backend.frontends[OLD]["enabled"])
        self.assertFalse((self.store / satip_tuner.JOURNAL).exists())
