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


def metadata(device):
    from . import persistent
    if (not persistent.schema_valid(read_json(device.store / "schema.json"))
            or read_json(device.store / "installation.json").get("state") not in ("configured", "qualified")
            or read_json(device.store / "factory-ready.json").get("backend_revision") != backup.REVISION):
        raise ValueError("Incompatible receiver installation")
    config = read_json(device.store / "config/web/config.json")
    result = {"version": 1, "network_uuid": uuid(config["network_uuid"])}
    if (device.store / "tvheadend/input/iptv/networks" / result["network_uuid"] / "config").is_file():
        result["input_type"] = "iptv"
    return result


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
            raise ValueError("Channel data exceeds backup limits")
    return files


def validate(stage, device):
    if read_json(stage / "channels.json") != metadata(device):
        raise ValueError("Channel backup belongs to a different channel network")
    channels, favourites = library(read_json(stage / "management/library.json"))
    if len(channels) > 10000:
        raise ValueError("Channel library exceeds limits")
    catalogue = read_json(stage / "web/candidates.json")
    if not isinstance(catalogue, dict) or set(catalogue) != {"channels"} or not isinstance(catalogue["channels"], list):
        raise ValueError("Invalid channel catalogue")
    if len(catalogue["channels"]) > 10000:
        raise ValueError("Channel catalogue exceeds limits")
    items = {}
    for item in catalogue["channels"]:
        if not isinstance(item, dict) or set(item) not in (
                {"name", "service_uuid", "mux_uuid", "video", "audio_pid"},
                {"name", "service_uuid", "mux_uuid", "video", "audio_pid", "encrypted"},
                set(Channel.__dataclass_fields__)):
            raise ValueError("Invalid available channel fields")
        if "encrypted" in item and item["encrypted"] is not False:
            raise ValueError("Encrypted channel refused")
        parsed = Channel.parse({**{k: v for k, v in item.items() if k != "encrypted"},
                                "uuid": item.get("uuid", item["service_uuid"])})
        if parsed.service_uuid in items:
            raise ValueError("Duplicate available channel")
        items[parsed.service_uuid] = parsed
    network = network_path(metadata(device))
    services = {}
    expanded = 0
    for path in stage.rglob("*"):
        if not path.is_file():
            continue
        name = path.relative_to(stage).as_posix()
        if not allowed(name):
            raise ValueError("Non-channel data refused")
        if name.startswith("tvheadend/input/"):
            if not name.startswith(network):
                raise ValueError("Foreign channel network refused")
            content = backup.regular_bytes(path)
            expanded += int.from_bytes(content[8:12], "big") if content.startswith(b"\xff\xffGZIP0") else len(content)
            if expanded > 64 * 1024 * 1024:
                raise ValueError("Expanded channel data exceeds limits")
            record = backup.backend_record(content)
            if "/input/iptv/" in name:
                from .tv_source import validate_iptv_record
                validate_iptv_record(name, record, device.store)
            if not isinstance(record, dict) or set(record) != {"config"} or not isinstance(record["config"], dict):
                raise ValueError("Invalid multiplex record")
            config = record["config"]
            def hooks(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if (key.lower() in {"command", "spawn", "script"} or
                                key.lower().endswith(("_command", "_spawn", "_script"))) and child:
                            raise ValueError("External hooks refused")
                        hooks(child)
                elif isinstance(value, list):
                    for child in value:
                        hooks(child)
            hooks(config)
            service_records = config.get("services", {})
            if not isinstance(service_records, dict):
                raise ValueError("Invalid services")
            services[path.name] = {uuid(key): value for key, value in service_records.items()}
    for item in [*channels.values(), *items.values()]:
        service = services.get(item.mux_uuid, {}).get(item.service_uuid)
        if not isinstance(service, dict) or service.get("enabled") not in (True, 1):
            raise ValueError("Missing enabled channel service")
    config = read_json(device.store / "config/web/config.json")
    acl = read_json(device.store / "tvheadend/accesscontrol" / config["playback_acl_uuid"])
    old_channels, _ = library(read_json(device.store / "management/library.json"))
    maximum = acl["channel_max"]
    expected = {"tvheadend/channel/config/" + key for key in channels}
    actual = {p.relative_to(stage).as_posix() for p in (stage / "tvheadend/channel/config").glob("*")}
    if actual != expected:
        raise ValueError("Unexpected backend channels")
    if acl.get("admin") or acl.get("webui") or acl.get("conn_limit") != 1:
        raise ValueError("Invalid playback access restrictions")
    for key, item in channels.items():
        record = read_json(stage / "tvheadend/channel/config" / key)
        if (not isinstance(record, dict) or set(record) - {
                "name", "enabled", "services", "number", "epgauto", "epglimit", "epg_running",
                "dvr_pre_time", "dvr_pst_time", "bouquet", "tags"}
                or record.get("services") != [item.service_uuid] or record.get("enabled") is not True
                or type(record.get("number")) is not int or not acl["channel_min"] <= record["number"] <= 2147483647
                or record.get("bouquet") or record.get("tags")):
            raise ValueError("Invalid backend channel mapping")
        if key not in old_channels and (device.store / "tvheadend/channel/config" / key).exists():
            raise ValueError("Channel UUID collision")
        maximum = max(maximum, record["number"])
    if maximum > acl["channel_max"]:
        for path in (device.store / "tvheadend/channel/config").glob("*"):
            record = backup.backend_record(backup.regular_bytes(path))
            if path.name not in old_channels and acl["channel_max"] < record.get("number", 0) <= maximum:
                raise ValueError("Unrelated channel would gain playback access")
    return {"available": len(set(items) | {c.service_uuid for c in channels.values()}),
            "favourites": len(favourites), "channel_max": maximum}


def overlay(stage, validated, device):
    """Replace only fixed channel paths in a copy of the current installation."""
    summary = validate(validated, device)
    old_channels, _ = library(read_json(stage / "management/library.json"))
    network = network_path(metadata(device)).rstrip("/")
    for path in [*(stage / network).iterdir(),
                 *(stage / "tvheadend/channel/config" / key for key in old_channels)]:
        path.unlink()
    for source in validated.rglob("*"):
        if not source.is_file() or source.name == "channels.json":
            continue
        name = source.relative_to(validated).as_posix()
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(backup.regular_bytes(source))
    config = read_json(stage / "config/web/config.json")
    path = stage / "tvheadend/accesscontrol" / config["playback_acl_uuid"]
    acl = read_json(path)
    if summary["channel_max"] != acl["channel_max"]:
        from satip_controller.state import atomic_json
        acl["channel_max"] = summary["channel_max"]
        atomic_json(path, acl)
