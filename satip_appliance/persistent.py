"""Fail-closed data preparation and first-import transaction. Root only on target."""
import argparse
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile

from satip_controller.state import atomic_json, read_json

SCHEMA = {"schema": 1, "reader_min": 1, "writer": 1, "rollback_min": 1}
# Fixed allowlist, shared by import and future consistent backup/restore.
IMPORT = {"etc/satip-player": "config/player", "etc/satip-web": "config/web",
          "var/lib/satip-player": "player", "var/lib/satip-management": "management",
          "var/lib/satip-web": "web", "var/lib/tvheadend/.hts/tvheadend": "tvheadend"}
TREES = {"config": (0, 0, 0o755), "config/player": (421, 421, 0o700),
         "config/web": (422, 422, 0o700), "player": (421, 423, 0o2750),
         "management": (422, 423, 0o2750), "web": (422, 422, 0o700),
         "tvheadend": (424, 424, 0o700), "network": (0, 0, 0o700),
         "identity": (0, 0, 0o700), "input": (0, 0, 0o700),
         "update": (0, 0, 0o700), "operations": (0, 0, 0o700)}
RUNTIME_FILES = {"status.json", "scan-lease.json", "preview-lease.json", "epg.json"}


def schema_valid(node):
    return (isinstance(node, dict) and set(node) == set(SCHEMA)
            and all(type(node[key]) is int and node[key] == value for key, value in SCHEMA.items()))


def validate_mount(data, mountinfo=None, probe=True):
    data = Path(data)
    if data.is_symlink() or not data.is_dir():
        raise ValueError("Data mount missing")
    lines = (mountinfo if mountinfo is not None else Path("/proc/self/mountinfo").read_text()).splitlines()
    rows = [line.split() for line in lines if len(line.split()) > 6 and line.split()[4] == str(data)]
    if len(rows) != 1:
        raise ValueError("Data must be an actual mount")
    row = rows[0]
    after = row[row.index("-") + 1:]
    if after[0] != "ext4" or "rw" not in row[5].split(",") or "rw" not in after[2].split(","):
        raise ValueError("Writable ext4 data required")
    expected = subprocess.check_output(["/sbin/blkid", "-L", "satip-data"], text=True, timeout=2).strip()
    if os.stat(expected).st_rdev != os.makedev(*map(int, row[2].split(":"))):
        raise ValueError("Wrong data device")
    if os.stat(data).st_dev == os.stat("/").st_dev:
        raise ValueError("Root slot cannot be data")
    if os.statvfs(data).f_bavail == 0:
        raise ValueError("Data filesystem full")
    if not probe:
        return
    fd, name = tempfile.mkstemp(prefix=".write-probe-", dir=data)
    try:
        os.write(fd, b"data\n")
        os.fsync(fd)
    finally:
        os.close(fd)
        Path(name).unlink()


def reject_links(tree):
    if tree.is_symlink():
        raise ValueError("Linked input refused")
    for path in tree.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("Only ordinary files and directories may be imported")


def permissions(store):
    reject_links(store)
    os.chmod(store, 0o755)
    # Web startup reads this non-secret installation-state marker. Archive
    # modes are deliberately ignored, so restore must reconstruct this mode.
    installation = store / "installation.json"
    if installation.is_file():
        os.chown(installation, 0, 0)
        os.chmod(installation, 0o644)
    for relative, (uid, gid, mode) in TREES.items():
        directory = store / relative
        directory.mkdir(parents=True, exist_ok=True)
        os.chown(directory, uid, gid)
        os.chmod(directory, mode)
        if relative == "config":
            continue
        for path in directory.rglob("*"):
            os.chown(path, uid, gid)
            os.chmod(path, mode if path.is_dir() else (0o640 if gid == 423 else 0o600))
            if relative == "player" and path.name in ("state.json", "youtube-identity.json") and path.is_file():
                os.chmod(path, 0o600)


def publish(data, source=None, backend_revision=None, fresh=False):
    """One-time, quiesced import. A single directory rename commits all trees."""
    data = Path(data)
    if (data / "store").exists() or (data / "store").is_symlink():
        raise ValueError("Existing installation must not be overwritten")
    stage = data / (".fresh-stage" if fresh else ".import-stage")
    if fresh and stage.exists():
        reject_links(stage)
        allowed = set(TREES) | {"schema.json", "installation.json"}
        temporary = [p for p in stage.iterdir() if p.is_file() and
                     re.fullmatch(r"\.(?:schema|installation)\.json[a-z0-9_]{8}", p.name)]
        if any(p.relative_to(stage).as_posix() not in allowed and p not in temporary for p in stage.rglob("*")):
            raise ValueError("Unexpected fresh initialization contents")
        # A power cut can leave atomic_json's temporary file before its rename.
        # Only our two non-secret seed records in the marked fresh stage qualify.
        for path in temporary:
            path.unlink()
    stage.mkdir(mode=0o700, exist_ok=fresh)
    if source is not None:
        source = Path(source)
        reject_links(source)
        if not backend_revision:
            raise ValueError("Backend source revision required")
        for old, new in IMPORT.items():
            origin = source / old
            if not origin.is_dir():
                raise ValueError("Incomplete import")
            shutil.copytree(origin, stage / new)
        # Copy explicit material only, never /etc wholesale or shadow databases.
        optional = {"etc/wpa_supplicant.conf": "network/wpa_supplicant.conf",
                    "etc/hostname": "identity/hostname", "etc/dropbear": "identity/dropbear",
                    "root/.ssh/authorized_keys": "identity/authorized_keys",
                    "etc/rc_keymaps": "input/rc_keymaps"}
        for old, new in optional.items():
            origin, destination = source / old, stage / new
            if origin.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                if origin.is_dir():
                    shutil.copytree(origin, destination)
                else:
                    shutil.copyfile(origin, destination)
        for tree in ("player", "management", "web"):
            for name in RUNTIME_FILES:
                path = stage / tree / name
                if path.is_file():
                    path.unlink()
        # Arbitrary recording hooks are outside the playback-only appliance.
        if any((stage / "tvheadend" / name).exists() for name in ("dvr", "autorec", "timerec")):
            raise ValueError("Review recording/hooks before importing a playback-only backend")
        atomic_json(stage / "backend.json", {"source_revision": backend_revision,
                    "selected_revision": "37453bc3fe5f9e10f3428ebb1abdc613f8b07186",
                    "qualified": False})
    permissions(stage)
    atomic_json(stage / "schema.json", SCHEMA)
    atomic_json(stage / "installation.json", {"state": "imported-unqualified" if source else "unconfigured"}, 0o644)
    for path in [*stage.rglob("*"), stage]:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    os.rename(stage, data / "store")
    fd = os.open(data, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def link(root, name, target):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() and os.readlink(path) == target:
        return
    if path.exists() or path.is_symlink():
        raise ValueError("Conflicting compatibility destination")
    path.symlink_to(target)


def prepare(root=Path("/"), data=Path("/data")):
    (root / "run/satip-data-ready").unlink(missing_ok=True)
    store = data / "store"
    reject_links(store)
    if not schema_valid(read_json(store / "schema.json")):
        raise ValueError("Incompatible persistent schema; no automatic migration")
    permissions(store)
    for tree in TREES:
        if "/" not in tree:
            link(root, "data/" + tree, "/data/store/" + tree)
    for name, target in {"etc/satip-player": "config/player", "etc/satip-web": "config/web",
                         "var/lib/satip-player": "player", "var/lib/satip-management": "management",
                         "var/lib/satip-web": "web", "etc/dropbear": "identity/dropbear"}.items():
        link(root, name, "/data/store/" + target)
    (store / "identity/dropbear").mkdir(mode=0o700, exist_ok=True)
    for name, uid, gid, mode in (("satip-player",421,421,0o700), ("satip-web",422,422,0o700), ("tvheadend",424,424,0o700),
                                 ("satip-status",421,423,0o2750), ("satip-management",422,423,0o2750), ("satip-supervisor",0,0,0o700)):
        directory = root / "run" / name
        if directory.is_symlink():
            raise ValueError("Linked runtime refused")
        directory.mkdir(mode=mode, parents=True, exist_ok=True)
        os.chown(directory, uid, gid)
        os.chmod(directory, mode)
    recovery = (store / "web/mux-rollback.json").exists()
    atomic_json(store / "management/recovery.json", {"pending": recovery}, 0o640)
    os.chown(store / "management/recovery.json", 422, 423)
    identity = store / "identity"
    # Restored configurations explicitly represent an absent Wi-Fi setup. Do
    # not accidentally keep the previous root-slot supplicant configuration.
    if (identity / "root-access.json").exists() and not (store / "network/wpa_supplicant.conf").exists():
        (root / "etc/wpa_supplicant.conf").unlink(missing_ok=True)
    for source, destination in ((identity / "hostname", "etc/hostname"),
                                (identity / "authorized_keys", "root/.ssh/authorized_keys"),
                                (store / "network/wpa_supplicant.conf", "etc/wpa_supplicant.conf")):
        if source.is_file():
            path = root / destination
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.is_symlink():
                raise ValueError("Linked generated file refused")
            shutil.copyfile(source, path)
            os.chmod(path, 0o644 if destination == "etc/hostname" else 0o600)
    atomic_json(root / "run/satip-data-ready", SCHEMA)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "initialize", "import"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--backend-revision")
    parser.add_argument("--quiesced", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    ready = Path("/run/satip-data-ready")
    ready.unlink(missing_ok=True)
    try:
        validate_mount(Path("/data"))
        if args.action == "import":
            if not args.quiesced or args.source is None:
                raise ValueError("Explicit quiesced snapshot required")
            publish(Path("/data"), args.source, args.backend_revision)
        elif args.action == "initialize":
            publish(Path("/data"))
        prepare()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        ready.unlink(missing_ok=True)
        raise SystemExit("Persistent preparation failed; application services remain disabled") from None


if __name__ == "__main__":
    main()
