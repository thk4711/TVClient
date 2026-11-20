import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/source_manifest.py"
SPEC = importlib.util.spec_from_file_location("source_manifest", SCRIPT)
manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest)


class SourceManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / "satip_controller"
        (self.package / "static").mkdir(parents=True)
        (self.package / "__init__.py").write_text("")
        (self.package / "player.py").write_text("# player\n")
        (self.root / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
        for name in manifest.STATIC_FILES:
            (self.package / "static" / name).write_text(name)

    def test_deterministic_identity_without_location_or_timestamp(self):
        first = manifest.source_manifest(self.root)
        self.assertEqual(first, manifest.source_manifest(self.root))
        self.assertEqual(first["version"], "0.1.0")
        self.assertNotIn(str(self.root), json.dumps(first))

    def test_ignores_private_data_and_generated_files(self):
        first = manifest.source_manifest(self.root)
        (self.root / "credentials.json").write_text('secret')
        (self.package / "__pycache__").mkdir()
        (self.package / "__pycache__/player.pyc").write_bytes(b"cache")
        (self.package / "static/private.txt").write_text('secret')
        self.assertEqual(first, manifest.source_manifest(self.root))

    def test_detects_module_and_asset_changes(self):
        first = manifest.source_manifest(self.root)
        for relative in ("satip_controller/player.py", "satip_controller/static/ui.js",
                         "satip_controller/static/qrcode.js",
                         "satip_controller/static/qrcode-LICENSE.txt"):
            with self.subTest(relative=relative):
                path = self.root / relative
                original = path.read_bytes()
                path.write_bytes(original + b"changed")
                changed = manifest.source_manifest(self.root)
                self.assertNotEqual(first["source_sha256"], changed["source_sha256"])
                self.assertEqual(manifest.compare_manifests(changed, first)["changed"], [relative])
                path.write_bytes(original)

    def test_detects_added_module(self):
        first = manifest.source_manifest(self.root)
        (self.package / "extra.py").write_text("# extra")
        second = manifest.source_manifest(self.root)
        self.assertEqual(manifest.compare_manifests(second, first)["only_current"],
                         ["satip_controller/extra.py"])

    def test_qr_encoder_and_notice_are_required_inputs(self):
        files = manifest.source_manifest(self.root)["files"]
        for name in ("qrcode.js", "qrcode-LICENSE.txt"):
            self.assertIn("satip_controller/static/" + name, files)
            path = self.package / "static" / name
            original = path.read_bytes()
            path.unlink()
            with self.assertRaises(ValueError):
                manifest.source_manifest(self.root)
            path.write_bytes(original)

    def test_rejects_missing_asset(self):
        (self.package / "static/ui.js").unlink()
        with self.assertRaises(ValueError):
            manifest.source_manifest(self.root)

    def test_rejects_source_and_directory_symlinks(self):
        player = self.package / "player.py"
        player.unlink()
        player.symlink_to(self.root / "pyproject.toml")
        with self.assertRaises(ValueError):
            manifest.source_manifest(self.root)
        player.unlink()
        static = self.package / "static"
        static.rename(self.root / "assets")
        static.symlink_to(self.root / "assets", target_is_directory=True)
        with self.assertRaises(ValueError):
            manifest.source_manifest(self.root)

    def test_metadata_version_changes_identity(self):
        first = manifest.source_manifest(self.root)
        (self.root / "pyproject.toml").write_text('[project]\nversion = "0.2.0"\n')
        self.assertNotEqual(first["source_sha256"], manifest.source_manifest(self.root)["source_sha256"])


if __name__ == "__main__":
    unittest.main()
