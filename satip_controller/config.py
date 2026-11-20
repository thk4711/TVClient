import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path

UUID = re.compile(r"^[0-9a-f]{32}$")


def uuid(value):
    if not isinstance(value, str) or not UUID.fullmatch(value):
        raise ValueError("Invalid channel/service UUID")
    return value


def credentials(path: Path):
    if path.stat().st_mode & (stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH | stat.S_IWGRP):
        raise ValueError("Credential file permissions are not private")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or set(data) != {"username", "password"} or not all(
            isinstance(v, str) and v and not any(c in v for c in "\r\n\0") for v in data.values()):
        raise ValueError("Expected separate username/password credentials only")
    return data


@dataclass(frozen=True)
class Channel:
    uuid: str
    name: str
    service_uuid: str
    mux_uuid: str
    video: str
    audio_pid: int

    @classmethod
    def parse(cls, data):
        if set(data) != {"uuid", "name", "service_uuid", "mux_uuid", "video", "audio_pid"}:
            raise ValueError("Invalid channel fields")
        for field in ("uuid", "service_uuid", "mux_uuid"):
            uuid(data[field])
        name = data["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > 150 or any(ord(c) < 32 for c in name):
            raise ValueError("Invalid channel name")
        if data["video"] not in ("h264", "mpeg2"):
            raise ValueError("Unsupported video policy")
        if type(data["audio_pid"]) is not int or not 1 <= data["audio_pid"] <= 8191:
            raise ValueError("Invalid main audio PID")
        return cls(**data)


def library(data):
    if not isinstance(data, dict) or set(data) != {"version", "channels", "favourites"} or type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("Invalid library version/fields")
    parsed = [Channel.parse(c) for c in data["channels"]]
    channels = {c.uuid: c for c in parsed}
    if not channels or len(channels) != len(parsed):
        raise ValueError("Empty or duplicate channel catalogue")
    favourites = data["favourites"]
    if (not isinstance(favourites, list) or not favourites or len(favourites) > 500
            or any(not isinstance(c, str) for c in favourites)
            or len(set(favourites)) != len(favourites) or any(c not in channels for c in favourites)):
        raise ValueError("Invalid/empty favourites")
    return channels, favourites


@dataclass(frozen=True)
class Config:
    library: Path
    credentials: Path
    state_dir: Path
    management_dir: Path
    runtime_dir: Path
    drm_device: str
    drm_connector: str
    drm_mode: str
    audio_device: str
    keyboard_vendor: int
    keyboard_product: int
    grab: bool
    osd_ms: int
    repeat_delay: float
    repeat_interval: float
    debounce: float
    load_timeout: float
    stall_timeout: float

    @classmethod
    def load(cls, path):
        d = json.loads(Path(path).read_text())
        if not isinstance(d, dict) or type(d.get("version")) is not int or d.pop("version", None) != 1 or set(d) != set(cls.__dataclass_fields__):
            raise ValueError("Invalid controller config version/fields")
        for name in ("library", "credentials", "state_dir", "management_dir", "runtime_dir"):
            if not isinstance(d[name], str) or not Path(d[name]).is_absolute():
                raise ValueError("Configuration paths must be absolute")
            d[name] = Path(d[name])
        for name in ("drm_device", "drm_connector", "drm_mode", "audio_device"):
            if not isinstance(d[name], str) or not d[name] or any(c in d[name] for c in "\r\n\0"):
                raise ValueError("Invalid device setting")
        for name in ("keyboard_vendor", "keyboard_product"):
            if type(d[name]) is not int or not 0 <= d[name] <= 65535:
                raise ValueError("Invalid keyboard selector")
        if type(d["grab"]) is not bool or type(d["osd_ms"]) is not int or not 500 <= d["osd_ms"] <= 10000:
            raise ValueError("Invalid input/OSD setting")
        for name in ("repeat_delay", "repeat_interval", "debounce", "load_timeout", "stall_timeout"):
            if type(d[name]) not in (int, float) or not 0.01 <= d[name] <= 120:
                raise ValueError("Invalid timing")
        return cls(**d)
