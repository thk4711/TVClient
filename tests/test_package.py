"""Archive validation tests without installing dependencies or contacting a device."""
import io
from pathlib import Path
import runpy
import tarfile
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
CHECK = runpy.run_path(str(ROOT / "scripts/check_package.py"))


class PackageArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.payload = {"satip_controller/__init__.py": b"", "satip_appliance/__init__.py": b"",
                        "satip_controller/static/request.js": b"// fixture"}
        for name in CHECK["LICENCES"][1:]:
            self.payload[name] = (ROOT / name).read_bytes()
        self.expected = {name: CHECK["digest"](data) for name, data in self.payload.items()}

    def wheel(self, changes=None):
        prefix = "satip_controller-0.1.0.dist-info/"
        metadata = "Name: satip-controller\nVersion: 0.1.0\nLicense-Expression: MIT AND Apache-2.0\n"
        metadata += "".join("License-File: " + name + "\n" for name in CHECK["LICENCES"])
        files = {**self.payload, prefix + "METADATA": metadata.encode()}
        for name in CHECK["LICENCES"]:
            files[prefix + "licenses/" + name] = (ROOT / name).read_bytes()
        for name, data in (changes or {}).items():
            if data is None:
                files.pop(name)
            else:
                files[name] = data
        path = self.root / "fixture.whl"
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        return path

    def sdist(self, changes=None, link=False):
        files = {**self.payload, "pyproject.toml": b"[build-system]", "MANIFEST.in": b"fixture",
                 "README.md": b"fixture", "LICENSE": b"MIT License", "PKG-INFO": b"fixture"}
        files.update(changes or {})
        path = self.root / "fixture.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, data in files.items():
                member = tarfile.TarInfo("satip_controller-0.1.0/" + name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            if link:
                member = tarfile.TarInfo("satip_controller-0.1.0/linked")
                member.type = tarfile.SYMTYPE
                member.linkname = "/outside"
                archive.addfile(member)
        return path

    def test_exact_wheel_and_sdist_payloads(self):
        self.assertEqual(CHECK["inspect_wheel"](self.wheel(), self.expected), self.expected)
        name, payload = CHECK["inspect_sdist"](self.sdist(), self.expected)
        self.assertEqual(name, "satip_controller-0.1.0")
        self.assertEqual(payload, self.expected)

    def test_missing_and_stale_wheel_payloads_are_rejected(self):
        for change in ({"satip_controller/static/request.js": None},
                       {"satip_controller/static/request.js": b"stale"},
                       {"satip_controller/removed.py": b"old module"}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "inventory"):
                CHECK["inspect_wheel"](self.wheel(change), self.expected)

    def test_incomplete_licence_and_unexpected_wheel_files_are_rejected(self):
        for name in ("../outside", "satip_controller/static/private.pem", "tests/test_core.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                CHECK["inspect_wheel"](self.wheel({name: b"fixture"}), self.expected)
        notice = "satip_controller/static/hls-LICENSE.txt"
        with self.assertRaisesRegex(ValueError, "Incomplete Apache"):
            CHECK["inspect_wheel"](self.wheel({notice: b"Apache URL only",
                "satip_controller-0.1.0.dist-info/licenses/" + notice: b"Apache URL only"}))

    def test_sdist_rejects_links_traversal_private_files_and_incomplete_tests(self):
        for change in ({"../../outside": b"fixture"}, {".baseline-private/evidence": b"fixture"},
                       {"tests/test_core.py": b"from tests.helpers import Fixture"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                CHECK["inspect_sdist"](self.sdist(change), self.expected)
        with self.assertRaisesRegex(ValueError, "Linked"):
            CHECK["inspect_sdist"](self.sdist(link=True), self.expected)

    def test_sdist_inventory_rejects_missing_current_module(self):
        expected = {**self.expected, "satip_appliance/new.py": "new source digest"}
        with self.assertRaisesRegex(ValueError, "inventory"):
            CHECK["inspect_sdist"](self.sdist(), expected)


if __name__ == "__main__":
    unittest.main()
