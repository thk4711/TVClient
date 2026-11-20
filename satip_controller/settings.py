"""Web-managed OSD preferences, stored on persistent shared management data."""
from pathlib import Path

from .state import read_json

DEFAULT_TIMEOUT_MS = 5000
DEFAULT_LANGUAGE = "en"


def settings_path(directory):
    # Deliberately not runtime_path: preferences must survive a reboot.
    return Path(directory) / "settings.json"


def validate_settings(data):
    if (not isinstance(data, dict) or set(data) not in (
            {"version", "osd_timeout_ms"}, {"version", "osd_timeout_ms", "osd_language"})
            or type(data["version"]) is not int or data["version"] != 1
            or type(data["osd_timeout_ms"]) is not int
            or not 500 <= data["osd_timeout_ms"] <= 30000
            or data.get("osd_language", DEFAULT_LANGUAGE) not in ("en", "de")):
        raise ValueError("Invalid OSD settings")
    return {**data, "osd_language": data.get("osd_language", DEFAULT_LANGUAGE)}


def load_settings(directory):
    try:
        return validate_settings(read_json(settings_path(directory)))
    except FileNotFoundError:
        return {"version": 1, "osd_timeout_ms": DEFAULT_TIMEOUT_MS, "osd_language": DEFAULT_LANGUAGE}
