"""Root-only fresh-store provisioning; fixed destinations, no web-supplied paths."""
import base64
import os
from pathlib import Path
import runpy
import secrets
import subprocess

from . import persistent
from satip_controller.config import Config, credentials, library, uuid
from satip_controller.state import atomic_json, read_json

REVISION = "37453bc3fe5f9e10f3428ebb1abdc613f8b07186"
DEFAULTS = {"timezone": "Etc/UTC", "lan_access": "login-free",
            "drm_connector": "HDMI-A-2", "drm_mode": "1920x1080@50",
            "audio_device": "alsa/hdmi:CARD=vc4hdmi1,DEV=0",
            "keyboard_vendor": "04d9", "keyboard_product": "0006",
            "ir_profile": "apple-a1294", "satellite": "Astra-19.2E",
            "satip_source": "1"}


def parser(root=Path("/")):
    return runpy.run_path(str(root / "usr/sbin/satip-dev-provision"))


def owned_json(path, value, uid, gid, mode=0o600):
    if path.is_symlink():
        raise ValueError("Linked provisioning file refused")
    atomic_json(path, value, mode)
    os.chown(path, uid, gid)


def player_config(settings):
    return {"version": 1, "library": "/var/lib/satip-management/library.json",
            "credentials": "/etc/satip-player/credentials.json",
            "state_dir": "/var/lib/satip-player", "management_dir": "/var/lib/satip-management",
            "runtime_dir": "/run/satip-player", "drm_device": "/dev/dri/by-path/platform-gpu-card",
            **{key: settings[key] for key in ("drm_connector", "drm_mode", "audio_device")},
            "keyboard_vendor": int(settings["keyboard_vendor"], 16),
            "keyboard_product": int(settings["keyboard_product"], 16),
            "grab": True, "osd_ms": 3000, "repeat_delay": .5, "repeat_interval": .125,
            "debounce": .08, "load_timeout": 20, "stall_timeout": 10}


def web_config(network, acl, settings, hosts):
    uuid(network)
    uuid(acl)
    return {"version": 1, "listen_host": "0.0.0.0", "listen_port": 8080,
            "allowed_hosts": hosts, "require_auth": settings["lan_access"] != "login-free",
            "library": "/var/lib/satip-management/library.json",
            "management_dir": "/var/lib/satip-management", "player_state_dir": "/var/lib/satip-player",
            "private_dir": "/var/lib/satip-web", "admin_credentials": "/etc/satip-web/tvheadend-admin.json",
            "web_credentials": "/etc/satip-web/login.json",
            "preview_credentials": "/etc/satip-web/preview-credentials.json",
            "preview_runtime_dir": "/run/satip-web", "network_uuid": network,
            "playback_acl_uuid": acl, "mux_timeout": 90}


def seed(root, data, values):
    """Credential seed is committed first; interrupted installation reuses it.

    Called before any backend writer. An unexpected backend tree is never erased.
    """
    store = data / "store"
    state = read_json(store / "installation.json").get("state")
    if state == "configured":
        apply_devices(root, read_json(store / "identity/factory.json")["settings"])
        return
    if state != "unconfigured":
        return
    persistent.reject_links(store)
    identity = store / "identity/factory.json"
    if not identity.exists():
        if any((store / "tvheadend").iterdir()):
            raise ValueError("Fresh setup requires an empty backend")
        # Only timezone is a public factory preference. Source selection and
        # access preferences belong to the browser; output/input use defaults.
        settings = {**DEFAULTS, **{key: value for key, value in values.items() if key == "timezone"}}
        timezone = root / "usr/share/zoneinfo" / settings["timezone"]
        if not timezone.is_file() or not timezone.resolve().is_relative_to((root / "usr/share/zoneinfo").resolve()):
            raise ValueError("Timezone is not installed")
        factory = {"settings": settings,
                   "admin": {"username": "satip-admin", "password": secrets.token_urlsafe(32)},
                   "playback": {"username": "satip-playback", "password": secrets.token_urlsafe(32)},
                   "login": {"username": "satip", "password": secrets.token_urlsafe(32)},
                   "ids": {role: secrets.token_hex(16) for role in ("admin_acl", "playback_acl", "admin_password", "playback_password", "pass")}}
        owned_json(identity, factory, 0, 0)
    factory = read_json(identity)
    settings, ids = factory["settings"], factory["ids"]
    # Only fixed credential destinations; no wholesale /etc persistence.
    for path, value, uid in (("config/player/credentials.json", factory["playback"], 421),
                             ("config/web/preview-credentials.json", factory["playback"], 422),
                             ("config/web/tvheadend-admin.json", factory["admin"], 422),
                             ("config/web/login.json", factory["login"], 422)):
        owned_json(store / path, value, uid, uid)
    owned_json(store / "config/player/config.json", player_config(settings), 421, 421)
    owned_json(store / "config/web/setup.json", {"settings": settings, "playback_acl_uuid": ids["playback_acl"]}, 422, 422)
    if not (store / "factory-ready.json").exists():
        # Tvheadend treats a missing full_version as an older database and tries
        # to run a migration backup even for our newly seeded current schema.
        version = "0.0.0~unknown"
        if root == Path("/"):
            version = subprocess.check_output(["/usr/bin/tvheadend", "--version"], text=True, timeout=5).strip().rsplit("version ", 1)[-1]
            if not version or len(version) > 128 or any(ord(c) < 33 or ord(c) > 126 for c in version):
                raise ValueError("Invalid installed backend version")
        # Retry only our exact files; unexpected private backend state is refused.
        records = {"config": {"version": 24, "full_version": version, "digest": 2, "wizard": "", "server_name": "SAT-IP",
                              "muxconfpath": "/usr/share/tvheadend/data/dvb-scan"},
                   "profile/" + ids["pass"]: {"class": "profile-mpegts", "name": "pass", "enabled": True}}
        for role in ("admin", "playback"):
            account = factory[role]
            records["passwd/" + ids[role + "_password"]] = {"enabled": True, "username": account["username"],
                "password2": base64.b64encode(("TVHeadend-Hide-" + account["password"]).encode()).decode()}
            records["accesscontrol/" + ids[role + "_acl"]] = {
                "enabled": True, "username": account["username"], "prefix": "127.0.0.1/32",
                "webui": role == "admin", "admin": role == "admin", "streaming": ["basic"] if role == "playback" else [],
                "dvr": [], "change": ["change_rights", "change_chrange", "change_profiles", "change_conn_limit"],
                "profile": [ids["pass"]], "conn_limit": 1 if role == "playback" else 0,
                # Administration must see newly mapped channels/EPG beyond 1–3.
                # Only playback is restricted to the initial streaming range.
                "channel_min": 1000000 if role == "playback" else 0,
                "channel_max": 3000000 if role == "playback" else 0}
        backend = store / "tvheadend"
        for path in backend.rglob("*"):
            if path.is_file() and path.relative_to(backend).as_posix() not in records:
                raise ValueError("Unexpected backend state before bootstrap completion")
        for relative, node in records.items():
            path = backend / relative
            path.parent.mkdir(exist_ok=True, mode=0o700)
            os.chown(path.parent, 424, 424)
            owned_json(path, node, 424, 424)
        owned_json(store / "factory-ready.json", {"backend_revision": REVISION, "fresh": True}, 0, 0)
    apply_devices(root, settings)


def apply_devices(root, settings):
    from .device_settings import overrides
    settings = {**settings, **{key: value for key, value in overrides(root).items() if key == "timezone"}}
    writer = parser(root)["atomic_write"]
    timezone = root / "usr/share/zoneinfo" / settings["timezone"]
    if not timezone.is_file() or not timezone.resolve().is_relative_to((root / "usr/share/zoneinfo").resolve()):
        raise ValueError("Timezone is not installed")
    # Buildroot may install /etc/localtime as a symlink: replace this exact slot
    # file, never follow it and overwrite packaged timezone data.
    destination = root / "etc/localtime"
    if destination.is_symlink():
        destination.unlink()
    writer(root, "etc/localtime", timezone.read_bytes(), 0o644)
    writer(root, "etc/timezone", settings["timezone"] + "\n", 0o644)
    rule = (
        'SUBSYSTEM=="input", KERNEL=="event*", ATTRS{id/bustype}=="0003", '
        'ENV{ID_INPUT_KEYBOARD}=="1", GROUP="satip-input", MODE="0660"\n'
        'SUBSYSTEM=="input", KERNEL=="event*", ATTRS{id/bustype}=="0003", '
        f'ATTRS{{id/vendor}}=="{settings["keyboard_vendor"].lower()}", ATTRS{{id/product}}=="{settings["keyboard_product"].lower()}", GROUP="satip-input", MODE="0660"\n')
    # Infrared and USB keyboards always coexist. Legacy ir_profile=disabled
    # remains parseable for old cards but no longer removes receiver access.
    rule += 'SUBSYSTEM=="input", KERNEL=="event*", DRIVERS=="gpio_ir_recv", GROUP="satip-input", MODE="0660"\n'
    writer(root, "etc/udev/rules.d/99-satip-input.rules", rule, 0o644)


def boot(root=Path("/"), data=Path("/data")):
    helper = parser(root)
    stage = "data-mount"
    def step(value):
        nonlocal stage
        stage = value
        helper["boot_stage"](root, value)
    try:
        step("data-mount")
        persistent.validate_mount(data)
        from .backup import recover_restore
        recover_restore(data)
        step("config-read")
        values = helper["read_config"](root / "boot/satip-config.txt")
        if not (data / "store").exists():
            step("data-init")
            marker = data / ".satip-factory-empty"
            fresh = (not marker.is_symlink() and marker.is_file()
                     and marker.read_bytes() == b"satip-fresh-data-v1\n")
            if not fresh:
                raise ValueError("Factory-empty data marker required")
            step("data-prepare")
            allowed = {"lost+found", ".satip-factory-empty", ".fresh-stage"}
            if any(p.name not in allowed for p in data.iterdir()):
                raise ValueError("Existing data prevents fresh initialization")
            persistent.publish(data, fresh=fresh)
        (data / ".satip-factory-empty").unlink(missing_ok=True)
        step("data-prepare")
        persistent.prepare(root, data)
        step("backend-init")
        seed(root, data, values)
        if root == Path("/") and (data / "store/identity/factory.json").is_file():
            step("devices")
            # Devices were already enumerated by eudev; reapply the narrow rules.
            for command in (["/usr/bin/udevadm", "control", "--reload-rules"],
                            ["/usr/bin/udevadm", "trigger", "--subsystem-match=input", "--action=change"],
                            ["/usr/bin/udevadm", "settle", "--timeout=3"]):
                subprocess.run(command, check=True, timeout=5)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        helper["boot_failure"](root, error, stage)
        raise


def activate(data=Path("/data")):
    """Root publishes only the installation marker, never request-chosen paths.

    Fresh configured state is not imported-database or physical qualification.
    """
    store = data / "store"
    if read_json(store / "installation.json") != {"state": "unconfigured"}:
        return
    request = store / "web/setup-complete.json"
    if not request.exists():
        return
    persistent.reject_links(store)
    node = read_json(request)
    if set(node) != {"network_uuid", "playback_acl_uuid"} or (store / "web/mux-rollback.json").exists():
        raise ValueError("Invalid setup completion or pending scan recovery")
    if any((store / "web/enigma2-import" / name).exists() for name in
           ("mux-rollback.json", "network-rollback.json", "publish-rollback.json")):
        raise ValueError("Recover the interrupted Enigma2 import first")
    factory = read_json(store / "identity/factory.json")
    if node["playback_acl_uuid"] != factory["ids"]["playback_acl"]:
        raise ValueError("Unexpected playback identity")
    uuid(node["network_uuid"])
    Config.load(store / "config/player/config.json")
    if read_json(store / "config/player/config.json") != player_config(factory["settings"]):
        raise ValueError("Unexpected generated player configuration")
    library(read_json(store / "management/library.json"))
    for path in ("config/player/credentials.json", "config/web/tvheadend-admin.json", "config/web/login.json", "config/web/preview-credentials.json"):
        credentials(store / path)
    config = read_json(store / "config/web/config.json")
    if config != web_config(node["network_uuid"], node["playback_acl_uuid"], factory["settings"], config.get("allowed_hosts")):
        raise ValueError("Unexpected generated management configuration")
    from .setup import verify_access
    from satip_controller.backend import Backend
    import asyncio
    backend = Backend(credentials(store / "config/web/tvheadend-admin.json"))
    asyncio.run(verify_access(backend, node["playback_acl_uuid"]))
    networks = backend._request("mpegts/network/grid", {"limit": 10000}, False).get("entries", [])
    if not any(n["uuid"] == node["network_uuid"] for n in networks):
        raise ValueError("Configured network is missing")
    from .tv_source import profile_path, validate_profile
    profile = profile_path(store, "enigma2")
    if profile.exists():
        source = validate_profile(read_json(profile))
        if source["network_uuid"] == node["network_uuid"] and source["library"] != read_json(store / "management/library.json"):
            raise ValueError("Enigma2 setup lineup does not match its profile")
    owned_json(store / "installation.json", {"state": "configured", "origin": "fresh", "hardware_qualified": False}, 0, 0, 0o644)


def main():
    os.umask(0o077)
    try:
        boot()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        Path("/run/satip-data-ready").unlink(missing_ok=True)
        raise SystemExit("Fresh provisioning failed; TV services remain disabled") from None


if __name__ == "__main__":
    main()
