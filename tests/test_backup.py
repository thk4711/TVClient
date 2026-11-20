"""Isolated backup/API tests. No real receiver access or host configuration writes."""
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from satip_appliance import backup, persistent, provision
from satip_appliance.device_api import install
from satip_appliance.device_helper import Device
from satip_controller.http import RequestLimits, check_action
from satip_controller.state import atomic_json, read_json

PARSER = Path("buildroot/external/board/satip/rootfs-overlay/usr/sbin/satip-dev-provision")


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / "data/store"
        for name in ("usr/sbin", "usr/share/zoneinfo/Etc", "etc", "boot", "run/satip-web", "data"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        shutil.copy(PARSER, self.root / "usr/sbin/satip-dev-provision")
        (self.root / "usr/share/zoneinfo/Etc/UTC").write_bytes(b"TZif-test")
        for name, text in {"etc/shadow": "root:*:0:0:99999:7:::\n", "etc/hostname": "test\n",
                           "etc/satip-ssh-enabled": "no\n", "boot/satip-config.txt": ""}.items():
            (self.root / name).write_text(text)
        with patch("os.chown"):
            persistent.publish(self.root / "data")
            provision.seed(self.root, self.root / "data", {})
        self.device = Device(self.root, run=lambda *a, **k: None)
        self.device.command = lambda *a, **k: None
        factory = read_json(self.store / "identity/factory.json")
        self.network, self.channel = "a" * 32, "b" * 32
        config = provision.web_config(self.network, factory["ids"]["playback_acl"], factory["settings"], ["testserver"])
        atomic_json(self.store / "config/web/config.json", config)
        atomic_json(self.store / "installation.json", {"state": "configured", "origin": "fresh"})
        self.device.parser["atomic_write"](self.root, "data/store/identity/development.txt", "hostname=test\nssh_enabled=no\n")
        channels = {"version": 1, "channels": [{"uuid": self.channel, "name": "Test TV", "service_uuid": "c" * 32,
            "mux_uuid": "d" * 32, "video": "h264", "audio_pid": 101}], "favourites": [self.channel]}
        atomic_json(self.store / "management/library.json", channels)
        for name in ("input/dvb/networks/" + self.network + "/config", "channel/config/" + self.channel):
            target = self.store / "tvheadend" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(target, {})
        self.archive = self.root / "backup.tar"
        self.files = backup.selected(self.store)
        self.files.update(backup.effective_files(self.device))
        backup.write_archive(self.archive, self.files)

    def unpack(self, name="stage"):
        stage = self.root / name
        manifest = backup.unpack(self.archive, stage)
        return stage, manifest

    def test_round_trip_and_explicit_exclusions(self):
        for name in ("tvheadend/epgdb.v3", "player/status.json", "web/scan-backup-1.json", "operations/private.tar"):
            path = self.store / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"excluded")
        files = backup.selected(self.store)
        self.assertFalse(any(name in files for name in ("tvheadend/epgdb.v3", "player/status.json", "operations/private.tar")))
        stage, manifest = self.unpack()
        backup.validate_stage(stage, self.device)
        self.assertEqual(manifest["format"], backup.FORMAT)
        self.assertEqual((stage / "identity/authorized_keys").read_bytes(), b"")
        self.assertNotIn("root_password=", (stage / "identity/development.txt").read_text())

    def test_packed_backend_records_and_decompression_bounds(self):
        import zlib
        # Original synthetic binary2 fields: a map with a string and a bool.
        fields = bytes([3, 4, 2]) + b"nameTV" + bytes([7, 7, 1]) + b"enabled\x01"
        packed = b"\xff\xffGZIP01" + len(fields).to_bytes(4, "big") + zlib.compress(fields)
        self.assertEqual(backup.backend_record(packed), {"name": "TV", "enabled": True})
        for bad in (packed[:-1], packed + b"trailing", packed[:8] + (backup.MAX_FILE + 1).to_bytes(4, "big") + packed[12:]):
            with self.assertRaises((ValueError, zlib.error)):
                backup.backend_record(bad)
        malformed = bytes([3, 4, 127]) + b"nameTV"
        bad = b"\xff\xffGZIP01" + len(malformed).to_bytes(4, "big") + zlib.compress(malformed)
        with self.assertRaises(ValueError):
            backup.backend_record(bad)

    def test_root_recovery_edits_and_web_hash_persistence(self):
        target = self.store / "identity/root-access.json"
        hashed = "$6$salt$" + "A" * 86
        atomic_json(target, {"version": 1, "hash": hashed})
        backup.apply_root_access(self.root)
        self.assertIn(hashed, (self.root / "etc/shadow").read_text())
        newer = "$6$salt$" + "B" * 86
        (self.root / "etc/shadow").write_text("root:" + newer + ":0:0:99999:7:::\n")
        backup.apply_root_access(self.root, {"recovery_password_hash": newer})
        self.assertEqual(read_json(target)["hash"], newer)

    def test_capture_effective_root_hash_and_keys(self):
        hashed = "$6$salt$" + "A" * 86
        (self.root / "etc/shadow").write_text("root:" + hashed + ":0:0:99999:7:::\n")
        keys = self.root / "root/.ssh/authorized_keys"
        keys.parent.mkdir(parents=True)
        keys.write_text("ssh-ed25519 AAAATEST fake-key\n")
        result = backup.effective_files(self.device)
        self.assertEqual(json.loads(result["identity/root-access.json"])["hash"], hashed)
        self.assertIn(hashed.encode(), result["identity/development.txt"])
        self.assertEqual(result["identity/authorized_keys"], keys.read_bytes())

    def test_reject_paths_links_duplicates_compression_and_special_files(self):
        for name, kind in (("../../etc/shadow", tarfile.REGTYPE), ("/etc/shadow", tarfile.REGTYPE),
                           ("identity/authorized_keys", tarfile.SYMTYPE), ("tvheadend/config", tarfile.FIFOTYPE)):
            with self.subTest(name=name, kind=kind):
                with tarfile.open(self.archive, "w") as out:
                    entry = tarfile.TarInfo(name)
                    entry.type = kind
                    out.addfile(entry)
                with self.assertRaises(ValueError):
                    self.unpack()
        with tarfile.open(self.archive, "w:gz") as out:
            out.add(self.store / "schema.json", arcname="schema.json")
        with self.assertRaises(tarfile.ReadError):
            self.unpack()
        with tarfile.open(self.archive, "w") as out:
            for _ in range(2):
                entry = tarfile.TarInfo("schema.json")
                out.addfile(entry)
        with self.assertRaises(ValueError):
            self.unpack()

    def test_checksum_and_backend_version_rejected_before_stage_creation(self):
        content = bytearray(self.archive.read_bytes())
        position = content.find(b"test")
        self.assertGreater(position, 0)
        content[position] = ord("X")
        self.archive.write_bytes(content)
        with self.assertRaises(ValueError):
            self.unpack()
        self.assertFalse((self.root / "stage").exists())
        with patch.object(backup, "REVISION", "different"):
            backup.write_archive(self.archive, self.files)
        with self.assertRaises(ValueError):
            self.unpack()

    def test_archive_size_and_member_bounds(self):
        with patch.object(backup, "MAX_ARCHIVE", 100):
            with self.assertRaises(ValueError):
                self.unpack()
        with tarfile.open(self.archive, "w") as out:
            entry = tarfile.TarInfo("tvheadend/config")
            entry.size = backup.MAX_FILE + 1
            # Only header is needed: bounds must reject before reading payload.
            out.fileobj.write(entry.tobuf())
        with self.assertRaises(ValueError):
            self.unpack()

    def test_invalid_paths_credentials_and_hooks(self):
        for case in ("paths", "credentials", "hook", "acl"):
            with self.subTest(case=case):
                stage, _ = self.unpack(case)
                if case == "paths":
                    node = read_json(stage / "config/web/config.json")
                    node["private_dir"] = "/etc"
                    atomic_json(stage / "config/web/config.json", node)
                elif case == "credentials":
                    atomic_json(stage / "config/web/login.json", {"username": "satip", "password": "different"})
                elif case == "hook":
                    atomic_json(stage / "tvheadend/config", {"spawn_command": "/bin/evil"})
                else:
                    config = read_json(stage / "config/web/config.json")
                    path = stage / "tvheadend/accesscontrol" / config["playback_acl_uuid"]
                    node = read_json(path)
                    node["admin"] = True
                    atomic_json(path, node)
                with self.assertRaises(ValueError):
                    backup.validate_stage(stage, self.device)

    def test_snapshot_stops_and_resumes_writers_and_expires(self):
        actions = []
        service = self.device.backups
        service.services = actions.append
        service.prepare()
        service.handle("backup_create", {"id": "e" * 32})
        service.not_before = 0
        service.tick()
        self.assertEqual(actions, ["stop", "start"])
        self.assertEqual(read_json(service.result)["state"], "ready")
        self.assertFalse(service.gate.exists())
        service.expires = 1
        service.tick()
        self.assertFalse(service.result.exists())
        self.assertFalse(list(service.directory.glob("download-*.tar")))

    def test_restore_activation_and_boot_commit_recovery(self):
        service = self.device.backups
        service.prepare()
        backup.unpack(self.archive, service.directory / "validated")
        (self.store / "player/state.json").write_text('{"volume":99}')
        with patch("os.chown"):
            service.activate()
        journal, stage, before = backup.journal_paths(self.root / "data")
        self.assertEqual(read_json(journal)["phase"], "activated")
        self.assertTrue(before.is_dir())
        self.assertEqual((self.store / "installation.json").stat().st_mode & 0o777, 0o644)
        self.assertEqual(before.stat().st_mode & 0o777, 0o700)
        self.assertFalse(stage.exists())
        backup.recover_restore(self.root / "data")
        self.assertFalse(journal.exists())
        self.assertFalse((self.store / "player/state.json").exists())
        backup.apply_root_access(self.root)
        self.assertTrue((self.root / "etc/shadow").read_text().startswith("root:*:"))

    def test_helper_interruption_recovery_uses_journal_before_restart(self):
        service = self.device.backups
        actions = []
        service.services = actions.append
        service.prepare()
        journal, stage, before = backup.journal_paths(self.root / "data")
        os.rename(self.store, before)
        stage.mkdir()
        atomic_json(journal, {"version": 1, "phase": "old-moved"})
        atomic_json(service.gate, {"id": "e" * 32})
        atomic_json(service.result, {"id": "e" * 32, "state": "working"})
        service.prepare()
        self.assertEqual(actions, ["stop", "start"])
        self.assertTrue(self.store.exists())
        self.assertFalse(service.gate.exists())
        self.assertFalse(stage.exists())
        self.assertFalse(journal.exists())
        backup.unpack(self.archive, service.directory / "validated")
        with patch("os.chown"):
            service.activate()
        atomic_json(service.gate, {"id": "f" * 32})
        atomic_json(service.result, {"id": "f" * 32, "state": "working"})
        commands = []
        self.device.command = lambda argv, **kw: commands.append(argv)
        service.prepare()
        self.assertEqual(read_json(service.result)["state"], "rebooting")
        self.assertEqual(commands, [["/sbin/reboot"]])
        self.assertEqual(actions, ["stop", "start", "stop"])
        self.assertFalse(journal.exists())

    def test_failure_after_each_rename_rolls_back(self):
        for failure in (1, 2):
            with self.subTest(failure=failure):
                service = self.device.backups
                service.prepare()
                validated = service.directory / "validated"
                if not validated.exists():
                    backup.unpack(self.archive, validated)
                (self.store / "player/state.json").write_text('{"volume":99}')
                original = os.rename
                calls = []
                def rename(source, destination):
                    calls.append(source)
                    original(source, destination)
                    if len(calls) == failure:
                        raise OSError("injected interruption")
                with patch("os.chown"), patch("os.rename", rename):
                    with self.assertRaises(OSError):
                        service.activate()
                self.assertEqual(read_json(self.store / "player/state.json"), {"volume": 99})
                _, stage, before = backup.journal_paths(self.root / "data")
                if stage.exists():
                    shutil.rmtree(stage)
                self.assertFalse(before.exists())

    def test_links_in_source_and_pending_wifi_refused(self):
        (self.store / "identity/linked").symlink_to(self.root / "etc/shadow")
        with self.assertRaises(ValueError):
            backup.selected(self.store)
        (self.store / "identity/linked").unlink()
        atomic_json(self.store / "network/wifi-trial.json", {"id": "pending"})
        with self.assertRaises(ValueError):
            self.device.backups.handle("backup_create", {"id": "e" * 32})

    def test_authenticated_binary_upload_and_download(self):
        async def authenticate(request: Request):
            check_action(request)
        app = FastAPI(dependencies=[Depends(authenticate)])
        app.add_middleware(RequestLimits, backup_upload=True)
        calls = []
        async def helper(action, args=None):
            calls.append((action, args))
            if action == "backup_validate":
                content = (self.root / "run/satip-web" / ("backup-upload-" + args["id"])).read_bytes()
                self.assertEqual(content, self.archive.read_bytes())
            return {"state": "ready"}
        install(app, self.store / "config/web/login.json", helper, backup_root=self.root / "run")
        client = TestClient(app)
        token = "e" * 32
        path = "/api/device/backup/upload/" + token
        content = self.archive.read_bytes()
        login = read_json(self.store / "config/web/login.json")
        auth = (login["username"], login["password"])
        headers = {"X-Satip-Action": "1", "Content-Type": "application/octet-stream"}
        self.assertEqual(client.post(path, content=content, headers=headers).status_code, 401)
        self.assertEqual(client.post(path, content=content, auth=auth).status_code, 403)
        self.assertEqual(client.post(path, content=content, auth=auth, headers=headers).status_code, 200)
        self.assertFalse(list((self.root / "run/satip-web").glob("backup-upload-*")))
        self.assertEqual(client.post(path, content=content, auth=auth, headers={**headers, "Content-Length": str(backup.MAX_ARCHIVE + 1)}).status_code, 413)
        with patch("satip_appliance.device_api.MAX_ARCHIVE", len(content) - 1):
            for extra in ({}, {"Content-Length": "8"}):
                response = client.post(path, content=iter((content[:1000], content[1000:])),
                    auth=auth, headers={**headers, **extra})
                self.assertEqual(response.status_code, 413)
                self.assertFalse(list((self.root / "run/satip-web").glob("backup-upload-*")))
        payload = {"action": "backup_restore", "args": {"id": token}}
        self.assertEqual(client.post("/api/device", json=payload, auth=auth, headers={"X-Satip-Action": "1"}).status_code, 403)
        payload["current_password"] = auth[1]
        self.assertEqual(client.post("/api/device", json=payload, auth=auth, headers={"X-Satip-Action": "1"}).status_code, 200)
        service = self.device.backups
        service.prepare()
        target = service.directory / ("download-" + token + ".tar")
        shutil.copy(self.archive, target)
        response = client.get("/api/device/backup/download/" + token, auth=auth)
        self.assertEqual(response.content, content)
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual(client.get("/api/device/backup/download/" + token).status_code, 401)


if __name__ == "__main__":
    unittest.main()
