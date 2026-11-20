"""Restore only the fixed FAT provisioning file from the installed image template."""
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from satip_controller.state import atomic_json, read_json


def request_id(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("Not a regular request")
        node = json.loads(source.read(257))
    if (not isinstance(node, dict) or set(node) != {"request_id"}
            or not isinstance(node["request_id"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", node["request_id"])):
        raise ValueError("Invalid restore request")
    return node["request_id"]


def restore(root=Path("/")):
    root = Path(root)
    boot = root / "boot"
    # Never write into the root filesystem if the FAT partition is absent.
    if not os.path.ismount(boot) or not any(
            fields[1:3] == [str(boot), "vfat"]
            for fields in (line.split() for line in Path("/proc/mounts").read_text().splitlines())):
        raise ValueError("Boot FAT is not mounted")
    template = root / "usr/share/satip/satip-config.txt"
    fd = os.open(template, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Untrusted template")
        content = source.read(32769)
    if not content or len(content) > 32768:
        raise ValueError("Invalid template size")
    fd, name = tempfile.mkstemp(prefix=".satip-config-", dir=boot)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, boot / "satip-config.txt")
        directory = os.open(boot, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


class ConfigRestore:
    def __init__(self, store=Path("/data/store"), root=Path("/")):
        self.store, self.root = Path(store), Path(root)
        self.seen = None

    def refresh(self):
        try:
            identifier = request_id(self.store / "web/config-restore-request.json")
        except (OSError, ValueError, TypeError):
            return
        if identifier == self.seen:
            return
        result_path = self.store / "web/config-restore-result.json"
        try:
            result = read_json(result_path)
        except (OSError, ValueError):
            result = {}
        if result.get("request_id") == identifier:
            self.seen = identifier
            return
        result = {"request_id": identifier, "state": "restored"}
        try:
            if read_json(self.store / "installation.json").get("state") != "unconfigured":
                raise ValueError("Not in setup")
            restore(self.root)
        except (OSError, ValueError):
            result.update(state="error", message="Could not restore satip-config.txt. Check the boot FAT mount and image template, then retry.")
        atomic_json(result_path, result)
        os.chown(result_path, 422, 422)
        self.seen = identifier


def cleanup_import(root=Path("/")):
    root = Path(root)
    store = root / "data/store"
    marker = store / "identity/boot-import-cleanup.json"
    try:
        if read_json(store / "installation.json").get("origin") not in (None, "fresh"):
            return
        if read_json(store / "installation.json").get("state") != "unconfigured":
            return
        if not (store / "identity/development.txt").is_file():
            return
        if marker.exists() and read_json(marker).get("state") == "complete":
            return
        restore(root)
        atomic_json(marker, {"state": "complete"})
    except (OSError, ValueError):
        atomic_json(marker, {"state": "retry"})
