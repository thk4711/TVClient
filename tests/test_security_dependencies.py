"""Security pins, target trust stores and path decisions; no live receiver access."""
import base64
import hashlib
import json
from pathlib import Path
import re
import runpy
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.datastructures import URL

from satip_appliance.device_api import install

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "buildroot/external/board/satip"
VERSIONS = runpy.run_path(str(BOARD / "validate-app-build.py"))["SECURITY_VERSIONS"]
TRUST = runpy.run_path(str(BOARD / "install-ca-trust.py"))
BUILD = runpy.run_path(str(ROOT / "buildroot/validate-security.py"))


class SecurityDependencyTests(unittest.TestCase):
    def test_manifests_and_override_hashes_match_target_versions(self):
        requirements = (ROOT / "tests/buildroot-requirements.txt").read_text()
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
        overrides = (ROOT / "buildroot/external/security-overrides.mk").read_text()
        for name, version in VERSIONS.items():
            self.assertIn(f"{name}=={version}", requirements.splitlines())
            self.assertTrue(any(re.match(re.escape(name) + r"(?:==|>=)" + re.escape(version) + r"(?:;|$)", item)
                                for item in project))
            self.assertIn(f"override PYTHON_{name.upper()}_VERSION = {version}", overrides)
            hashes = ROOT / f"buildroot/external/patches/python-{name}/{version}/python-{name}.hash"
            self.assertRegex(hashes.read_text(), r"sha256  [0-9a-f]{64}  " + re.escape(f"{name}-{version}.tar.gz"))
        self.assertIn('BR2_PACKAGE_OVERRIDE_FILE="$(BR2_EXTERNAL_SATIP_PATH)/security-overrides.mk"',
                      (ROOT / "buildroot/external/configs/satip_rpi4_64_defconfig").read_text())

    def test_actual_recipe_validation_rejects_ignored_override(self):
        def result(command, **kwargs):
            names = command[-1].removeprefix("VARS=").split()
            values = {}
            for key in names:
                package = key.removeprefix("HOST_").removeprefix("PYTHON_")
                name, _, field = package.partition("_")
                name = name.lower()
                version = VERSIONS[name]
                values[key] = (f"{name}-{version}.tar.gz" if field == "SOURCE" else
                    str(ROOT / f"buildroot/external/patches/python-{name}/{version}/python-{name}.hash")
                    if field == "HASH_FILES" else version)
            if stale:
                values["PYTHON_STARLETTE_VERSION"] = "1.0.0"
            return subprocess.CompletedProcess(command, 0, "\n".join(f"{k}={v}" for k, v in values.items()), "")
        with patch("subprocess.run", side_effect=result):
            stale = False
            self.assertEqual(BUILD["validate"]("checkout", "output", ROOT), VERSIONS)
            stale = True
            with self.assertRaisesRegex(ValueError, "PYTHON_STARLETTE_VERSION"):
                BUILD["validate"]("checkout", "output", ROOT)


class TrustStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.target = Path(temporary.name).resolve()
        self.bundle = self.target / "etc/ssl/certs/ca-certificates.crt"
        self.bundle.parent.mkdir(parents=True)
        self.sources = self.target / "usr/share/ca-certificates/mozilla"
        self.sources.mkdir(parents=True)
        self.bad = b"-----BEGIN CERTIFICATE-----\n" + base64.b64encode(b"revoked fixture") + b"\n-----END CERTIFICATE-----\n"
        self.good = b"-----BEGIN CERTIFICATE-----\n" + base64.b64encode(b"trusted fixture") + b"\n-----END CERTIFICATE-----\n"
        self.bundle.write_bytes(self.bad + self.good)
        revoked = patch.dict(TRUST["revoked"].__globals__, REVOKED=hashlib.sha256(b"revoked fixture").hexdigest())
        revoked.start()
        self.addCleanup(revoked.stop)

    def test_distrust_removes_bundle_source_and_relative_absolute_hash_aliases(self):
        source = self.sources / "compromised.crt"
        source.write_bytes(self.bad)
        (self.sources / "trusted.crt").write_bytes(self.good)
        pem = self.bundle.parent / "compromised.pem"
        pem.symlink_to("../../../usr/share/ca-certificates/mozilla/compromised.crt")
        alias = self.bundle.parent / "00000000.0"
        alias.symlink_to("compromised.pem")
        absolute = self.bundle.parent / "11111111.0"
        absolute.symlink_to("/usr/share/ca-certificates/mozilla/compromised.crt")
        (self.bundle.parent / "trusted.pem").symlink_to("../../../usr/share/ca-certificates/mozilla/trusted.crt")
        TRUST["install"](self.target)
        self.assertEqual(self.bundle.read_bytes(), self.good)
        for path in (source, pem, alias, absolute):
            self.assertFalse(path.exists() or path.is_symlink())
        self.assertTrue((self.bundle.parent / "trusted.pem").is_file())
        TRUST["install"](self.target)
        self.assertEqual(self.bundle.read_bytes(), self.good)

    def test_linked_bundle_and_source_tree_refused(self):
        outside = self.target / "outside"
        outside.write_bytes(self.good)
        self.bundle.unlink()
        self.bundle.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "Linked CA"):
            TRUST["install"](self.target)
        self.assertEqual(outside.read_bytes(), self.good)
        self.bundle.unlink()
        self.bundle.write_bytes(self.good)
        (self.sources / "linked.crt").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "Linked CA"):
            TRUST["install"](self.target)


class DevicePathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.login = self.root / "login.json"
        self.login.write_text(json.dumps({"username": "fixture", "password": "fixture-password"}))
        self.login.chmod(0o600)
        self.calls = []
        async def helper(action, args=None):
            self.calls.append((action, args))
            return {"state": "ready", "kind": "configuration"}
        self.app = FastAPI()
        install(self.app, self.login, helper, backup_root=self.root, require_auth=lambda: False)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_maintenance_uses_routed_path_not_reconstructed_url(self):
        directory = self.root / "satip-backup"
        directory.mkdir()
        (directory / "maintenance.json").write_text("{}")
        @self.app.post("/other")
        async def other():
            return {"unexpected": True}
        with patch.object(Request, "url", new=property(lambda request: URL("http://testserver/api/device"))):
            self.assertEqual(self.client.post("/other").status_code, 409)
            self.assertEqual(self.client.post("/api/device", json={"action": "storage_status"}).status_code, 200)

    def test_upload_kind_uses_routed_path_not_reconstructed_url(self):
        (self.root / "satip-web").mkdir()
        token = "b" * 32
        headers = {"X-Satip-Action": "1", "Content-Type": "application/octet-stream"}
        for kind, poisoned, expected in (("channels", "backup", "backup_channels_validate"),
                                         ("backup", "channels", "backup_validate")):
            with patch.object(Request, "url", new=property(
                    lambda request: URL("http://testserver/api/device/" + poisoned + "/"))):
                response = self.client.post(f"/api/device/{kind}/upload/{token}",
                                            content=b"archive fixture", headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.calls[-1], (expected, {"id": token}))
        self.assertFalse(list((self.root / "satip-web").glob("backup-upload-*")))

    def test_download_kind_uses_routed_path_not_reconstructed_url(self):
        token = "a" * 32
        directory = self.root / "satip-backup"
        directory.mkdir()
        (directory / f"download-{token}.tar").write_bytes(b"configuration fixture")
        with patch.object(Request, "url", new=property(lambda request: URL("http://testserver/api/device/channels/"))):
            response = self.client.get("/api/device/backup/download/" + token)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"configuration fixture")
        with patch.object(Request, "url", new=property(lambda request: URL("http://testserver/api/device/backup/"))):
            self.assertEqual(self.client.get("/api/device/channels/download/" + token).status_code, 409)
