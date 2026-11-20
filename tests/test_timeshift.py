"""No real storage, formatting, tuner or mpv access in these tests."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from satip_appliance.timeshift_storage import Storage, StorageError
from satip_controller.input import binding
from satip_controller.timeshift import Timeshift, ranges, storage

CONFIG = {"version": 1, "boot_id": "boot", "directory": "/media/satip-timeshift/buffer",
          "device": "8:1", "uuid": "test-uuid", "minutes": 60, "max_bytes": 8 * 1024**3}
DISK = {"name": "sda", "path": "/dev/sda", "type": "disk", "tran": "usb", "size": 32 * 1024**3,
        "maj:min": "8:0", "model": "Test", "serial": "123", "children": [
            {"name": "sda1", "path": "/dev/sda1", "type": "part", "size": 31 * 1024**3,
             "maj:min": "8:1", "fstype": "ext4", "uuid": "test-uuid", "mountpoints": []}]}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.command = Mock(return_value=SimpleNamespace(stdout=""))
        self.store = Storage(self.root, self.command)
        for name in ("proc/self", "sys/class/block/sda/holders", "sys/class/block/sda1/holders",
                     "sbin", "data/store/identity"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        (self.root / "proc/self/mountinfo").write_text("")
        (self.root / "proc/swaps").write_text("Filename Type Size Used Priority\n")
        (self.root / "sys/class/block/sda/diskseq").write_text("1")
        self.disk = copy.deepcopy(DISK)
        self.store.tree = Mock(return_value=[self.disk])
        self.identifier = self.store.identity(self.disk)

    def args(self, **kwargs):
        return {"id": self.identifier, "minutes": 60, "gib": 8, **kwargs}

    def test_identity_changes_on_hotplug(self):
        (self.root / "sys/class/block/sda/diskseq").write_text("2")
        self.assertNotEqual(self.identifier, self.store.identity(self.disk))
        with self.assertRaisesRegex(StorageError, "changed"):
            self.store.selected(self.identifier)
        self.command.assert_not_called()

    def test_budget_accepts_full_large_device_and_fractional_gib(self):
        self.assertEqual(self.store.budget(256, 256 * 1024**3), 256 * 1024**3)
        self.assertEqual(self.store.budget(31.5, 32 * 1024**3), int(31.5 * 1024**3))
        for value in (True, 0, 33, float("nan"), float("inf"), "8"):
            with self.assertRaises(StorageError):
                self.store.budget(value, 32 * 1024**3)

    def test_limit_saves_without_formatting_and_rejects_stale_device(self):
        config = {"version": 1, "uuid": "test-uuid", "minutes": 60, "max_bytes": 8 * 1024**3}
        self.store.config.write_text(json.dumps(config))
        self.store.status = Mock(return_value={"mounted": True, "ready": True,
            "device_id": self.identifier, "total_bytes": 256 * 1024**3})
        self.store.publish = Mock()
        self.store.handle("storage_limit", {"id": self.identifier, "gib": 256})
        self.assertEqual(self.store.saved()["max_bytes"], 256 * 1024**3)
        self.assertEqual(self.store.saved()["minutes"], 0)
        self.command.assert_not_called()
        for args in ({"id": "stale", "gib": 8}, {"id": self.identifier, "gib": 257},
                     {"id": self.identifier, "gib": 8, "extra": True}):
            with self.assertRaises(StorageError):
                self.store.handle("storage_limit", args)

    def test_mounted_status_identifies_device_and_budget(self):
        self.store.mount.mkdir(parents=True)
        (self.root / "proc/self/mountinfo").write_text(
            f"1 0 8:1 / {self.store.mount} rw - ext4 /dev/sda1 rw\n")
        self.store.config.write_text(json.dumps({"version": 1, "uuid": "test-uuid",
            "minutes": 0, "max_bytes": 8 * 1024**3}))
        result = self.store.status()
        self.assertEqual(result["device"]["name"], "sda")
        self.assertEqual(result["device_id"], self.identifier)
        self.assertEqual(result["total_bytes"], DISK["size"])
        self.assertEqual(result["max_bytes"], 8 * 1024**3)

    def test_only_usb_whole_disks(self):
        for name, tran, kind in (("mmcblk0", "mmc", "disk"), ("sda1", "usb", "part"), ("sda", "sata", "disk")):
            with self.assertRaises(StorageError):
                self.store.identity(dict(self.disk, name=name, tran=tran, type=kind))

    def test_mounted_child_bind_mount_and_holders_are_busy(self):
        (self.root / "proc/self/mountinfo").write_text("1 0 8:1 / /other rw - ext4 /dev/sda1 rw\n")
        self.assertTrue(self.store.busy(self.disk))
        with self.assertRaisesRegex(StorageError, "in use"):
            self.store.selected(self.identifier)
        (self.root / "proc/self/mountinfo").write_text("")
        (self.root / "sys/class/block/sda1/holders/dm-0").touch()
        self.assertTrue(self.store.busy(self.disk))

    def test_busybox_formatter_is_rejected_before_erasing(self):
        (self.root / "bin").mkdir()
        (self.root / "bin/busybox").touch()
        (self.root / "sbin/mke2fs").symlink_to("../bin/busybox")
        self.store.selected = Mock(return_value=self.disk)
        with self.assertRaisesRegex(StorageError, "no disk was erased"):
            self.store.handle("storage_format", self.args(confirm=True))
        self.command.assert_not_called()

    def test_confirmation_limits_and_extra_fields_fail_without_commands(self):
        self.store.selected = Mock(return_value=self.disk)
        for args in (self.args(confirm="yes"), self.args(confirm=False),
                     self.args(confirm=True, minutes=True), self.args(confirm=True, gib=101),
                     self.args(confirm=True, path="/dev/mmcblk0")):
            with self.assertRaises(StorageError):
                self.store.handle("storage_format", args)
        self.command.assert_not_called()

    def test_format_sequence_and_fixed_commands(self):
        self.store.selected = Mock(return_value=self.disk)
        self.store.formatter = Mock(return_value=("/sbin/mke2fs", None))
        self.store.mounted_device = Mock(return_value=None)
        self.store.attach = Mock()
        with patch("satip_appliance.timeshift_storage.Path.exists", return_value=True):
            self.store.handle("storage_format", self.args(confirm=True))
        commands = [c.args[0] for c in self.command.call_args_list]
        self.assertEqual(commands[0][0], "/sbin/sfdisk")
        self.assertIn("--lock=nonblock", commands[0])
        self.assertEqual(commands[0][-1], "/dev/sda")
        self.assertEqual(commands[1], ["/sbin/mke2fs", "-t", "ext4", "-L", "satip-timeshift", "-m", "0", "/dev/sda1"])
        self.assertEqual(self.store.selected.call_count, 4)
        self.store.attach.assert_called_once()

    def test_mount_never_formats(self):
        self.store.selected = Mock(return_value=self.disk)
        self.store.attach = Mock()
        self.store.handle("storage_mount", self.args())
        self.command.assert_not_called()
        self.store.attach.assert_called_once()
        self.disk["children"][0]["fstype"] = "vfat"
        self.store.attach = Storage.attach.__get__(self.store)
        with self.assertRaises(StorageError):
            self.store.handle("storage_mount", self.args())
        self.command.assert_not_called()

    def test_eject_revokes_before_stopping_and_restarts_after_failure(self):
        self.store.ready.parent.mkdir(parents=True)
        self.store.ready.write_text("{}")
        self.store.config.write_text(json.dumps({"version": 1, "uuid": "test-uuid", "minutes": 0, "max_bytes": 8 * 1024**3}))
        self.store.status = Mock(return_value={"device_id": self.identifier})
        self.store.mounted_device = Mock(return_value="8:1")
        def command(argv, **kwargs):
            self.assertFalse(self.store.ready.exists())
            if argv[0] == "/bin/umount":
                raise OSError("busy")
        self.command.side_effect = command
        with self.assertRaises(OSError):
            self.store.handle("storage_eject", {})
        self.assertEqual(self.command.call_args_list[-1].args[0][-1], "start")

    def test_duplicate_saved_uuid_does_not_auto_mount(self):
        self.store.config.write_text(json.dumps({"version": 1, "uuid": "test-uuid", "minutes": 60, "max_bytes": 8 * 1024**3}))
        self.store.tree.return_value.append(copy.deepcopy(self.disk))
        self.store.attach = Mock()
        self.store.tick()
        self.store.attach.assert_not_called()
        self.assertFalse(self.store.ready.exists())

    def test_reboot_remount_uses_saved_uuid_even_when_device_name_changes(self):
        config = {"version": 1, "minutes": 60, "max_bytes": 8 * 1024**3}
        self.store.publish = Mock()

        def mount(argv, **kwargs):
            self.assertEqual(argv[0], "/bin/mount")
            device = "8:1" if argv[-2] == "/dev/sda1" else "8:17"
            (self.root / "proc/self/mountinfo").write_text(
                f"1 0 {device} / {self.store.mount} rw - ext4 {argv[-2]} rw\n")
            return SimpleNamespace(stdout="")

        self.command.side_effect = mount
        self.store.attach(self.disk["children"][0], config)
        self.assertEqual(self.store.saved()["uuid"], "test-uuid")
        self.assertEqual(self.store.config.stat().st_mode & 0o777, 0o600)
        # Simulate a cold boot: only persistent settings survive. /dev/sdX
        # enumeration and the helper's in-memory state must not matter.
        (self.root / "proc/self/mountinfo").write_text("")
        rebooted = Storage(self.root, self.command)
        renamed = copy.deepcopy(self.disk)
        renamed.update(name="sdb", path="/dev/sdb", **{"maj:min": "8:16"})
        renamed["children"][0].update(name="sdb1", path="/dev/sdb1", **{"maj:min": "8:17"})
        rebooted.tree = Mock(return_value=[renamed])
        rebooted.busy = Mock(return_value=False)
        rebooted.publish = Mock()
        self.command.reset_mock()
        rebooted.tick()
        self.command.assert_called_once_with(["/bin/mount", "-t", "ext4", "-o",
            "nosuid,nodev,noexec", "/dev/sdb1", str(rebooted.mount)])
        rebooted.publish.assert_called_once_with(config)
        self.assertEqual(rebooted.saved(), config)
        self.assertEqual(rebooted.message, "Saved USB storage mounted.")

    def test_boot_retries_when_usb_appears_late(self):
        config = {"version": 1, "uuid": "test-uuid", "minutes": 60, "max_bytes": 8 * 1024**3}
        self.store.config.write_text(json.dumps(config))
        self.store.tree.return_value = []
        self.store.attach = Mock()
        with patch("satip_appliance.timeshift_storage.time.monotonic", return_value=100):
            self.store.tick()
        self.store.attach.assert_not_called()
        self.store.tree.return_value = [self.disk]
        with patch("satip_appliance.timeshift_storage.time.monotonic", return_value=106):
            self.store.tick()
        self.store.attach.assert_called_once_with(self.disk["children"][0], config)

    def test_boot_does_not_claim_busy_or_unconfigured_usb(self):
        self.store.attach = Mock()
        self.store.tick()
        self.store.attach.assert_not_called()
        config = {"version": 1, "uuid": "test-uuid", "minutes": 60, "max_bytes": 8 * 1024**3}
        self.store.config.write_text(json.dumps(config))
        self.store.busy = Mock(return_value=True)
        self.store.tick()
        self.store.attach.assert_not_called()
        self.command.assert_not_called()


class TimeshiftTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ts = Timeshift()
        self.ts.config = dict(CONFIG)
        self.ts.active = True
        self.ts.started = time.monotonic()
        self.ts.position = 90
        self.ts.ranges = [(0, 100)]
        self.ipc = Mock()
        self.ipc.command = AsyncMock()
        self.ipc.get = AsyncMock(side_effect=lambda name: {"pause": self.ts.paused, "time-pos": self.ts.position, "seeking": False,
            "demuxer-cache-state": {"seekable-ranges": [{"start": a, "end": b} for a, b in self.ts.ranges]}}[name])

    async def test_pause_and_seek_are_independent_of_tuner_release(self):
        await self.ts.action(self.ipc, "pause")
        self.assertTrue(self.ts.paused)
        self.ipc.command.assert_awaited_with("set_property", "pause", True)
        await self.ts.action(self.ipc, "seek_back")
        self.ipc.command.assert_awaited_with("seek", 60, "absolute+keyframes")
        self.assertGreater(self.ts.seek_until, time.monotonic())
        await self.ts.action(self.ipc, "live")
        self.assertFalse(self.ts.paused)
        self.ipc.command.assert_awaited_with("seek", 99, "absolute+keyframes")

    async def test_seek_is_clamped_to_existing_range(self):
        self.ts.position = 5
        await self.ts.action(self.ipc, "seek_back")
        self.ipc.command.assert_awaited_with("seek", 0, "absolute+keyframes")
        self.ts.position = 95
        await self.ts.action(self.ipc, "seek_forward")
        self.ipc.command.assert_awaited_with("seek", 99.5, "absolute+keyframes")

    def test_ranges_reject_nonfinite_and_bad_data(self):
        self.assertEqual(ranges({"seekable-ranges": [{"start": 1, "end": 5}, {"start": 0, "end": float('nan')},
            {"start": True, "end": 6}, {"start": 9, "end": 2}, "bad"]}), [(1, 5)])

    def test_former_mute_keys_toggle_timeshift_and_preserve_remote_centre(self):
        for group in ("keyboard", "remote"):
            self.assertEqual(binding(group, 164), "pause")
            self.assertEqual(binding(group, 106), "remote_right" if group == "remote" else "volume_up")
        self.assertEqual(binding("keyboard", 50), "pause")
        self.assertEqual(binding("keyboard", 113), "pause")
        self.assertEqual(binding("remote", 113), "menu")
        self.assertEqual(binding("keyboard", 57), "pause")
        self.assertEqual(binding("keyboard", 104), "seek_back")
        self.assertEqual(binding("keyboard", 109), "seek_forward")
        self.assertEqual(binding("keyboard", 102), "live")

    def test_readiness_never_uses_an_ordinary_directory(self):
        with patch("satip_controller.timeshift.READY") as ready, \
             patch("satip_controller.timeshift.read_json", return_value=CONFIG), \
             patch("satip_controller.timeshift.boot_id", return_value="boot"), \
             patch("satip_controller.timeshift.Path.read_text", return_value=""):
            ready.stat.return_value = SimpleNamespace(st_uid=0, st_mode=0o644)
            self.assertIsNone(storage())
