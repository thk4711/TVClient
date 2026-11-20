"""Channel-only snapshots; never import receiver identity, credentials or tuners."""
import json
import re

from satip_controller.config import Channel, library, uuid
from satip_controller.state import read_json
from . import backup

FORMAT = "satip-channels"
REQUIRED = frozenset({"channels.json", "management/library.json", "web/candidates.json"})


def allowed(name):
    return name in REQUIRED or bool(re.fullmatch(
        r"tvheadend/(?:channel/config/[0-9a-f]{32}|input/(?:dvb|iptv)/networks/[0-9a-f]{32}/muxes/[0-9a-f]{32})", name))


def destination_config(device):
    store = device.store
    installation = read_json(store / "installation.json").get("state")
    if installation in ("configured", "qualified"):
        return read_json(store / "config/web/config.json")
    if installation != "unconfigured":
        raise backup.BackupError("Incompatible receiver installation")
    path = store / "web/setup-state.json"
    state = read_json(path) if path.exists() else {}
    if (state.get("source", "satip") != "satip" or state.get("prepared") is not True
            or (store / "web/setup-complete.json").exists()):
        raise backup.BackupError("Select and prepare a SAT-IP tuner before restoring channels")
    for key in ("network_uuid", "server_uuid", "frontend_uuid"):
        uuid(state[key])
    from .provision import web_config
    from .web import allowed_hosts
    from pathlib import Path
    factory = read_json(store / "identity/factory.json")
    return web_config(state["network_uuid"], factory["ids"]["playback_acl"],
                      factory["settings"], allowed_hosts() if device.root == Path("/") else ["localhost"])


def current_library(store):
    path = store / "management/library.json"
    return library(read_json(path))[0] if path.exists() else {}


def metadata(device):
    from . import persistent
    if (not persistent.schema_valid(read_json(device.store / "schema.json"))
            or read_json(device.store / "factory-ready.json").get("backend_revision") != backup.REVISION):
        raise backup.BackupError("Incompatible receiver installation")
    config = destination_config(device)
    network = uuid(config["network_uuid"])
    if (device.store / "tvheadend/input/iptv/networks" / network / "config").is_file():
        return {"version": 1, "network_uuid": network, "input_type": "iptv"}
    path = device.store / "tvheadend/input/dvb/networks" / network / "config"
    record = backup.backend_record(backup.regular_bytes(path))
    if record.get("class") != "dvb_network_dvbs" or record.get("orbital_pos") != "19.2E":
        raise backup.BackupError("Channel restore requires a configured Astra 19.2E DVB-S/S2 network")
    return {"version": 2, "network_uuid": network, "source": "satip", "satellite": "19.2E"}


def archive_metadata(stage, device):
    meta = read_json(stage / "channels.json")
    destination = metadata(device)
    if destination.get("input_type") == "iptv":
        if meta != destination:
            raise backup.BackupError("Enigma2 channel backups require the same configured channel network")
        return meta
    if (not isinstance(meta, dict) or type(meta.get("version")) is not int
            or not ((meta["version"] == 1 and set(meta) == {"version", "network_uuid"})
                    or (meta["version"] == 2 and set(meta) == {"version", "network_uuid", "source", "satellite"}
                        and meta["source"] == "satip" and meta["satellite"] == destination["satellite"]))):
        raise backup.BackupError("Restore an Astra 19.2E SAT-IP channel backup for this source")
    uuid(meta["network_uuid"])
    return meta


def network_path(meta):
    return "tvheadend/input/" + meta.get("input_type", "dvb") + "/networks/" + meta["network_uuid"] + "/muxes/"


def selected(device):
    store = device.store
    backup.persistent.reject_links(store)
    channels, _ = library(read_json(store / "management/library.json"))
    meta = metadata(device)
    files = {"channels.json": json.dumps(meta).encode(),
             "management/library.json": backup.regular_bytes(store / "management/library.json")}
    candidates = store / "web/candidates.json"
    files["web/candidates.json"] = (backup.regular_bytes(candidates) if candidates.exists() else
        json.dumps({"channels": [{k: v for k, v in vars(c).items() if k != "uuid"} for c in channels.values()]}).encode())
    for channel in channels:
        name = "tvheadend/channel/config/" + channel
        files[name] = backup.regular_bytes(store / name)
    network = network_path(meta)
    for path in sorted((store / network).iterdir()):
        name = network + uuid(path.name)
        files[name] = backup.regular_bytes(path)
        if len(files) > backup.MAX_FILES or sum(map(len, files.values())) > backup.MAX_ARCHIVE - 1024 * 1024:
            raise backup.BackupError("Channel data exceeds backup limits")
    return files


def validate(stage, device):
    meta = archive_metadata(stage, device)
    destination = metadata(device)
    channels, favourites = library(read_json(stage / "management/library.json"))
    if len(channels) > 10000:
        raise backup.BackupError("Channel library exceeds limits")
    catalogue = read_json(stage / "web/candidates.json")
    if not isinstance(catalogue, dict) or set(catalogue) != {"channels"} or not isinstance(catalogue["channels"], list):
        raise backup.BackupError("Invalid channel catalogue")
    if len(catalogue["channels"]) > 10000:
        raise backup.BackupError("Channel catalogue exceeds limits")
    items = {}
    for item in catalogue["channels"]:
        if not isinstance(item, dict) or set(item) not in (
                {"name", "service_uuid", "mux_uuid", "video", "audio_pid"},
                {"name", "service_uuid", "mux_uuid", "video", "audio_pid", "encrypted"},
                set(Channel.__dataclass_fields__)):
            raise backup.BackupError("Invalid available channel fields")
        if "encrypted" in item and item["encrypted"] is not False:
            raise backup.BackupError("Encrypted channel refused")
        parsed = Channel.parse({**{k: v for k, v in item.items() if k != "encrypted"},
                                "uuid": item.get("uuid", item["service_uuid"])})
        if parsed.service_uuid in items:
            raise backup.BackupError("Duplicate available channel")
        items[parsed.service_uuid] = parsed
    network = network_path(meta)
    services = {}
    orbital_evidence = False
    expanded = 0
    for path in stage.rglob("*"):
        if not path.is_file():
            continue
        name = path.relative_to(stage).as_posix()
        if not allowed(name):
            raise backup.BackupError("Non-channel data refused")
        if name.startswith("tvheadend/input/"):
            if not name.startswith(network):
                raise backup.BackupError("Foreign channel network refused")
            content = backup.regular_bytes(path)
            expanded += int.from_bytes(content[8:12], "big") if content.startswith(b"\xff\xffGZIP0") else len(content)
            if expanded > 64 * 1024 * 1024:
                raise backup.BackupError("Expanded channel data exceeds limits")
            record = backup.backend_record(content)
            if "/input/iptv/" in name:
                from .tv_source import validate_iptv_record
                validate_iptv_record(name, record, device.store)
            if not isinstance(record, dict) or set(record) != {"config"} or not isinstance(record["config"], dict):
                raise backup.BackupError("Invalid multiplex record")
            config = record["config"]
            if destination.get("source") == "satip":
                orbital_evidence |= config.get("orbital") == destination["satellite"]
                # Empty orbital position inherits the enclosing network in Tvheadend.
                if (config.get("orbital", "") not in ("", destination["satellite"])
                        or config.get("delsys") not in ("DVB-S", "DVB-S2")
                        or config.get("dvb_satip_dvbc_freq") or config.get("dvb_satip_dvbt_freq")):
                    raise backup.BackupError("Backup multiplexes do not match Astra 19.2E DVB-S/S2 reception")
            def hooks(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if (key.lower() in {"command", "spawn", "script"} or
                                key.lower().endswith(("_command", "_spawn", "_script"))) and child:
                            raise backup.BackupError("External hooks refused")
                        hooks(child)
                elif isinstance(value, list):
                    for child in value:
                        hooks(child)
            hooks(config)
            service_records = config.get("services", {})
            if not isinstance(service_records, dict):
                raise backup.BackupError("Invalid services")
            services[path.name] = {uuid(key): value for key, value in service_records.items()}
    if (destination.get("source") == "satip" and meta["version"] == 1
            and meta["network_uuid"] != destination["network_uuid"] and not orbital_evidence):
        raise backup.BackupError("Legacy backup does not identify its satellite; use a new channel backup or rescan")
    for item in [*channels.values(), *items.values()]:
        service = services.get(item.mux_uuid, {}).get(item.service_uuid)
        if not isinstance(service, dict) or service.get("enabled") not in (True, 1):
            raise backup.BackupError("Missing enabled channel service")
    config = destination_config(device)
    acl = read_json(device.store / "tvheadend/accesscontrol" / config["playback_acl_uuid"])
    old_channels = current_library(device.store)
    from .tv_source import profile_path, validate_profile
    source = "enigma2" if destination.get("input_type") == "iptv" else "satip"
    profile = profile_path(device.store, source)
    if profile.exists() and validate_profile(read_json(profile))["network_uuid"] != config["network_uuid"]:
        raise backup.BackupError("Saved TV source does not match the active channel network")
    # Tvheadend identifiers are global, including inactive source lineups.
    # Never let an imported mux/service alias an unrelated backend object.
    incoming = set(channels) | set(services) | {key for records in services.values() for key in records}
    if sum(map(len, services.values())) + len(services) + len(channels) != len(incoming):
        raise backup.BackupError("Conflicting identifiers inside channel backup")
    target_network = network_path(destination)
    owned_channels = {"tvheadend/channel/config/" + key for key in old_channels}
    old_services = set()
    for path in (device.store / target_network).glob("*"):
        record = backup.backend_record(backup.regular_bytes(path))
        old_services.update(record.get("config", {}).get("services", {}))
    for path in (device.store / "tvheadend").rglob("*"):
        if not path.is_file():
            continue
        name = path.relative_to(device.store).as_posix()
        owned = name.startswith(target_network) or name in owned_channels
        if path.name == "config" and re.fullmatch("[0-9a-f]{32}", path.parent.name) and path.parent.name in incoming:
            raise backup.BackupError("Backup identifiers conflict with a configured network")
        if not owned and path.name in incoming:
            raise backup.BackupError("Backup identifiers conflict with another configured source")
        if "/muxes/" in name and not name.startswith(target_network):
            record = backup.backend_record(backup.regular_bytes(path))
            if incoming & set(record.get("config", {}).get("services", {})):
                raise backup.BackupError("Backup services conflict with another configured source")
        if name.startswith("tvheadend/channel/config/") and not owned:
            record = backup.backend_record(backup.regular_bytes(path))
            if set(record.get("services", [])) & (old_services | incoming):
                raise backup.BackupError("Unrelated channels depend on the lineup being replaced")
    maximum = acl["channel_max"]
    expected = {"tvheadend/channel/config/" + key for key in channels}
    actual = {p.relative_to(stage).as_posix() for p in (stage / "tvheadend/channel/config").glob("*")}
    if actual != expected:
        raise backup.BackupError("Unexpected backend channels")
    if acl.get("admin") or acl.get("webui") or acl.get("conn_limit") != 1:
        raise backup.BackupError("Invalid playback access restrictions")
    for key, item in channels.items():
        record = backup.backend_record(backup.regular_bytes(stage / "tvheadend/channel/config" / key))
        if (not isinstance(record, dict) or set(record) - {
                "name", "enabled", "services", "number", "epgauto", "epglimit", "epg_running",
                "dvr_pre_time", "dvr_pst_time", "bouquet", "tags"}
                or record.get("services") != [item.service_uuid] or record.get("enabled") is not True
                or type(record.get("number")) is not int or not acl["channel_min"] <= record["number"] <= 2147483647
                or record.get("bouquet") or record.get("tags")):
            raise backup.BackupError("Invalid backend channel mapping")
        if key not in old_channels and (device.store / "tvheadend/channel/config" / key).exists():
            raise backup.BackupError("Channel UUID collision")
        maximum = max(maximum, record["number"])
    if maximum > acl["channel_max"]:
        for path in (device.store / "tvheadend/channel/config").glob("*"):
            record = backup.backend_record(backup.regular_bytes(path))
            if path.name not in old_channels and acl["channel_max"] < record.get("number", 0) <= maximum:
                raise backup.BackupError("Unrelated channel would gain playback access")
    return {"available": len(set(items) | {c.service_uuid for c in channels.values()}),
            "favourites": len(favourites), "channel_max": maximum}


def overlay(stage, validated, device):
    """Replace only fixed channel paths in a copy of the current installation."""
    summary = validate(validated, device)
    old_channels = current_library(stage)
    network = network_path(metadata(device)).rstrip("/")
    archive_network = network_path(archive_metadata(validated, device))
    for path in [*(stage / network).glob("*"),
                 *(stage / "tvheadend/channel/config" / key for key in old_channels)]:
        path.unlink()
    for source in validated.rglob("*"):
        if not source.is_file() or source.name == "channels.json":
            continue
        name = source.relative_to(validated).as_posix()
        if name.startswith(archive_network):
            name = network + "/" + source.name
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(backup.regular_bytes(source))
    from satip_controller.state import atomic_json
    config = destination_config(device)
    if read_json(stage / "installation.json").get("state") == "unconfigured":
        atomic_json(stage / "config/web/config.json", config)
        atomic_json(stage / "web/setup-complete.json", {
            "network_uuid": config["network_uuid"], "playback_acl_uuid": config["playback_acl_uuid"]})
    # Source switching must not resurrect the pre-restore lineup.
    from .tv_source import profile_path, validate_profile
    source = "enigma2" if metadata(device).get("input_type") == "iptv" else "satip"
    profile = profile_path(stage, source)
    if profile.exists():
        node = validate_profile(read_json(profile))
        if node["network_uuid"] != config["network_uuid"]:
            raise backup.BackupError("Saved TV source does not match the active channel network")
        atomic_json(profile, {**node, "library": read_json(stage / "management/library.json"),
                              "candidates": read_json(stage / "web/candidates.json")})
    path = stage / "tvheadend/accesscontrol" / config["playback_acl_uuid"]
    acl = read_json(path)
    if summary["channel_max"] != acl["channel_max"]:
        acl["channel_max"] = summary["channel_max"]
        atomic_json(path, acl)
