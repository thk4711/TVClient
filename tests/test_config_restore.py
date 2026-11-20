import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from satip_appliance import config_restore
from satip_controller.state import atomic_json, read_json


class ConfigRestoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.boot = self.root / "boot"
        self.boot.mkdir()
        self.template = self.root / "usr/share/satip/satip-config.txt"
        self.template.parent.mkdir(parents=True)
        self.template.write_bytes(b"# public template\nhostname=satip-dev\n")
        self.target = self.boot / "satip-config.txt"
        self.target.write_text("setup_password=private\n")
        self.store = self.root / "data/store"
        (self.store / "web").mkdir(parents=True)
        atomic_json(self.store / "installation.json", {"state": "unconfigured"})
        self.worker = config_restore.ConfigRestore(self.store, self.root)

    def mounts(self):
        original = Path.read_text
        def read(path, *args, **kwargs):
            if str(path) == "/proc/mounts":
                return f"/dev/mmcblk0p1 {self.boot} vfat rw 0 0\n"
            return original(path, *args, **kwargs)
        return patch.object(Path, "read_text", read)

    def trusted(self):
        original = os.fstat
        def info(fd):
            node = original(fd)
            return SimpleNamespace(st_mode=node.st_mode, st_uid=0)
        return patch.object(config_restore.os, "fstat", side_effect=info)

    def test_exact_template_atomic_replace_and_no_other_changes(self):
        saved = self.store / "settings.json"
        saved.write_text("private saved settings")
        with self.mounts(), self.trusted(), patch.object(os.path, "ismount", return_value=True):
            config_restore.restore(self.root)
            self.assertEqual(self.target.read_bytes(), self.template.read_bytes())
            config_restore.restore(self.root)
        self.assertEqual(saved.read_text(), "private saved settings")
        self.assertEqual(list(self.boot.iterdir()), [self.target])

    def test_missing_mount_or_template_leaves_original_intact(self):
        with patch.object(os.path, "ismount", return_value=False), self.assertRaises(ValueError):
            config_restore.restore(self.root)
        self.template.unlink()
        with self.mounts(), patch.object(os.path, "ismount", return_value=True), self.assertRaises(OSError):
            config_restore.restore(self.root)
        self.assertEqual(self.target.read_text(), "setup_password=private\n")

    def test_untrusted_or_symlink_template_refused(self):
        self.template.chmod(0o666)
        with self.mounts(), self.trusted(), patch.object(os.path, "ismount", return_value=True), self.assertRaises(ValueError):
            config_restore.restore(self.root)
        self.template.unlink()
        self.template.symlink_to(self.target)
        with self.mounts(), patch.object(os.path, "ismount", return_value=True), self.assertRaises(OSError):
            config_restore.restore(self.root)

    def test_failed_replace_preserves_original_and_cleans_temporary(self):
        with self.mounts(), self.trusted(), patch.object(os.path, "ismount", return_value=True), \
                patch.object(os, "replace", side_effect=OSError("private details")), self.assertRaises(OSError):
            config_restore.restore(self.root)
        self.assertEqual(self.target.read_text(), "setup_password=private\n")
        self.assertEqual(list(self.boot.iterdir()), [self.target])

    def test_mailbox_validates_requests_reports_errors_and_deduplicates(self):
        request = self.store / "web/config-restore-request.json"
        result = self.store / "web/config-restore-result.json"
        with patch.object(config_restore, "restore") as restore, patch.object(os, "chown"):
            for node in ({"request_id": "a" * 32, "path": "/etc/shadow"}, {"request_id": 1}, {}):
                atomic_json(request, node)
                self.worker.refresh()
            restore.assert_not_called()
            atomic_json(request, {"request_id": "a" * 32})
            self.worker.refresh()
            self.worker.refresh()
            config_restore.ConfigRestore(self.store, self.root).refresh()
            restore.assert_called_once_with(self.root)
            self.assertEqual(read_json(result)["state"], "restored")
            atomic_json(request, {"request_id": "b" * 32})
            restore.side_effect = OSError("private details")
            self.worker.refresh()
            self.assertEqual(read_json(result)["state"], "error")
            self.assertNotIn("private details", result.read_text())
            atomic_json(request, {"request_id": "c" * 32})
            atomic_json(self.store / "installation.json", {"state": "configured"})
            self.worker.refresh()
            self.assertEqual(restore.call_count, 2)

    def test_symlink_and_oversized_requests_are_ignored(self):
        request = self.store / "web/config-restore-request.json"
        request.symlink_to(self.target)
        with patch.object(config_restore, "restore") as restore:
            self.worker.refresh()
            request.unlink()
            request.write_text("x" * 1000)
            self.worker.refresh()
            restore.assert_not_called()
