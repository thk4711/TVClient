"""Private, bounded configuration archives and journaled fixed-path activation.

Only the root helper creates/activates archives. No archive-selected OS paths,
links, modes, commands or ownership are honored. EPG/media/history are excluded.
"""
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tarfile
import time

from satip_controller.config import Config, credentials, library, uuid
from satip_controller.coordination import preview_held
from satip_controller.settings import validate_settings
from satip_controller.state import atomic_json, lease_active, read_json
from . import device_settings as settings, persistent
from .provision import DEFAULTS, REVISION, player_config, web_config


class BackupError(ValueError):
    """A fixed, secret-free backup refusal suitable for the browser."""


MAX_ARCHIVE = 32 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
MAX_FILES = 10000
FORMAT = "satip-configuration"
FILES = frozenset("""
schema.json installation.json factory-ready.json backend.json
config/player/config.json config/player/credentials.json
config/web/config.json config/web/setup.json config/web/login.json
config/web/tvheadend-admin.json config/web/preview-credentials.json
player/state.json player/youtube-identity.json
management/library.json management/settings.json management/youtube.json
web/candidates.json web/last-scan.json web/setup-state.json
web/setup-complete.json web/server-selection.json
web/tv-source-satip.json web/tv-source-enigma2.json
identity/development.txt identity/device.json identity/factory.json
identity/satip-server.json identity/timeshift.json identity/hostname
identity/authorized_keys identity/root-access.json
network/wpa_supplicant.conf
""".split())
REQUIRED = frozenset("""
schema.json installation.json factory-ready.json
config/player/config.json config/player/credentials.json config/web/config.json
config/web/login.json config/web/tvheadend-admin.json config/web/preview-credentials.json
management/library.json identity/development.txt identity/device.json
identity/factory.json identity/root-access.json identity/authorized_keys
""".split())
ROOT_HASH = re.compile(r"(?:|[!*]{1,2}|[$]6[$](?:rounds=[0-9]+[$])?[./A-Za-z0-9]{1,16}[$][./A-Za-z0-9]{86})")
TOKEN = re.compile(r"[0-9a-f]{32}")


def allowed(name):
    parts = name.split("/")
    if (name.startswith("/") or any(p in ("", ".", "..") for p in parts)
            or len(name) > 240 or "\\" in name or any(ord(c) < 32 for c in name)):
        return False
    if name in FILES:
        return True
    if name.startswith("identity/dropbear/"):
        return len(parts) == 3 and bool(re.fullmatch(r"dropbear_(rsa|ecdsa|ed25519)_host_key", parts[-1]))
    if name.startswith("input/rc_keymaps/"):
        return len(parts) == 3 and bool(re.fullmatch(r"[a-zA-Z0-9_-]+[.]toml", parts[-1]))
    if parts[0] != "tvheadend":
        return False
    # Exact playback-only backend namespaces. IPTV records are additionally
    # restricted to managed Enigma2 HTTP references during validation. No file inputs, recordings,
    # external grabbers, image payloads, EPG cache, hooks or historical backups.
    return bool(re.fullmatch(
        r"tvheadend/(?:config|(?:accesscontrol|passwd|profile|bouquet)/[0-9a-f]{32}"
        r"|channel/config/[0-9a-f]{32}|(?:epggrab|imagecache)/config"
        r"|input/satip/adapters/[0-9a-f]{32}"
        r"|input/(?:dvb|iptv)/networks/[0-9a-f]{32}/(?:config|muxes/[0-9a-f]{32}))", name))


def regular_bytes(path, limit=MAX_FILE):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Invalid backup file")
        content = source.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Backup file too large")
    return content


def sync_tree(tree):
    persistent.reject_links(tree)
    for path in [*tree.rglob("*"), tree]:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def selected(store):
    persistent.reject_links(store)
    result = {}
    for path in sorted(store.rglob("*")):
        name = path.relative_to(store).as_posix()
        if path.is_file() and allowed(name):
            result[name] = regular_bytes(path)
            if len(result) > MAX_FILES or sum(map(len, result.values())) > MAX_ARCHIVE - 1024 * 1024:
                raise ValueError("Configuration exceeds backup limits")
    return result


def effective_files(device):
    """Capture effective root access, not a stale/plaintext FAT recovery seed."""
    root = device.root
    values = settings.saved_preferences(root, device.parser)
    values.pop("root_password", None)
    values.pop("ssh_authorized_key", None)
    shadow = regular_bytes(root / "etc/shadow", 16384).decode()
    rows = [line.split(":") for line in shadow.splitlines() if line.startswith("root:")]
    if len(rows) != 1 or not ROOT_HASH.fullmatch(rows[0][1]):
        raise ValueError("Unsupported root password hash")
    hashed = rows[0][1]
    values.pop("recovery_password_hash", None)
    if hashed.startswith("$6$"):
        values["recovery_password_hash"] = hashed
    values["ssh_enabled"] = regular_bytes(root / "etc/satip-ssh-enabled", 16).decode().strip()
    device.parser["parse_config"]("".join(f"{k}={v}\n" for k, v in values.items()))
    keys = root / "root/.ssh/authorized_keys"
    wifi = root / "etc/wpa_supplicant.conf"
    result = {
        "identity/development.txt": "".join(f"{k}={v}\n" for k, v in values.items()).encode(),
        "identity/root-access.json": json.dumps({"version": 1, "hash": hashed}).encode(),
        "identity/authorized_keys": regular_bytes(keys, 16384) if keys.exists() else b"",
        "identity/device.json": json.dumps(settings.overrides(root)).encode(),
    }
    if wifi.exists() and "wifi_ssid" in values:
        # Regenerate from confirmed preferences, excluding transient supplicant state.
        result["network/wpa_supplicant.conf"] = settings.wifi_config(values).encode()
    return result


def write_archive(path, files, archive_format=FORMAT):
    manifest = {"format": archive_format, "version": 1, "backend_revision": REVISION,
        "created_at": datetime.now(timezone.utc).isoformat(), "schema": persistent.SCHEMA,
        "files": {name: {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                  for name, content in files.items()}}
    with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as out:
        for name, content in [("manifest.json", json.dumps(manifest, sort_keys=True).encode()), *sorted(files.items())]:
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(content), 0o600
            out.addfile(entry, io.BytesIO(content))
    if path.stat().st_size > MAX_ARCHIVE:
        raise ValueError("Configuration exceeds backup limits")
    with path.open("rb") as source:
        os.fsync(source.fileno())
    return manifest


def unpack(path, stage, archive_format=FORMAT, allow=allowed, required=REQUIRED):
    """Read only plain tar, never extractall or trust archive modes/paths."""
    if path.stat().st_size > MAX_ARCHIVE:
        raise ValueError("Backup exceeds 32 MiB")
    contents = {}
    with tarfile.open(fileobj=io.BytesIO(regular_bytes(path, MAX_ARCHIVE)), mode="r:") as archive:
        for entry in archive:
            if (not entry.isfile() or entry.pax_headers or entry.name in contents
                    or not (entry.name == "manifest.json" or allow(entry.name))
                    or entry.size < 0 or entry.size > MAX_FILE
                    or len(contents) >= MAX_FILES + 1):
                raise ValueError("Invalid backup archive entries")
            contents[entry.name] = archive.extractfile(entry).read(entry.size + 1)
    manifest = json.loads(contents.pop("manifest.json"))
    if (not isinstance(manifest, dict) or set(manifest) != {
            "format", "version", "backend_revision", "created_at", "schema", "files"}
            or manifest["format"] != archive_format or type(manifest["version"]) is not int
            or manifest["version"] != 1 or manifest["backend_revision"] != REVISION
            or not persistent.schema_valid(manifest["schema"])
            or not isinstance(manifest["created_at"], str) or len(manifest["created_at"]) > 64
            or not isinstance(manifest["files"], dict)
            or set(manifest["files"]) != set(contents) or not required <= set(contents)):
        raise ValueError("Incomplete or incompatible configuration backup")
    if sum(map(len, contents.values())) > MAX_ARCHIVE - 1024 * 1024:
        raise ValueError("Backup contents too large")
    for name, content in contents.items():
        record = manifest["files"][name]
        if (not isinstance(record, dict) or set(record) != {"size", "sha256"}
                or type(record["size"]) is not int or record["size"] != len(content)
                or record["sha256"] != hashlib.sha256(content).hexdigest()):
            raise ValueError("Backup checksum mismatch")
    stage.mkdir(mode=0o700)
    for name, content in contents.items():
        target = stage / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o600)
    return manifest


def backend_record(content):
    """Validate the pinned backend's JSON or GZIP0[01] packed settings.

    Independent bounded reader of the HTS message wire format: typed fields
    with a one-byte name length and fixed/varying payload length. Archive bytes
    are preserved; this reader is only for validation, not backend migration.
    """
    if not content.startswith(b"\xff\xffGZIP0"):
        return json.loads(content)
    import zlib
    if len(content) <= 12 or content[7:8] not in (b"0", b"1"):
        raise ValueError("Invalid packed backend settings")
    size = int.from_bytes(content[8:12], "big")
    if not 0 < size <= MAX_FILE:
        raise ValueError("Packed backend settings exceed limits")
    decoder = zlib.decompressobj(47)
    payload = decoder.decompress(content[12:], size + 1)
    if len(payload) != size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("Invalid packed backend settings")
    fields = 0
    def message(data, is_list=False, depth=0):
        nonlocal fields
        if depth > 32:
            raise ValueError("Backend settings nesting exceeds limits")
        result = [] if is_list else {}
        position = 0
        while position < len(data):
            fields += 1
            if fields > 100000 or len(data) - position < 3:
                raise ValueError("Invalid packed backend fields")
            kind, name_size = data[position:position + 2]
            position += 2
            if content[7:8] == b"0":
                if len(data) - position < 4:
                    raise ValueError("Truncated backend field")
                length = int.from_bytes(data[position:position + 4], "big")
                position += 4
            else:
                length = 0
                for _ in range(5):
                    if position >= len(data):
                        raise ValueError("Truncated backend field")
                    byte = data[position]
                    position += 1
                    length = (length << 7) | (byte & 127)
                    if not byte & 128:
                        break
                else:
                    raise ValueError("Invalid backend field length")
            if position + name_size + length > len(data):
                raise ValueError("Truncated backend field")
            name = data[position:position + name_size].decode("utf-8")
            position += name_size
            value = data[position:position + length]
            position += length
            if kind in (1, 5):
                value = message(value, kind == 5, depth + 1)
            elif kind == 3:
                value = value.decode("utf-8")
            elif kind == 2 and length <= 8:
                value = int.from_bytes(value, "little", signed=True)
            elif kind == 7 and length <= 1:
                value = bool(value and value[0])
            elif kind == 8 and length == 16:
                value = value.hex()
            elif kind != 4:
                raise ValueError("Unsupported backend field")
            if is_list:
                if name:
                    raise ValueError("Named backend list field")
                result.append(value)
            else:
                if not name or name in result or "\0" in name:
                    raise ValueError("Invalid backend map field")
                result[name] = value
        return result
    return message(payload)


def validate_stage(stage, device):
    node = lambda name: read_json(stage / name)
    if not persistent.schema_valid(node("schema.json")):
        raise ValueError("Incompatible data schema")
    installation = node("installation.json")
    if installation.get("state") not in ("configured", "qualified") or installation.get("origin") != "fresh":
        raise ValueError("Only configured appliance backups are supported")
    factory = node("identity/factory.json")
    preferences = device.parser["parse_config"]((stage / "identity/development.txt").read_text())
    if "root_password" in preferences:
        raise ValueError("Plaintext root password refused")
    base = factory["settings"]
    if set(base) - set(DEFAULTS) - {"satip_xml", "tuner_number"} or not set(DEFAULTS) <= set(base):
        raise ValueError("Invalid device preferences")
    device.parser["parse_config"]("".join(f"{k}={v}\n" for k, v in base.items()))
    override = node("identity/device.json")
    if (not isinstance(override, dict) or set(override) - {"timezone", "wifi_enabled", "boot_recovery"}
            or ("wifi_enabled" in override and type(override["wifi_enabled"]) is not bool)
            or ("boot_recovery" in override and not isinstance(override["boot_recovery"], dict))):
        raise ValueError("Invalid device overrides")
    device.timezone(override.get("timezone", base["timezone"]))
    access = node("identity/root-access.json")
    if (not isinstance(access, dict) or set(access) != {"version", "hash"}
            or type(access["version"]) is not int or access["version"] != 1
            or not isinstance(access["hash"], str) or not ROOT_HASH.fullmatch(access["hash"])):
        raise ValueError("Invalid root access record")
    keys = regular_bytes(stage / "identity/authorized_keys", 16384).decode()
    if "\0" in keys:
        raise ValueError("Invalid authorized keys")
    Config.load(stage / "config/player/config.json")
    if node("config/player/config.json") != player_config(base):
        raise ValueError("Unsupported player paths/settings")
    config = node("config/web/config.json")
    uuid(config["network_uuid"])
    uuid(config["playback_acl_uuid"])
    if config != web_config(config["network_uuid"], config["playback_acl_uuid"], base, config["allowed_hosts"]):
        raise ValueError("Unsupported management paths/settings")
    accounts = {name: credentials(stage / path) for name, path in {
        "playback": "config/player/credentials.json", "preview": "config/web/preview-credentials.json",
        "admin": "config/web/tvheadend-admin.json", "login": "config/web/login.json"}.items()}
    if accounts["playback"] != accounts["preview"] or any(factory[k] != accounts[k] for k in ("playback", "admin", "login")):
        raise ValueError("Inconsistent credentials")
    library(node("management/library.json"))
    if (stage / "management/settings.json").exists():
        validate_settings(node("management/settings.json"))
    backend = stage / "tvheadend"
    if node("factory-ready.json").get("backend_revision") != REVISION or not (backend / "config").is_file():
        raise ValueError("Incompatible backend")
    # UUIDs and account mappings must survive together.
    acl = node("tvheadend/accesscontrol/" + config["playback_acl_uuid"])
    if (acl.get("username") != accounts["playback"]["username"] or acl.get("admin")
            or acl.get("webui") or acl.get("prefix") != "127.0.0.1/32" or acl.get("dvr")):
        raise ValueError("Invalid playback access restrictions")
    import base64
    for role in ("admin", "playback"):
        record = node("tvheadend/passwd/" + factory["ids"][role + "_password"])
        if (record.get("username") != accounts[role]["username"] or record.get("password2") !=
                base64.b64encode(("TVHeadend-Hide-" + accounts[role]["password"]).encode()).decode()):
            raise ValueError("Inconsistent backend passwords")
    if not any((backend / ("input/" + kind + "/networks") / config["network_uuid"] / "config").is_file()
               for kind in ("dvb", "iptv")):
        raise ValueError("Missing channel network")
    from .tv_source import validate_profile, validate_iptv_record
    for source in ("satip", "enigma2"):
        path = stage / ("web/tv-source-" + source + ".json")
        if path.exists():
            validate_profile(read_json(path))
    for channel in node("management/library.json")["channels"]:
        if not (backend / "channel/config" / channel["uuid"]).is_file():
            raise ValueError("Missing backend channel mapping")
    # Validate JSON backend settings and reject arbitrary external-command hooks.
    expanded = 0
    for path in backend.rglob("*"):
        if path.is_file():
            content = regular_bytes(path)
            expanded += (int.from_bytes(content[8:12], "big")
                         if content.startswith(b"\xff\xffGZIP0") else len(content))
            if expanded > 64 * 1024 * 1024:
                raise ValueError("Expanded backend configuration exceeds limits")
            record = backend_record(content)
            if path.is_relative_to(backend / "input/iptv"):
                validate_iptv_record(path.relative_to(stage).as_posix(), record, stage)
            def hooks(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if (key.lower() in {"command", "spawn", "script"}
                                or key.lower().endswith(("_command", "_spawn", "_script"))) and child:
                            raise ValueError("External backend hooks are not supported")
                        hooks(child)
                elif isinstance(value, list):
                    for child in value:
                        hooks(child)
            hooks(record)


def journal_paths(data):
    return data / ".satip-restore.json", data / ".satip-restore-stage", data / ".satip-restore-before"


def recover_restore(data):
    """A crash before the activation commit rolls back; after it keeps the new tree."""
    journal, stage, before = journal_paths(data)
    if not journal.exists():
        return
    record = read_json(journal)
    if record not in ({"version": 1, "phase": p} for p in ("prepared", "old-moved", "activated")):
        raise ValueError("Invalid restore journal")
    store = data / "store"
    for path in (stage, before, store):
        if path.is_symlink():
            raise ValueError("Linked restore tree refused")
    if record["phase"] != "activated" and before.exists():
        if store.exists():
            if stage.exists():
                raise ValueError("Ambiguous restore state")
            os.rename(store, stage)
        os.rename(before, store)
        store.chmod(0o755)
        sync_directory(data)
    if record["phase"] != "activated" and stage.exists():
        persistent.reject_links(stage)
        shutil.rmtree(stage)
    if not store.is_dir():
        raise ValueError("Missing installation during restore recovery")
    journal.unlink()
    sync_directory(data)


def apply_root_access(root, current=None):
    """Regenerate the root hash without importing a whole shadow database.

    Explicit new FAT recovery edits take precedence. Web root-password changes
    keep this record in sync. Authorized keys use the canonical identity file.
    """
    path = root / "data/store/identity/root-access.json"
    if not path.exists():
        return
    node = read_json(path)
    hashed = node["hash"]
    if current and ("root_password" in current or "recovery_password_hash" in current):
        hashed = next(line.split(":")[1] for line in (root / "etc/shadow").read_text().splitlines() if line.startswith("root:"))
        atomic_json(path, {"version": 1, "hash": hashed})
    if not isinstance(hashed, str) or not ROOT_HASH.fullmatch(hashed):
        raise ValueError("Invalid root access record")
    shadow = root / "etc/shadow"
    rows = regular_bytes(shadow, 16384).decode().splitlines()
    if sum(row.startswith("root:") for row in rows) != 1:
        raise ValueError("Root account unavailable")
    settings.atomic_text(shadow, "\n".join("root:" + hashed + ":" + row.split(":", 2)[2] if row.startswith("root:") else row for row in rows) + "\n")


class Backups:
    def __init__(self, device):
        self.device = device
        self.root = device.root
        self.directory = self.root / "run/satip-backup"
        self.gate = self.directory / "maintenance.json"
        self.result = self.directory / "result.json"
        self.pending = None
        self.not_before = 0
        self.expires = 0

    def prepare(self):
        self.directory.mkdir(mode=0o750, exist_ok=True)
        if self.root == Path("/"):
            os.chown(self.directory, 0, 422)
        os.chmod(self.directory, 0o750)
        # Helper interruption must not leave TV disabled. Boot clears /run.
        if self.gate.exists():
            result = read_json(self.result)
            journal, _, _ = journal_paths(self.root / "data")
            channels = result.get("kind") == "channels"
            committed = result.get("state") in ("rebooting", "restored")
            if journal.exists():
                self.services("stop")
                committed = read_json(journal).get("phase") == "activated"
                recover_restore(self.root / "data")
            if committed and not channels:
                atomic_json(self.result, {"id": result.get("id"), "state": "rebooting"})
                self.device.command(["/sbin/reboot"])
                return
            self.services("start")
            self.gate.unlink()
            atomic_json(self.result, {"id": result.get("id"), "kind": result.get("kind", "configuration"),
                "state": "restored" if committed and channels else "error",
                "message": "Channels restored." if committed and channels else "Interrupted operation recovered. Retry."})
        self.expires = time.monotonic() + 600

    def services(self, action):
        self.device.command(["/usr/bin/python3", "-m", "satip_appliance.supervisor", "all", action], timeout=330)

    def quiet(self, *, source_recovery=False):
        source_gate = self.root / "run/satip-source-maintenance.json"
        if source_gate.exists():
            from satip_controller.coordination import owner_alive
            if not source_recovery or owner_alive(read_json(source_gate).get("owner")):
                raise BackupError("TV source maintenance is running")
        if not source_recovery and any((self.device.store / name).exists() for name in
                                       ("web/tv-source-transaction.json", "web/satip-tuner-transaction.json")):
            raise BackupError("Recover the interrupted TV source selection first")
        if self.device.work or self.device.trial():
            raise BackupError("Finish the pending device change first")
        store = self.device.store
        source_journal = source_recovery and any((store / ("web/enigma2-import/" + name)).exists()
                                               for name in ("mux-rollback.json", "network-rollback.json", "publish-rollback.json"))
        if (store / "web/mux-rollback.json").exists() or (not source_journal and settings.read_optional(store / "management/recovery.json").get("pending")):
            raise BackupError("Finish channel search or recovery first")
        if self.root == Path("/"):
            for name in ("scan-lease.json", "preview-lease.json"):
                lease = settings.read_optional(self.root / "run/satip-management" / name)
                if source_journal and name == "scan-lease.json":
                    continue
                if lease.get("recovery_pending") or (preview_held(lease) if name.startswith("preview") else lease_active(lease)):
                    raise BackupError("Stop Live TV or channel search first")

    def handle(self, action, args):
        if not isinstance(args, dict) or set(args) != {"id"} or not isinstance(args["id"], str) or not TOKEN.fullmatch(args["id"]):
            raise ValueError("Invalid backup request")
        identifier = args["id"]
        channels = action.startswith("backup_channels_")
        kind = "channels" if channels else "configuration"
        action = action.replace("backup_channels_", "backup_")
        if action == "backup_status":
            result = read_json(self.result) if self.result.exists() else {}
            return result if result.get("id") == identifier else {"id": identifier, "state": "missing"}
        self.quiet()
        if self.pending or self.gate.exists():
            raise BackupError("Configuration maintenance is already running")
        if not channels and read_json(self.device.store / "installation.json").get("state") == "unconfigured":
            raise BackupError("Complete initial setup before using configuration backups")
        if channels and action == "backup_create" and read_json(self.device.store / "installation.json").get("state") == "unconfigured":
            raise BackupError("Complete initial setup before downloading channel backups")
        self.prepare()
        if action == "backup_create":
            for old in self.directory.glob("download-*.tar"):
                old.unlink()
        if action == "backup_validate":
            atomic_json(self.result, {"id": identifier, "state": "error", "message": "Backup validation failed."})
            upload = self.root / "run/satip-web" / ("backup-upload-" + identifier)
            archive = self.directory / "incoming.tar"
            archive.write_bytes(regular_bytes(upload, MAX_ARCHIVE))
            archive.chmod(0o600)
            upload.unlink()
            stage = self.directory / "validated"
            if stage.exists():
                persistent.reject_links(stage)
                shutil.rmtree(stage)
            try:
                if channels:
                    from . import channel_backup
                    manifest = unpack(archive, stage, channel_backup.FORMAT, channel_backup.allowed, channel_backup.REQUIRED)
                    summary = channel_backup.validate(stage, self.device)
                else:
                    manifest = unpack(archive, stage)
                    validate_stage(stage, self.device)
                    summary = {}
            except Exception as error:
                if stage.exists():
                    persistent.reject_links(stage)
                    shutil.rmtree(stage)
                if isinstance(error, BackupError):
                    raise
                raise BackupError("Invalid or incompatible backup. Check the file format and backend version.") from None
            finally:
                archive.unlink(missing_ok=True)
            atomic_json(self.result, {"id": identifier, "state": "validated", "kind": kind, **summary, "created_at": manifest["created_at"],
                "message": ("Channel backup validated. Restore replaces channels without rebooting." if channels else
                            "Backup validated. Restore replaces settings and reboots the receiver.")})
        elif action in ("backup_create", "backup_restore"):
            if action == "backup_restore" and (self.handle("backup_status", args).get("state") != "validated"
                    or self.handle("backup_status", args).get("kind", "configuration") != kind):
                raise ValueError("Validate the backup before restoring")
            self.pending = (identifier, action if not channels else action.replace("backup_", "backup_channels_"))
            self.not_before = time.monotonic() + 2
            atomic_json(self.result, {"id": identifier, "state": "working", "kind": kind})
            atomic_json(self.gate, {"id": identifier})
        else:
            raise ValueError("Unsupported backup action")
        self.expires = time.monotonic() + 600
        return read_json(self.result)

    def tick(self):
        if not self.pending:
            if self.gate.exists():
                result = read_json(self.result)
                if result.get("state") == "rebooting":
                    self.device.command(["/sbin/reboot"])
                    return
                self.services("start")
                self.gate.unlink()
            if self.expires and time.monotonic() > self.expires:
                for path in [*self.directory.glob("download-*.tar"), self.result]:
                    path.unlink(missing_ok=True)
                stage = self.directory / "validated"
                if stage.exists():
                    persistent.reject_links(stage)
                    shutil.rmtree(stage)
                self.expires = 0
            return
        if time.monotonic() < self.not_before:
            return
        identifier, action = self.pending
        channels = action.startswith("backup_channels_")
        kind = "channels" if channels else "configuration"
        action = action.replace("backup_channels_", "backup_")
        self.pending = None
        stopped = False
        committed = False
        try:
            self.quiet()
            stopped = True
            self.services("stop")
            self.quiet()  # Check again after writers have drained.
            if action == "backup_create":
                if channels:
                    from . import channel_backup
                    files = channel_backup.selected(self.device)
                else:
                    files = selected(self.device.store)
                    files.pop("network/wpa_supplicant.conf", None)
                    files.update(effective_files(self.device))
                download = self.directory / ("download-" + identifier + ".tar")
                write_archive(download, files, channel_backup.FORMAT if channels else FORMAT)
                # Round-trip validation before offering a download.
                stage = self.directory / "check"
                if stage.exists():
                    persistent.reject_links(stage)
                    shutil.rmtree(stage)
                try:
                    if channels:
                        unpack(download, stage, channel_backup.FORMAT, channel_backup.allowed, channel_backup.REQUIRED)
                        channel_backup.validate(stage, self.device)
                    else:
                        unpack(download, stage)
                        validate_stage(stage, self.device)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
                if self.root == Path("/"):
                    os.chown(download, 0, 422)
                download.chmod(0o640)
                state = "ready"
            else:
                self.activate(channels=channels)
                committed = True
                if channels:
                    recover_restore(self.root / "data")
                state = "restored" if channels else "rebooting"
            atomic_json(self.result, {"id": identifier, "state": state, "kind": kind})
        except Exception:
            # Never expose passwords, raw JSON, archive names or command output.
            atomic_json(self.result, {"id": identifier, "state": "error", "kind": kind,
                "message": ("Restore committed; service recovery is pending. Check receiver status." if committed else
                            "Configuration operation failed. Previous settings retained; retry or check recovery.")})
        finally:
            if stopped and (not committed or channels):
                self.services("start")
            if not committed or channels:
                self.gate.unlink(missing_ok=True)
            self.expires = time.monotonic() + 600
        if committed and not channels:
            self.device.command(["/sbin/reboot"])

    def activate(self, channels=False):
        data = self.root / "data"
        journal, stage, before = journal_paths(data)
        if journal.exists() or stage.exists():
            raise ValueError("Previous restore needs recovery")
        validated = self.directory / "validated"
        if channels:
            from . import channel_backup
            channel_backup.validate(validated, self.device)
        else:
            validate_stage(validated, self.device)
        if channels:
            needed = sum(path.stat().st_size for path in self.device.store.rglob("*") if path.is_file())
            needed += sum(path.stat().st_size for path in validated.rglob("*") if path.is_file()) + 1024 * 1024
            if shutil.disk_usage(data).free < needed:
                raise ValueError("Insufficient space for safe channel restore")
        if before.exists():
            persistent.reject_links(before)
            shutil.rmtree(before)  # Fixed, private rollback tree from a prior completed restore.
        stage.mkdir(mode=0o700)
        try:
            if channels:
                persistent.reject_links(self.device.store)
                shutil.copytree(self.device.store, stage, dirs_exist_ok=True)
                channel_backup.overlay(stage, validated, self.device)
            else:
                shutil.copytree(validated, stage, dirs_exist_ok=True)
            # Operational history/update artifacts are not settings; preserve current ones.
            for name in ("operations", "update"):
                source = self.device.store / name
                if source.exists() and not channels:
                    persistent.reject_links(source)
                    shutil.copytree(source, stage / name)
            persistent.permissions(stage)
            # Retain player-private state after generic shared-tree permissions.
            for name in ("state.json", "youtube-identity.json"):
                if (stage / "player" / name).exists():
                    (stage / "player" / name).chmod(0o600)
            if not channels:
                device = read_json(stage / "identity/device.json")
                ignored = device.setdefault("boot_recovery", {})
                for key, value in self.device.parser["read_config"](self.root / "boot/satip-config.txt").items():
                    if key in ("ssh_enabled", "root_password", "recovery_password_hash", "ssh_authorized_key"):
                        ignored[key] = settings.fingerprint(value)
                atomic_json(stage / "identity/device.json", device)
            sync_tree(stage)
            atomic_json(journal, {"version": 1, "phase": "prepared"})
            os.rename(data / "store", before)
            before.chmod(0o700)
            sync_directory(data)
            atomic_json(journal, {"version": 1, "phase": "old-moved"})
            os.rename(stage, data / "store")
            sync_directory(data)
            atomic_json(journal, {"version": 1, "phase": "activated"})
        except Exception:
            if journal.exists():
                recover_restore(data)
            elif stage.exists():
                persistent.reject_links(stage)
                shutil.rmtree(stage)
            raise
