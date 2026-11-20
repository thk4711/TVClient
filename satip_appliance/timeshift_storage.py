"""Root-only, fixed USB operations; disk identities are boot/hotplug specific."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import time

from satip_controller.state import atomic_json, boot_id


class StorageError(ValueError):
    pass


class Storage:
    def __init__(self, root, command):
        self.root = Path(root)
        self.command = command
        self.mount = self.root / "media/satip-timeshift"
        self.config = self.root / "data/store/identity/timeshift.json"
        self.ready = self.root / "run/satip-timeshift/storage.json"
        self.ejected = self.ready.with_name("ejected.json")
        self.message = ""
        self.next_retry = 0

    def tree(self):
        # BusyBox lsblk has no JSON/options support on the current image.
        base = self.root / "sys/class/block"
        result = []
        for sys in sorted(base.iterdir()):
            if not re.fullmatch(r"sd[a-z]+", sys.name) or "/usb" not in str(sys.resolve()):
                continue
            def node(path, kind):
                device = "/dev/" + path.name
                try:
                    text = self.command(["/sbin/blkid", "-p", "-o", "export", device], timeout=3).stdout
                except subprocess.CalledProcessError as error:
                    if error.returncode != 2:
                        raise StorageError("USB filesystem could not be identified") from None
                    text = ""
                attributes = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
                return {"name": path.name, "path": device, "type": kind, "tran": "usb",
                    "size": int((path / "size").read_text()) * 512,
                    "maj:min": (path / "dev").read_text().strip(),
                    "fstype": attributes.get("TYPE"), "uuid": attributes.get("UUID"), "mountpoints": []}
            disk = node(sys, "disk")
            for field in ("model", "serial"):
                path = sys / "device" / field
                disk[field] = path.read_text().strip() if path.exists() else ""
            disk["children"] = [node(p, "part") for p in sorted(sys.iterdir()) if (p / "partition").exists()]
            result.append(disk)
            if len(result) >= 16:
                break
        return result

    @staticmethod
    def descendants(disk):
        return [disk] + [n for c in disk.get("children", []) for n in Storage.descendants(c)]

    def identity(self, disk):
        name = disk["name"]
        if not re.fullmatch(r"sd[a-z]+", name) or disk.get("type") != "disk" or disk.get("tran") != "usb":
            raise StorageError("Only USB disks are eligible")
        sys = self.root / "sys/class/block" / name
        # diskseq distinguishes a replacement device even at the same USB port.
        sequence = (sys / "diskseq").read_text().strip()
        identity = [boot_id(), str(sys.resolve()), sequence, disk["maj:min"],
                    disk.get("serial"), disk["size"]]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def busy(self, disk):
        nodes = self.descendants(disk)
        devices = {n["maj:min"] for n in nodes}
        # Also covers mounts hidden by later mounts and bind mounts.
        for line in (self.root / "proc/self/mountinfo").read_text().splitlines():
            if line.split()[2] in devices:
                return True
        for n in nodes:
            if any(n.get("mountpoints") or []):
                return True
            holders = self.root / "sys/class/block" / n["name"] / "holders"
            if any(holders.iterdir()):
                return True
        for line in (self.root / "proc/swaps").read_text().splitlines()[1:]:
            try:
                value = os.stat(line.split()[0]).st_rdev
                if f"{os.major(value)}:{os.minor(value)}" in devices:
                    return True
            except OSError:
                return True
        return False

    def disks(self):
        result = []
        for disk in self.tree():
            if disk.get("tran") != "usb" or disk.get("type") != "disk":
                continue
            try:
                token = self.identity(disk)
                busy = self.busy(disk)
            except (OSError, StorageError):
                continue
            result.append({"id": token, "name": disk["name"],
                "model": (disk.get("model") or "USB disk").strip()[:100],
                "serial": (disk.get("serial") or "Not reported")[:100], "bytes": disk["size"],
                "busy": busy, "filesystems": [{"name": n["name"], "type": n.get("fstype"),
                    "uuid": n.get("uuid")} for n in self.descendants(disk) if n.get("fstype")]})
        return result[:16]

    def selected(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise StorageError("Select a USB disk again")
        for disk in self.tree():
            if disk.get("tran") != "usb" or disk.get("type") != "disk":
                continue
            if self.identity(disk) != identifier:
                continue
            if self.busy(disk):
                raise StorageError("Disk is mounted or in use. Eject it before formatting or mounting.")
            path = Path(disk["path"])
            if str(path) != "/dev/" + disk["name"]:
                raise StorageError("Unexpected USB device path")
            info = path.lstat()
            if not stat.S_ISBLK(info.st_mode) or f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}" != disk["maj:min"]:
                raise StorageError("USB device identity changed; refresh the list")
            return disk
        raise StorageError("USB disk changed or was disconnected; refresh the list")

    def mounted_device(self):
        for line in (self.root / "proc/self/mountinfo").read_text().splitlines():
            parts = line.split()
            if parts[4] == str(self.mount):
                after = parts.index("-")
                if parts[after + 1] != "ext4":
                    raise StorageError("Time shift storage is not writable ext4")
                return parts[2]
        return None

    def publish(self, config):
        device = self.mounted_device()
        if not device:
            raise StorageError("Time shift storage is not mounted")
        mounted = next(line.split() for line in (self.root / "proc/self/mountinfo").read_text().splitlines()
                       if line.split()[4] == str(self.mount))
        if "rw" not in mounted[5].split(","):
            raise StorageError("Time shift filesystem is read-only")
        directory = self.mount / "buffer"
        if directory.is_symlink():
            raise StorageError("Linked buffer directory refused")
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.stat().st_dev != self.mount.stat().st_dev:
            raise StorageError("Buffer directory is on another filesystem")
        os.chown(directory, 421, 421)
        os.chmod(directory, 0o700)
        space = os.statvfs(directory)
        if space.f_bavail * space.f_frsize < 2 * 1024**3:
            raise StorageError("At least 2 GiB free space is required")
        self.ready.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        atomic_json(self.ready, {"version": 1, "boot_id": boot_id(), "directory": str(directory),
            "device": device, "uuid": config["uuid"], "minutes": config["minutes"],
            "max_bytes": config["max_bytes"]}, 0o644, durable=False)

    def attach(self, node, config):
        if node.get("fstype") != "ext4" or not node.get("uuid"):
            raise StorageError("Use a disk with exactly one ext4 filesystem, or format it first")
        self.mount.mkdir(mode=0o755, parents=True, exist_ok=True)
        if self.mount.is_symlink() or self.mounted_device() or any(self.mount.iterdir()):
            raise StorageError("Time shift mountpoint is occupied")
        self.command(["/bin/mount", "-t", "ext4", "-o", "nosuid,nodev,noexec", node["path"], str(self.mount)])
        try:
            if self.mounted_device() != node["maj:min"]:
                raise StorageError("Mounted device identity mismatch")
            config["uuid"] = node["uuid"]
            self.publish(config)
            self.config.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(self.config, config, 0o600)
            self.ejected.unlink(missing_ok=True)
        except BaseException:
            self.ready.unlink(missing_ok=True)
            self.command(["/bin/umount", str(self.mount)])
            raise

    def formatter(self):
        # In-place test deployments may supply isolated, checksum-verified tools.
        private = self.root / "opt/satip-tools/e2fsprogs"
        if (private / "mke2fs").is_file():
            env = dict(os.environ, LD_LIBRARY_PATH=str(private / "lib"),
                       MKE2FS_CONFIG=str(private / "mke2fs.conf"))
            return str(private / "mke2fs"), env
        binary = self.root / "sbin/mke2fs"
        if not binary.exists() or binary.resolve().name == "busybox":
            raise StorageError("The ext4 formatter is not installed; no disk was erased")
        return str(binary), None

    def handle(self, action, args):
        if action == "storage_status":
            if args:
                raise StorageError("Invalid storage fields")
            return self.status()
        if action == "storage_limit":
            if set(args) != {"id", "gib"}:
                raise StorageError("Invalid storage fields")
            current = self.status()
            if (not current["mounted"] or not current["ready"] or not current["device_id"]
                    or args["id"] != current["device_id"]):
                raise StorageError("USB storage changed or is unavailable; refresh and retry")
            budget = self.budget(args["gib"], current["total_bytes"])
            config = self.saved()
            config.update(minutes=0, max_bytes=budget)
            self.publish(config)
            atomic_json(self.config, config, 0o600)
            return {"message": "Storage limit saved."}
        if action == "storage_eject":
            if args:
                raise StorageError("Invalid storage fields")
            # Revoke before stopping mpv so no new cache can be opened.
            self.ready.unlink(missing_ok=True)
            device = self.status().get("device_id")
            self.ejected.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(self.ejected, {"boot_id": boot_id(), "id": device}, 0o600, durable=False)
            self.command(["/usr/bin/python3", "-m", "satip_appliance.supervisor", "player", "stop"], timeout=20)
            try:
                if self.mounted_device():
                    self.command(["/bin/umount", str(self.mount)])
            finally:
                self.command(["/usr/bin/python3", "-m", "satip_appliance.supervisor", "player", "start"], timeout=20)
            return {"message": "USB storage safely removed. Its settings are remembered; reconnect it to use time shifting again."}
        if action == "storage_forget":
            if args or self.mounted_device():
                raise StorageError("Safely remove USB storage before forgetting its configuration")
            self.ready.unlink(missing_ok=True)
            self.config.unlink(missing_ok=True)
            self.ejected.unlink(missing_ok=True)
            return {"message": "USB storage configuration forgotten. No files on the device were erased."}
        required = {"id", "minutes", "gib"} | ({"confirm"} if action == "storage_format" else set())
        if action not in ("storage_format", "storage_mount") or set(args) != required:
            raise StorageError("Invalid storage action or fields")
        if type(args["minutes"]) is not int or not 0 <= args["minutes"] <= 120:
            raise StorageError("Use a session limit of 0–120 minutes (0 disables the time limit)")
        if self.mounted_device():
            raise StorageError("Eject the current time shift storage first")
        disk = self.selected(args["id"])
        budget = self.budget(args["gib"], disk["size"])
        if action == "storage_format":
            if args["confirm"] is not True:
                raise StorageError("Confirm that all data on the selected device will be lost")
            if disk["size"] < 3 * 1024**3:
                raise StorageError("Use a device of at least 3 GiB to leave 2 GiB free after formatting")
            formatter, environment = self.formatter()
            disk = self.selected(args["id"])
            self.command(["/sbin/sfdisk", "--lock=nonblock", "--wipe", "always", "--wipe-partitions", "always", disk["path"]],
                         input="label: gpt\n, , L\n", timeout=20)
            deadline = time.monotonic() + 5
            node = None
            while time.monotonic() < deadline:
                fresh = self.selected(args["id"])
                partitions = fresh.get("children", [])
                if len(partitions) == 1 and partitions[0].get("type") == "part" and Path(partitions[0]["path"]).exists():
                    node = partitions[0]
                    break
                time.sleep(.1)
            if node is None:
                raise StorageError("Partition creation did not complete; refresh storage status")
            self.command([formatter, "-t", "ext4", "-L", "satip-timeshift", "-m", "0", node["path"]],
                         timeout=120, **({"env": environment} if environment else {}))
            disk = self.selected(args["id"])
        filesystems = [n for n in self.descendants(disk) if n.get("fstype")]
        if len(filesystems) != 1:
            raise StorageError("Use a disk with exactly one ext4 filesystem, or format it first")
        config = {"version": 1, "minutes": args["minutes"], "max_bytes": budget}
        self.attach(filesystems[0], config)
        self.message = ""
        return {"message": "USB storage mounted."}

    @staticmethod
    def budget(gib, capacity):
        if (type(gib) not in (int, float) or not math.isfinite(gib)
                or not 1 <= gib <= capacity / 1024**3):
            raise StorageError("Use a storage limit from 1 GiB up to the USB device capacity")
        return int(gib * 1024**3)

    def saved(self):
        config = json.loads(self.config.read_text())
        if (not isinstance(config, dict) or set(config) != {"version", "uuid", "minutes", "max_bytes"}
                or config["version"] != 1 or not isinstance(config["uuid"], str)
                or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", config["uuid"])
                or type(config["minutes"]) is not int or not 0 <= config["minutes"] <= 120
                or type(config["max_bytes"]) is not int or not 1024**3 <= config["max_bytes"] < 2**63):
            raise StorageError("Invalid saved time shift configuration")
        return config

    def tick(self):
        if not self.config.exists():
            self.ready.unlink(missing_ok=True)
            return
        if time.monotonic() < self.next_retry:
            return
        self.next_retry = time.monotonic() + 5
        if self.mounted_device():
            if self.ejected.exists():
                try:
                    ejected = json.loads(self.ejected.read_text())
                except (OSError, ValueError):
                    ejected = {}
                if (ejected.get("boot_id") == boot_id() and any(
                        self.identity(disk) == ejected.get("id") for disk in self.tree())):
                    self.ready.unlink(missing_ok=True)
                    return  # A failed/busy unmount must not re-authorize capture.
            if not self.config.exists():
                return
            config = self.saved()
            nodes = [n for d in self.tree() if d.get("tran") == "usb"
                     for n in self.descendants(d) if n.get("uuid") == config.get("uuid")]
            if len(nodes) != 1 or nodes[0]["maj:min"] != self.mounted_device():
                self.ready.unlink(missing_ok=True)
                self.message = "USB disconnected. Eject the stale mount before reconnecting."
            elif not self.ready.exists():
                self.publish(config)
            return
        self.ready.unlink(missing_ok=True)
        if not self.config.exists():
            return
        config = self.saved()
        matches = [(d, n) for d in self.tree() if d.get("tran") == "usb"
                   for n in self.descendants(d) if n.get("uuid") == config.get("uuid")]
        if self.ejected.exists():
            try:
                ejected = json.loads(self.ejected.read_text())
            except (OSError, ValueError):
                ejected = {}
            if (ejected.get("boot_id") == boot_id() and len(matches) == 1
                    and ejected.get("id") == self.identity(matches[0][0])):
                return
            self.ejected.unlink(missing_ok=True)
        if len(matches) == 1 and not self.busy(matches[0][0]):
            self.attach(matches[0][1], config)
            self.message = "Saved USB storage mounted."

    def status(self):
        mounted_device = self.mounted_device()
        mounted = bool(mounted_device)
        space = os.statvfs(self.mount) if mounted else None
        disks = self.disks()
        device = next((d for d in self.tree() if any(n["maj:min"] == mounted_device
                       for n in self.descendants(d))), None) if mounted else None
        selected = next((d for d in disks if device and d["name"] == device["name"]), None)
        config = self.saved() if mounted and self.config.exists() else None
        return {"disks": disks, "mounted": mounted, "ready": self.ready.exists(),
            "remembered": self.config.exists(), "safely_removed": self.ejected.exists(),
            "device": selected, "device_id": selected["id"] if selected else None,
            "total_bytes": selected["bytes"] if selected else (space.f_blocks * space.f_frsize if space else None),
            "max_bytes": config["max_bytes"] if config else None,
            "free_bytes": space.f_bavail * space.f_frsize if space else None,
            "message": self.message}
