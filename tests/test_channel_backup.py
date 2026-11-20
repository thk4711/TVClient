"""Channel-only archive, activation and authenticated API regression tests."""
import io
import json
from pathlib import Path
import shutil
import tarfile
import unittest
from unittest.mock import patch

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

import test_backup
from satip_appliance import backup, channel_backup
from satip_appliance.device_api import install
from satip_controller.http import RequestLimits, check_action
from satip_controller.state import atomic_json, read_json


class ChannelBackupTests(unittest.TestCase):
    def setUp(self):
        test_backup.BackupTests.setUp(self)
        self.second = "f" * 32
        self.extra_service = "1" * 32
        data = read_json(self.store / "management/library.json")
        data["channels"].append({**data["channels"][0], "uuid": self.second,
                                 "name": "Second TV", "service_uuid": "2" * 32})
        data["favourites"] = [self.second, self.channel]
        atomic_json(self.store / "management/library.json", data)
        candidates = [{k: v for k, v in item.items() if k != "uuid"} for item in data["channels"]]
        candidates.append({**candidates[0], "name": "Available only", "service_uuid": self.extra_service, "encrypted": False})
        atomic_json(self.store / "web/candidates.json", {"channels": candidates})
        for index, item in enumerate(data["channels"]):
            atomic_json(self.store / "tvheadend/channel/config" / item["uuid"],
                {"name": item["name"], "enabled": True, "services": [item["service_uuid"]],
                 "number": (index + 4) * 1000000, "epgauto": True})
        target = self.store / "tvheadend/input/dvb/networks" / self.network / "muxes" / ("d" * 32)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(target, {"config": {"frequency": 10000000, "services": {
            item["service_uuid"]: {"enabled": True, "sid": index + 1} for index, item in enumerate(candidates)}}})
        self.service = self.device.backups
        self.actions = []
        self.service.services = self.actions.append
        self.service.prepare()
        self.files = channel_backup.selected(self.device)
        backup.write_archive(self.archive, self.files, channel_backup.FORMAT)
        self.token = "e" * 32

    def unpack(self, name="stage"):
        stage = self.root / name
        backup.unpack(self.archive, stage, channel_backup.FORMAT, channel_backup.allowed, channel_backup.REQUIRED)
        return stage

    def validate_upload(self):
        upload = self.root / "run/satip-web" / ("backup-upload-" + self.token)
        shutil.copy(self.archive, upload)
        return self.service.handle("backup_channels_validate", {"id": self.token})

    def test_complete_catalogue_and_order_without_secrets(self):
        stage = self.unpack()
        summary = channel_backup.validate(stage, self.device)
        self.assertEqual(summary["available"], 3)
        self.assertEqual(summary["favourites"], 2)
        self.assertEqual(read_json(stage / "management/library.json")["favourites"], [self.second, self.channel])
        self.assertTrue(all(channel_backup.allowed(name) for name in self.files))
        self.assertFalse(any(word in name for name in self.files for word in ("password", "identity", "network/wpa", "config/web")))
        self.assertNotIn("tvheadend/input/dvb/networks/" + self.network + "/config", self.files)

    def test_snapshot_stops_resumes_without_reboot(self):
        self.service.handle("backup_channels_create", {"id": self.token})
        self.service.not_before = 0
        self.service.tick()
        self.assertEqual(self.actions, ["stop", "start"])
        self.assertEqual(read_json(self.service.result), {"id": self.token, "state": "ready", "kind": "channels"})
        self.assertFalse(self.service.gate.exists())

    def test_restore_replaces_channels_preserves_other_content(self):
        self.validate_upload()
        data = read_json(self.store / "management/library.json")
        data["favourites"] = [self.channel]
        atomic_json(self.store / "management/library.json", data)
        atomic_json(self.store / "web/candidates.json", {"channels": []})
        atomic_json(self.store / "management/settings.json", {"version": 1, "osd_timeout_ms": 9000, "osd_language": "de"})
        atomic_json(self.store / "player/state.json", {"volume": 43})
        before = {p.relative_to(self.store).as_posix(): p.read_bytes() for p in self.store.rglob("*") if p.is_file()}
        self.service.handle("backup_channels_restore", {"id": self.token})
        self.service.not_before = 0
        with patch("os.chown"):
            self.service.tick()
        self.assertEqual(self.actions, ["stop", "start"])
        self.assertEqual(read_json(self.service.result)["state"], "restored")
        self.assertEqual(read_json(self.store / "management/library.json")["favourites"], [self.second, self.channel])
        self.assertEqual(len(read_json(self.store / "web/candidates.json")["channels"]), 3)
        acl_path = "tvheadend/accesscontrol/" + read_json(self.store / "config/web/config.json")["playback_acl_uuid"]
        old_acl = json.loads(before[acl_path])
        new_acl = read_json(self.store / acl_path)
        self.assertEqual(new_acl.pop("channel_max"), 5000000)
        old_acl.pop("channel_max")
        self.assertEqual(new_acl, old_acl)
        for name, content in before.items():
            if not channel_backup.allowed(name) and name != acl_path:
                self.assertEqual((self.store / name).read_bytes(), content, name)
        journal, stage, previous = backup.journal_paths(self.root / "data")
        self.assertFalse(journal.exists())
        self.assertFalse(stage.exists())
        self.assertEqual(previous.stat().st_mode & 0o777, 0o700)
        self.assertFalse(self.service.gate.exists())

    def test_wrong_format_or_network_and_missing_service_rejected(self):
        with self.assertRaises(ValueError):
            backup.unpack(self.archive, self.root / "full-stage")
        for case in ("network", "missing", "hook", "encrypted", "collision", "extra"):
            with self.subTest(case=case):
                stage = self.unpack(case)
                if case == "network":
                    atomic_json(stage / "channels.json", {"version": 1, "network_uuid": "9" * 32})
                elif case in ("missing", "hook"):
                    path = next((stage / "tvheadend/input/dvb/networks").glob("*/muxes/*"))
                    record = read_json(path)
                    if case == "missing":
                        record["config"]["services"].pop(self.extra_service)
                    else:
                        record["config"]["spawn_command"] = "evil"
                    atomic_json(path, record)
                elif case == "encrypted":
                    record = read_json(stage / "web/candidates.json")
                    record["channels"][-1]["encrypted"] = True
                    atomic_json(stage / "web/candidates.json", record)
                elif case == "collision":
                    record = read_json(stage / "management/library.json")
                    record["channels"][0]["uuid"] = "8" * 32
                    record["favourites"][-1] = "8" * 32
                    atomic_json(stage / "management/library.json", record)
                else:
                    atomic_json(stage / "identity.json", {})
                with self.assertRaises((ValueError, OSError)):
                    channel_backup.validate(stage, self.device)

    def test_tar_paths_links_duplicate_and_secret_files_refused(self):
        for name, kind in (("../../etc/shadow", tarfile.REGTYPE), ("config/web/login.json", tarfile.REGTYPE),
                           ("channels.json", tarfile.SYMTYPE)):
            with tarfile.open(self.archive, "w") as out:
                entry = tarfile.TarInfo(name)
                entry.type = kind
                out.addfile(entry, io.BytesIO(b""))
            with self.assertRaises(ValueError):
                self.unpack()

    def test_validation_cancellation_and_cross_kind_restore(self):
        before = (self.store / "management/library.json").read_bytes()
        result = self.validate_upload()
        self.assertEqual((result["available"], result["favourites"], result["kind"]), (3, 2, "channels"))
        self.assertEqual((self.store / "management/library.json").read_bytes(), before)
        with self.assertRaises(ValueError):
            self.service.handle("backup_restore", {"id": self.token})
        self.assertFalse(self.service.gate.exists())

    def test_rename_failure_retains_previous_library(self):
        self.validate_upload()
        before = (self.store / "management/library.json").read_bytes()
        original = backup.os.rename
        def rename(source, target):
            if Path(source).name == ".satip-restore-stage":
                raise OSError("fake interruption")
            return original(source, target)
        with patch.object(backup.os, "rename", rename), patch("os.chown"):
            with self.assertRaises(OSError):
                self.service.activate(channels=True)
        self.assertEqual((self.store / "management/library.json").read_bytes(), before)
        self.assertFalse(backup.journal_paths(self.root / "data")[0].exists())

    def test_committed_helper_recovery_does_not_reboot(self):
        self.validate_upload()
        self.service.handle("backup_channels_restore", {"id": self.token})
        with patch("os.chown"):
            self.service.activate(channels=True)
        commands = []
        self.device.command = commands.append
        self.service.prepare()
        self.assertEqual(read_json(self.service.result)["state"], "restored")
        self.assertFalse(commands)
        self.assertEqual(self.actions, ["stop", "start"])

    def test_insufficient_space_fails_before_activation(self):
        self.validate_upload()
        with patch("shutil.disk_usage", return_value=shutil._ntuple_diskusage(100, 100, 0)):
            with self.assertRaises(ValueError):
                self.service.activate(channels=True)
        self.assertTrue(self.store.exists())

    def test_channel_api_streaming_authentication_and_kind_separation(self):
        async def authenticate(request: Request):
            check_action(request)
        app = FastAPI(dependencies=[Depends(authenticate)])
        app.add_middleware(RequestLimits, backup_upload=True)
        async def helper(action, args=None):
            return self.service.handle(action, args)
        install(app, self.store / "config/web/login.json", helper, backup_root=self.root / "run")
        client = TestClient(app)
        auth = ("satip", "fake-web-password")
        headers = {"X-Satip-Action": "1", "Content-Type": "application/octet-stream"}
        path = "/api/device/channels/upload/" + self.token
        content = self.archive.read_bytes()
        self.assertEqual(client.post(path, content=content, headers=headers).status_code, 401)
        result = client.post(path, content=content, headers=headers, auth=auth)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["available"], 3)
        payload = {"action": "backup_channels_restore", "args": {"id": self.token}}
        self.assertEqual(client.post("/api/device", json=payload, headers={"X-Satip-Action": "1"}, auth=auth).status_code, 403)
        payload["current_password"] = auth[1]
        self.assertEqual(client.post("/api/device", json=payload, headers={"X-Satip-Action": "1"}, auth=auth).status_code, 200)
        self.service.pending = None
        self.service.gate.unlink()
        self.service.handle("backup_channels_create", {"id": self.token})
        self.service.not_before = 0
        self.service.tick()
        result = client.get("/api/device/channels/download/" + self.token, auth=auth)
        self.assertEqual(result.status_code, 200)
        self.assertIn("satip-channels-", result.headers["content-disposition"])
        self.assertEqual(client.get("/api/device/backup/download/" + self.token, auth=auth).status_code, 409)
        self.assertEqual(client.get("/api/device/channels/download/" + self.token).status_code, 401)


if __name__ == "__main__":
    unittest.main()
