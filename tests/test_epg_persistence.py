import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from satip_appliance.epg_persistence import EPGPersistence
from satip_controller.state import atomic_json, read_json


class EPGPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.persistence = EPGPersistence(self.directory)
        self.config = self.directory / "epggrab/config"

    def configure(self):
        self.persistence.configure(os.getuid(), os.getgid())

    def test_fresh_backend_retains_default_passive_grabbers(self):
        self.configure()
        config = read_json(self.config)
        self.assertEqual(config["epgdb_periodicsave"], 1)
        self.assertTrue(config["modules"]["eit"]["enabled"])
        self.assertTrue(config["modules"]["psip"]["enabled"])
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_existing_settings_preserved_and_update_idempotent(self):
        config = {"epgdb_periodicsave": 0, "modules": {"eit": {"enabled": False}},
                  "ota_cron": "", "channel_rename": True}
        atomic_json(self.config, config)
        self.configure()
        self.assertEqual(read_json(self.config), {**config, "epgdb_periodicsave": 1})
        with patch("satip_appliance.epg_persistence.atomic_json") as write:
            self.configure()
        write.assert_not_called()

    def test_invalid_configuration_not_overwritten(self):
        self.config.parent.mkdir()
        self.config.write_text("[]")
        with self.assertRaises(ValueError):
            self.configure()
        self.assertEqual(self.config.read_text(), "[]")

    def test_linked_config_refused(self):
        self.config.parent.mkdir()
        target = self.directory / "other"
        target.write_text("{}")
        self.config.symlink_to(target)
        with self.assertRaises(OSError):
            self.configure()
        self.assertEqual(target.read_text(), "{}")

    def test_flush_only_completed_changed_database_and_directory(self):
        with patch("satip_appliance.epg_persistence.os.fsync") as fsync:
            self.assertFalse(self.persistence.flush())
            (self.directory / "epgdb.v3.tmp").write_bytes(b"incomplete")
            self.assertFalse(self.persistence.flush())
            fsync.assert_not_called()
            path = self.directory / "epgdb.v3"
            path.write_bytes(b"saved")
            self.assertTrue(self.persistence.flush())
            self.assertEqual(fsync.call_count, 2)
            self.assertFalse(self.persistence.flush())
            self.assertEqual(fsync.call_count, 2)
            replacement = self.directory / "epgdb.v3.tmp"
            replacement.replace(path)
            self.assertTrue(self.persistence.flush())
            self.assertEqual(fsync.call_count, 4)

    def test_failed_flush_is_retried(self):
        (self.directory / "epgdb.v3").write_bytes(b"saved")
        with patch("satip_appliance.epg_persistence.os.fsync", side_effect=OSError("I/O error")):
            with self.assertRaises(OSError):
                self.persistence.flush()
        self.assertIsNone(self.persistence.synced)
        self.assertTrue(self.persistence.flush())

    def test_linked_database_refused(self):
        (self.directory / "epgdb.v3").symlink_to("epgdb.v3.tmp")
        (self.directory / "epgdb.v3.tmp").write_bytes(b"incomplete")
        with self.assertRaises(OSError):
            self.persistence.flush()
