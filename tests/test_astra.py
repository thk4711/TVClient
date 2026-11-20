import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from satip_controller.astra import sections, supplement, transponders
from satip_controller.backend import BackendError
from tests import test_lifecycle

TABLE = """[ProSiebenSat.1]
DELIVERY_SYSTEM = DVBS
FREQUENCY = 12545000
POLARIZATION = HORIZONTAL
SYMBOL_RATE = 22000000
[ProSiebenSat.1]
DELIVERY_SYSTEM = DVBS
FREQUENCY = 12051000
POLARIZATION = VERTICAL
SYMBOL_RATE = 27500000
"""


class AstraTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        (self.directory / "Astra-1L-19.2E").write_text(TABLE)
        (self.directory / "Astra-1N-19.2E").write_text(TABLE)
        (self.directory / "Astra-2E-28.2E").write_text(TABLE.replace("12545000", "12345000"))

    def test_repeated_names_and_overlapping_files_preserve_both_muxes(self):
        self.assertEqual(len(list(sections(TABLE))), 2)
        muxes = transponders(self.directory)
        self.assertEqual(len(muxes), 2)
        self.assertEqual(muxes[0], {"frequency": 12545000, "polarisation": "H",
            "symbolrate": 22000000, "delsys": "DVB-S", "modulation": "QPSK", "fec": "AUTO"})

    def test_dvbs2_modulation_names_match_tvheadend(self):
        (self.directory / "Astra-1L-19.2E").write_text(TABLE.replace("DVBS", "DVBS2") + "MODULATION = APSK/16\n")
        self.assertEqual(transponders(self.directory)[1]["modulation"], "16APSK")

    def test_ka_band_feeds_are_not_imported_for_conventional_lnb(self):
        (self.directory / "Astra-1L-19.2E").write_text(TABLE + TABLE.replace("12545000", "18507000"))
        self.assertEqual(len(transponders(self.directory)), 2)

    def test_missing_or_invalid_tables_fail_closed(self):
        with self.assertRaises(BackendError):
            transponders(self.directory / "missing")
        (self.directory / "Astra-1L-19.2E").write_text(TABLE.replace("12545000", "1"))
        with self.assertRaises(BackendError):
            transponders(self.directory)

    async def test_import_is_additive_idempotent_and_disabled(self):
        existing = {"uuid": "old", "network_uuid": "network", "frequency": 12051000,
                    "polarisation": "V", "enabled": 1, "epg": 101, "symbolrate": 123}
        muxes = [existing.copy()]
        async def grid(endpoint):
            return ([{"uuid": "network", "orbital_pos": "19.2E"}] if endpoint == "mpegts/network/grid" else muxes)
        async def create(endpoint, **values):
            self.assertEqual(endpoint, "mpegts/network/mux_create")
            self.assertEqual(values["uuid"], "network")
            self.assertEqual(values["conf"]["enabled"], 0)
            self.assertEqual(values["conf"]["epg"], 0)
            muxes.append({**values["conf"], "uuid": "new", "network_uuid": "network"})
        backend = AsyncMock()
        backend.grid.side_effect = grid
        backend.request.side_effect = create
        self.assertEqual(await supplement(backend, "network", self.directory), 1)
        self.assertEqual(await supplement(backend, "network", self.directory), 0)
        self.assertEqual(muxes[0], existing)
        backend.request.assert_awaited_once()

    async def test_other_satellite_is_not_modified(self):
        backend = AsyncMock()
        backend.grid.return_value = [{"uuid": "network", "orbital_pos": "28.2E"}]
        self.assertEqual(await supplement(backend, "network", self.directory), 0)
        backend.request.assert_not_awaited()

    async def test_failed_readback_is_reported(self):
        backend = AsyncMock()
        backend.grid.side_effect = [[{"uuid": "network", "orbital_pos": "19.2E"}], [], []]
        with self.assertRaises(BackendError):
            await supplement(backend, "network", self.directory)


class AstraScanTests(test_lifecycle.ScanTests):
    async def test_full_scan_supplements_after_pause_and_before_tuning(self):
        async def repair(*args):
            self.manager.quiet.assert_awaited_once()
            self.assertTrue(list(self.manager.directory.glob("scan-backup-*.json")))
            return 1
        with patch.object(self.manager, "quiet", new=AsyncMock()), \
                patch("satip_controller.astra.supplement", new=AsyncMock(side_effect=repair)) as add:
            await self.manager.scan()
        add.assert_awaited_once_with(self.backend, self.web_config["network_uuid"])
        self.assertEqual(self.manager.status["added_muxes"], 1)

    async def test_enabled_only_scan_does_not_expand_network(self):
        self.backend.mux["enabled"] = 1
        with patch("satip_controller.astra.supplement", new=AsyncMock()) as add:
            await self.manager.scan(enabled_only=True)
        add.assert_not_awaited()
