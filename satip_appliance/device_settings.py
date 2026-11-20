"""Root-owned settings shared by boot provisioning and the device helper."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from satip_controller.state import atomic_json, read_json


def atomic_text(path, text, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Linked settings file refused")
    fd, name = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def read_optional(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Linked settings file refused")
    try:
        if path.stat().st_size > 16384:
            raise ValueError("Settings file too large")
        return read_json(path)
    except FileNotFoundError:
        return {}


def overrides(root):
    data = read_optional(Path(root) / "data/store/identity/device.json")
    if (not isinstance(data, dict) or set(data) - {"timezone", "wifi_enabled", "boot_recovery"}
            or ("timezone" in data and not isinstance(data["timezone"], str))
            or ("wifi_enabled" in data and type(data["wifi_enabled"]) is not bool)
            or ("boot_recovery" in data and not isinstance(data["boot_recovery"], dict))):
        raise ValueError("Invalid device settings")
    return data


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


def recovery_values(root, current):
    """Ignore unchanged FAT seeds superseded by a web edit; new edits still recover."""
    ignored = overrides(root).get("boot_recovery", {})
    return {key: value for key, value in current.items()
            if ignored.get(key) != fingerprint(value)}


def save_override(root, **fields):
    data = overrides(root)
    data.update(fields)
    atomic_json(Path(root) / "data/store/identity/device.json", data, 0o600)


def suppress_boot_seeds(root, keys, parser):
    data = overrides(root)
    ignored = data.setdefault("boot_recovery", {})
    for key, value in parser["read_config"](Path(root) / "boot/satip-config.txt").items():
        if key in keys:
            ignored[key] = fingerprint(value)
    atomic_json(Path(root) / "data/store/identity/device.json", data, 0o600)


def saved_preferences(root, parser):
    path = Path(root) / "data/store/identity/development.txt"
    if path.is_symlink():
        raise ValueError("Linked recovery settings refused")
    if path.exists():
        return parser["parse_config"](path.read_text())
    hostname = (Path(root) / "etc/hostname").read_text().strip()
    return {"hostname": hostname}


def save_preferences(root, parser, values):
    text = "".join(f"{key}={value}\n" for key, value in values.items())
    parser["parse_config"](text)
    atomic_text(Path(root) / "data/store/identity/development.txt", text)


def wifi_config(values):
    psk = values["wifi_psk"]
    encoded = psk if len(psk) == 64 else json.dumps(psk, ensure_ascii=True)
    ssid = json.dumps(values['wifi_ssid']) if values['wifi_ssid'].isascii() else values['wifi_ssid'].encode().hex()
    return (f"country={values['wifi_country']}\nctrl_interface=/run/wpa_supplicant\n"
            f"network={{\n    ssid={ssid}\n"
            f"    psk={encoded}\n    scan_ssid=1\n    key_mgmt=WPA-PSK\n}}\n")
