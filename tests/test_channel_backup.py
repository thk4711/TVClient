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
        atomic_json(target.parent.parent / "config", {"class": "dvb_network_dvbs", "orbital_pos": "19.2E"})
        atomic_json(target, {"config": {"frequency": 10000000, "delsys": "DVB-S2", "orbital": "19.2E", "services": {
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

    def move_destination(self):
        from satip_appliance import provision
        destination = "9" * 32
        base = self.store / "tvheadend/input/dvb/networks"
        (base / self.network).rename(base / destination)
        config = read_json(self.store / "config/web/config.json")
        atomic_json(self.store / "config/web/config.json", {**config, "network_uuid": destination})
        self.destination = destination
        return provision

    def make_fresh_destination(self):
        self.move_destination()
        for key in read_json(self.store / "management/library.json")["channels"]:
            (self.store / "tvheadend/channel/config" / key["uuid"]).unlink()
        for name in ("management/library.json", "web/candidates.json", "config/web/config.json"):
            (self.store / name).unlink()
        atomic_json(self.store / "installation.json", {"state": "unconfigured"})
        atomic_json(self.store / "web/setup-state.json", {"network_uuid": self.destination,
            "server_uuid": "3" * 32, "frontend_uuid": "4" * 32, "prepared": True})

    def test_portable_restore_keeps_destination_identity_and_mux_bytes(self):
        self.move_destination()
        config = (self.store / "config/web/config.json").read_bytes()
        self.validate_upload()
        self.service.handle("backup_channels_restore", {"id": self.token})
        self.service.not_before = 0
        with patch("os.chown"):
            self.service.tick()
        self.assertEqual(read_json(self.service.result)["state"], "restored")
        self.assertEqual((self.store / "config/web/config.json").read_bytes(), config)
        self.assertFalse((self.store / "tvheadend/input/dvb/networks" / self.network).exists())
        for name, content in self.files.items():
            if name.startswith("tvheadend/input/"):
                target = name.replace(self.network, self.destination)
                self.assertEqual((self.store / target).read_bytes(), content)

    def test_legacy_satip_backup_is_portable(self):
        self.files["channels.json"] = json.dumps({"version": 1, "network_uuid": self.network}).encode()
        backup.write_archive(self.archive, self.files, channel_backup.FORMAT)
        self.move_destination()
        self.assertEqual(self.validate_upload()["favourites"], 2)

    def test_fresh_restore_finishes_setup_without_existing_library(self):
        from satip_appliance import provision
        from unittest.mock import AsyncMock, Mock
        self.make_fresh_destination()
        factory = (self.store / "identity/factory.json").read_bytes()
        self.validate_upload()
        self.service.handle("backup_channels_restore", {"id": self.token})
        self.service.not_before = 0
        with patch("os.chown"):
            self.service.tick()
        self.assertEqual(read_json(self.service.result)["state"], "restored")
        self.assertEqual(read_json(self.store / "installation.json"), {"state": "unconfigured"})
        self.assertEqual(read_json(self.store / "web/setup-complete.json")["network_uuid"], self.destination)
        backend = Mock()
        backend._request.return_value = {"entries": [{"uuid": self.destination}]}
        with patch("os.chown"), patch("satip_controller.backend.Backend", return_value=backend), patch("satip_appliance.setup.verify_access", new=AsyncMock()):
            provision.activate(self.root / "data")
        self.assertEqual(read_json(self.store / "installation.json")["state"], "configured")
        self.assertEqual((self.store / "identity/factory.json").read_bytes(), factory)
        self.assertEqual(read_json(self.store / "management/library.json")["favourites"], [self.second, self.channel])
        self.assertEqual(self.actions, ["stop", "start"])

    def test_fresh_restore_requires_prepared_satip_tuner(self):
        self.make_fresh_destination()
        state = read_json(self.store / "web/setup-state.json")
        for update in ({"prepared": False}, {"source": "enigma2"}):
            atomic_json(self.store / "web/setup-state.json", {**state, **update})
            with self.assertRaisesRegex(backup.BackupError, "prepare a SAT-IP tuner"):
                self.validate_upload()
        with self.assertRaisesRegex(backup.BackupError, "Complete initial setup"):
            self.service.handle("backup_create", {"id": self.token})

    def test_portable_mux_and_service_collisions_with_inactive_network_refused(self):
        stage = self.unpack()
        foreign = self.store / "tvheadend/input/dvb/networks" / ("8" * 32) / "muxes"
        foreign.mkdir(parents=True)
        path = foreign / ("d" * 32)
        atomic_json(path, {"config": {"services": {}}})
        with self.assertRaisesRegex(backup.BackupError, "identifiers conflict"):
            channel_backup.validate(stage, self.device)
        path.unlink()
        atomic_json(foreign / ("7" * 32), {"config": {"services": {self.extra_service: {"enabled": True}}}})
        with self.assertRaisesRegex(backup.BackupError, "services conflict"):
            channel_backup.validate(stage, self.device)

    def test_portable_enigma2_and_wrong_source_backups_remain_refused(self):
        stage = self.unpack()
        atomic_json(stage / "channels.json", {"version": 1, "network_uuid": self.network, "input_type": "iptv"})
        with self.assertRaisesRegex(backup.BackupError, "SAT-IP channel backup"):
            channel_backup.validate(stage, self.device)
        path = self.store / "tvheadend/input/iptv/networks" / self.network / "config"
        atomic_json(path, {})
        atomic_json(stage / "channels.json", {"version": 1, "network_uuid": "8" * 32, "input_type": "iptv"})
        with self.assertRaisesRegex(backup.BackupError, "Enigma2 channel backups require the same"):
            channel_backup.validate(stage, self.device)

    def test_inherited_orbital_positions_and_legacy_satellite_evidence(self):
        self.move_destination()
        stage = self.unpack()
        path = next((stage / "tvheadend/input/dvb/networks").glob("*/muxes/*"))
        record = read_json(path)
        record["config"]["orbital"] = ""
        atomic_json(path, record)
        # Version 2 explicitly declares the satellite even when all muxes inherit it.
        channel_backup.validate(stage, self.device)
        atomic_json(stage / "channels.json", {"version": 1, "network_uuid": self.network})
        with self.assertRaisesRegex(backup.BackupError, "does not identify its satellite"):
            channel_backup.validate(stage, self.device)
        evidence = path.parent / ("5" * 32)
        atomic_json(evidence, {"config": {"delsys": "DVB-S2", "orbital": "19.2E", "services": {}}})
        channel_backup.validate(stage, self.device)
        atomic_json(evidence, {"config": {"delsys": "DVB-S2", "orbital": "13.0E", "services": {}}})
        with self.assertRaisesRegex(backup.BackupError, "multiplexes do not match"):
            channel_backup.validate(stage, self.device)

    def test_wrong_satellite_and_delivery_system_refused_for_legacy_and_v2(self):
        for version in (1, 2):
            for key, value in (("orbital", "13.0E"), ("delsys", "DVB-T")):
                stage = self.unpack(f"satellite-{version}-{key}")
                if version == 1:
                    atomic_json(stage / "channels.json", {"version": 1, "network_uuid": self.network})
                path = next((stage / "tvheadend/input/dvb/networks").glob("*/muxes/*"))
                record = read_json(path)
                record["config"][key] = value
                atomic_json(path, record)
                with self.assertRaisesRegex(backup.BackupError, "multiplexes do not match"):
                    channel_backup.validate(stage, self.device)

    def test_restore_updates_active_source_snapshot_only(self):
        from satip_appliance.tv_source import profile_path
        old = {"version": 1, "network_uuid": self.network,
               "library": read_json(self.store / "management/library.json"),
               "candidates": {"channels": []}}
        atomic_json(profile_path(self.store, "satip"), old)
        inactive = profile_path(self.store, "enigma2")
        atomic_json(inactive, {**old, "network_uuid": "8" * 32})
        inactive_bytes = inactive.read_bytes()
        self.validate_upload()
        with patch("os.chown"):
            self.service.activate(channels=True)
        profile = read_json(profile_path(self.store, "satip"))
        self.assertEqual(profile["candidates"], json.loads(self.files["web/candidates.json"]))
        self.assertEqual(profile["library"], json.loads(self.files["management/library.json"]))
        self.assertEqual(inactive.read_bytes(), inactive_bytes)

    def test_device_returns_specific_safe_backup_refusals(self):
        self.device.work = (0, "wifi")
        with self.assertRaisesRegex(ValueError, "Finish the pending device change first"):
            self.device.handle("backup_channels_restore", {"id": self.token})
        self.device.work = None
        stage = self.unpack()
        atomic_json(stage / "channels.json", {"version": 2, "network_uuid": self.network,
            "source": "satip", "satellite": "13.0E"})
        with self.assertRaisesRegex(backup.BackupError, "Astra 19.2E SAT-IP"):
            channel_backup.validate(stage, self.device)

    def test_setup_app_streams_channel_upload_and_refuses_other_device_actions(self):
        from satip_appliance.web import create_setup_app
        from unittest.mock import AsyncMock
        self.make_fresh_destination()
        async def helper(action, args=None):
            return self.device.handle(action, args)
        app = create_setup_app(self.store, backend=AsyncMock(), hosts=["testserver"], device_helper=helper)
        client = TestClient(app)
        headers = {"X-Satip-Action": "1", "Content-Type": "application/octet-stream"}
        files = dict(self.files)
        mux_name = next(name for name in files if name.startswith("tvheadend/input/"))
        record = json.loads(files[mux_name])
        record["config"]["fixture_padding"] = "x" * 40000
        files[mux_name] = json.dumps(record).encode()
        backup.write_archive(self.archive, files, channel_backup.FORMAT)
        content = self.archive.read_bytes()
        self.assertGreater(len(content), 32768)
        result = client.post("/api/device/channels/upload/" + self.token, content=content, headers=headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["favourites"], 2)
        self.assertFalse((self.store / "management/library.json").exists())
        self.assertEqual(client.post("/api/device/backup/upload/" + ("6" * 32), content=content, headers=headers).status_code, 409)
        self.assertEqual(client.post("/api/device", json={"action": "reboot"}, headers={"X-Satip-Action": "1"}).status_code, 409)
        self.assertEqual(client.post("/api/device", json={"action": "backup_channels_restore", "args": {"id": self.token}}, headers={"X-Satip-Action": "1"}).status_code, 200)
        self.service.pending = None
        self.service.gate.unlink()

    def test_fresh_rename_failure_retains_unconfigured_installation(self):
        self.make_fresh_destination()
        self.validate_upload()
        original = backup.os.rename
        def rename(source, target):
            if Path(source).name == ".satip-restore-stage":
                raise OSError("interrupted fresh import")
            return original(source, target)
        with patch.object(backup.os, "rename", rename), patch("os.chown"), self.assertRaises(OSError):
            self.service.activate(channels=True)
        self.assertEqual(read_json(self.store / "installation.json"), {"state": "unconfigured"})
        self.assertFalse((self.store / "web/setup-complete.json").exists())
        self.assertFalse((self.store / "management/library.json").exists())

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
        login = read_json(self.store / "config/web/login.json")
        auth = (login["username"], login["password"])
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
