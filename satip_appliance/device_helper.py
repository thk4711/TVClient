"""Fixed device actions over a peer-checked Unix socket, with durable Wi-Fi trials."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import socket
import struct
import subprocess
import time

from satip_controller.config import credentials
from satip_controller.state import atomic_json, boot_id, lease_active
from . import device_settings as settings
from . import persistent
from .timeshift_storage import Storage, StorageError
from .provision import parser

LIMIT = 16384
TRIAL_SECONDS = 90


class DeviceError(ValueError):
    """An explicitly secret-free message suitable for the settings UI."""


def fields(args, required):
    if not isinstance(args, dict) or set(args) != set(required):
        raise DeviceError("Invalid device settings fields")


def password(value):
    if (not isinstance(value, str) or not value or len(value) > 128
            or any(ord(c) < 32 or ord(c) > 126 for c in value)):
        raise DeviceError("Use 1–128 printable characters for the password")
    return value


class Device:
    def __init__(self, root=Path("/"), run=subprocess.run, clock=time.monotonic):
        self.root = Path(root)
        self.run, self.clock = run, clock
        self.parser = parser(self.root)
        self.store = self.root / "data/store"
        self.trial_path = self.store / "network/wifi-trial.json"
        self.work = None
        self.last_result = ""
        self.storage = Storage(self.root, self.command)
        from .backup import Backups
        self.backups = Backups(self)

    def command(self, argv, timeout=10, **kwargs):
        return self.run(argv, capture_output=True, text=True, timeout=timeout, check=True, **kwargs)

    def validate(self):
        if self.root == Path("/"):
            if not Path("/run/satip-data-ready").is_file():
                raise DeviceError("Persistent storage is not ready")
            persistent.validate_mount(Path("/data"), probe=False)

    def trial(self):
        return settings.read_optional(self.trial_path)

    def timezone(self, name):
        base = self.root / "usr/share/zoneinfo"
        if (not isinstance(name, str) or not name or name.startswith("/")
                or ".." in name.split("/")):
            raise DeviceError("Choose an installed timezone")
        path = base / name
        if not path.is_file() or not path.resolve().is_relative_to(base.resolve()) or path.read_bytes()[:4] != b"TZif":
            raise DeviceError("Choose an installed timezone")
        return path

    def boot_prepare(self):
        # A trial never survives reboot or helper failure as an accepted change.
        if self.trial():
            self.revert(restart=self.root == Path("/") and Path("/run/satip-wpa.pid").exists())
        enabled = settings.overrides(self.root).get("wifi_enabled", True)
        settings.atomic_text(self.root / "etc/satip-wifi-enabled", "yes\n" if enabled else "no\n", 0o644)
        settings.atomic_text(self.root / "run/satip-device-ready", "ready\n", 0o644)

    def restart_wifi(self):
        self.command(["/etc/init.d/S40network", "restart-wifi"], timeout=15)

    def revert(self, restart=True):
        trial = self.trial()
        if not trial:
            return
        settings.save_preferences(self.root, self.parser, trial["previous_preferences"])
        path = self.root / "etc/wpa_supplicant.conf"
        if trial["previous_config"] is None:
            path.unlink(missing_ok=True)
        else:
            settings.atomic_text(path, trial["previous_config"])
        if restart:
            self.restart_wifi()
        self.trial_path.unlink()
        self.work = None
        self.last_result = "Previous Wi-Fi settings restored."

    def tick(self):
        if self.backups.pending or self.backups.gate.exists():
            self.backups.tick()
            return
        self.backups.tick()
        try:
            self.storage.tick()
        except (OSError, ValueError, subprocess.SubprocessError):
            self.storage.ready.unlink(missing_ok=True)
            self.storage.message = "USB storage unavailable. Eject it or reconnect and retry."
        if not self.work and not self.trial():
            return
        self.validate()
        trial = self.trial()
        if trial and (trial["boot_id"] != boot_id() or self.clock() >= trial["deadline"]):
            self.revert()
        elif self.work and self.clock() >= self.work[0]:
            _, action = self.work
            self.work = None
            if action == "wifi":
                settings.atomic_text(self.root / "etc/wpa_supplicant.conf", settings.wifi_config(trial["candidate"]))
                # Remove only this interface's DHCP hint; a stale address cannot confirm a trial.
                (self.store / "network/wlan0.json").unlink(missing_ok=True)
                self.restart_wifi()
                atomic_json(self.trial_path, {**trial, "applied": True}, 0o600)
            elif action.startswith("tuner:"):
                try:
                    self.command(["/usr/bin/python3", "-m", "satip_appliance.tv_source",
                                  "tuner", action.split(":", 1)[1], "--apply"], timeout=90)
                    self.last_result = "SAT-IP tuner applied."
                except (OSError, ValueError, subprocess.SubprocessError):
                    self.last_result = ("SAT-IP tuner change failed. Reboot the receiver to retry interrupted recovery."
                                        if (self.store / "web/satip-tuner-transaction.json").exists() else
                                        "SAT-IP tuner change failed. Previous configuration restored; check the tuner and retry.")
            elif action.startswith("source:"):
                try:
                    self.command(["/usr/bin/python3", "-m", "satip_appliance.tv_source",
                                  "select", action.split(":", 1)[1], "--apply"], timeout=90)
                    self.last_result = "TV source selected."
                except (OSError, ValueError, subprocess.SubprocessError):
                    self.last_result = "TV source selection failed. Check the source and recover if necessary."
            elif action == "restart_player":
                self.command(["/usr/bin/python3", "-m", "satip_appliance.supervisor", "player", "stop"], timeout=20)
                self.command(["/usr/bin/python3", "-m", "satip_appliance.supervisor", "player", "start"], timeout=20)
            else:
                self.command(["/etc/init.d/S90satip-services", "stop"], timeout=90)
                self.command(["/sbin/reboot" if action == "reboot" else "/sbin/poweroff"])

    def wifi_link(self):
        try:
            text = self.command(["/usr/sbin/iw", "dev", "wlan0", "link"], timeout=3).stdout
            match = re.search(r"^\s*SSID: (.*)$", text, re.M)
            from .setup_device import decode_ssid
            return decode_ssid(match.group(1)) if match else ""
        except (OSError, ValueError, subprocess.SubprocessError):
            return ""

    def has_address(self, interface):
        text = self.command(["/sbin/ip", "-o", "-4", "addr", "show", "dev", interface], timeout=3).stdout
        return bool(re.search(r"inet (?!169\.254\.|127\.)", text))

    def status(self):
        from .tv_source import public_status
        values = settings.saved_preferences(self.root, self.parser)
        trial = self.trial()
        zones = []
        base = self.root / "usr/share/zoneinfo"
        # zone.tab is compact and avoids hundreds of duplicate aliases.
        for line in (base / "zone.tab").read_text().splitlines():
            if line and not line.startswith("#"):
                zones.append(line.split()[2])
        current_zone = (self.root / "etc/timezone").read_text().strip()
        return {"hostname": values.get("hostname", "satip-dev"),
                "running_hostname": socket.gethostname(), "timezone": current_zone,
                "timezones": sorted(set(zones + ["Etc/UTC", current_zone])),
                "ssh_enabled": (self.root / "etc/satip-ssh-enabled").read_text().strip() == "yes",
                "wifi_enabled": settings.overrides(self.root).get("wifi_enabled", True),
                "wifi_ssid": values.get("wifi_ssid", ""), "wifi_country": values.get("wifi_country", "DE"),
                "connected_ssid": self.wifi_link(),
                "trial": {"id": trial["id"], "seconds_left": max(0, int(trial["deadline"] - self.clock())),
                          "applied": trial.get("applied", False),
                          "ssid": trial["candidate"]["wifi_ssid"]} if trial else None,
                "tv_source": public_status(self.store),
                "message": self.last_result}

    def handle(self, action, args):
        self.validate()
        if (self.root / "run/satip-source-maintenance.json").exists():
            raise DeviceError("TV source maintenance is running")
        if action.startswith("backup_"):
            try:
                return self.backups.handle(action, args)
            except ValueError as error:
                raise DeviceError("Invalid backup or pending operation. Check compatibility and finish other changes.") from error
        if self.backups.pending or self.backups.gate.exists():
            raise DeviceError("Configuration maintenance is running")
        if action == "status":
            fields(args, ())
            return self.status()
        if (self.work and action != "wifi_revert") or (self.trial() and action not in ("wifi_confirm", "wifi_revert")):
            raise DeviceError("Finish the pending device change first")
        if action == "satip_tuner":
            fields(args, ("frontend",))
            from satip_controller.config import uuid
            from .tv_source import public_status
            uuid(args["frontend"])
            if public_status(self.store)["active"] != "satip":
                raise DeviceError("Activate SAT-IP before changing its tuner")
            self.backups.quiet()
            self.work = (self.clock() + 2, "tuner:" + args["frontend"])
            self.last_result = "Applying SAT-IP tuner; playback and this page restart shortly."
            return {"message": self.last_result}
        if action == "source_select":
            fields(args, ("source",))
            from .tv_source import public_status
            if args["source"] not in public_status(self.store)["available"]:
                raise DeviceError("Import the requested TV source first")
            self.backups.quiet()
            self.work = (self.clock() + 2, "source:" + args["source"])
            self.last_result = "Switching TV source; playback and the web service restart shortly."
            return {"message": self.last_result}
        if action in ("wifi", "wifi_enabled", "restart_player", "reboot", "shutdown",
                      "storage_format", "storage_mount", "storage_limit", "storage_eject", "storage_forget"):
            recovery = settings.read_optional(self.store / "management/recovery.json")
            if (self.store / "web/mux-rollback.json").exists() or recovery.get("pending"):
                raise DeviceError("Finish channel search or recovery first")
            if self.root == Path("/"):
                for name in ("scan-lease.json", "preview-lease.json"):
                    if lease_active(settings.read_optional(Path("/run/satip-management") / name)):
                        raise DeviceError("Stop Live TV or channel search first")
        if action.startswith("storage_"):
            try:
                return self.storage.handle(action, args)
            except StorageError as error:
                raise DeviceError(str(error)) from None
        values = settings.saved_preferences(self.root, self.parser)
        if action.startswith("setup_"):
            from .setup_device import handle
            return handle(self, action, args)
        if action == "device":
            fields(args, ("hostname", "timezone"))
            if not isinstance(args["hostname"], str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", args["hostname"]):
                raise DeviceError("Enter a valid hostname")
            self.parser["parse_config"]("hostname=" + args["hostname"])
            zone = self.timezone(args["timezone"])
            values["hostname"] = args["hostname"]
            settings.save_preferences(self.root, self.parser, values)
            settings.save_override(self.root, timezone=args["timezone"])
            localtime = self.root / "etc/localtime"
            if localtime.is_symlink():
                localtime.unlink()
            self.parser["atomic_write"](self.root, "etc/localtime", zone.read_bytes(), 0o644)
            settings.atomic_text(self.root / "etc/timezone", args["timezone"] + "\n", 0o644)
            # Running Python services refresh tzset during their normal polling.
            return {"message": "Saved. Reboot to apply the hostname." if args["hostname"] != socket.gethostname() else "Saved."}
        if action in ("web_password", "root_password"):
            fields(args, ("password", "confirm"))
            new = password(args["password"])
            if new != args["confirm"]:
                raise DeviceError("Passwords do not match")
            if action == "root_password":
                hashed = self.command(["/bin/busybox", "mkpasswd", "-m", "sha512", "-P", "0"], input=new + "\n").stdout.strip()
                self.parser["parse_config"]("recovery_password_hash=" + hashed)
                values.pop("root_password", None)
                values["recovery_password_hash"] = hashed
                # Reuse the reviewed shadow writer, without reapplying hostname/Wi-Fi.
                path = self.root / "etc/shadow"
                if path.is_symlink():
                    raise DeviceError("Linked root account file refused")
                lines = path.read_text().splitlines()
                if sum(line.startswith("root:") for line in lines) != 1:
                    raise DeviceError("Root account is unavailable")
                settings.suppress_boot_seeds(self.root, ("root_password", "recovery_password_hash"), self.parser)
                settings.save_preferences(self.root, self.parser, values)
                lines = ["root:" + hashed + ":" + line.split(":", 2)[2] if line.startswith("root:") else line for line in lines]
                settings.atomic_text(path, "\n".join(lines) + "\n")
                access = self.store / "identity/root-access.json"
                if access.exists():
                    atomic_json(access, {"version": 1, "hash": hashed})
            else:
                path = self.store / "config/web/login.json"
                login = credentials(path)
                login["password"] = new
                factory_path = self.store / "identity/factory.json"
                factory = settings.read_optional(factory_path)
                if factory:
                    factory["login"] = login
                    atomic_json(factory_path, factory, 0o600)
                atomic_json(path, login, 0o600)
                os.chown(path, 422, 422)
                if factory.get("settings", {}).get("lan_access") == "login-free":
                    factory["settings"]["lan_access"] = "authenticated"
                    atomic_json(factory_path, factory, 0o600)
                    config_path = self.store / "config/web/config.json"
                    if config_path.exists():
                        config = settings.read_optional(config_path)
                        config["require_auth"] = True
                        atomic_json(config_path, config, 0o600)
                        os.chown(config_path, 422, 422)
            return {"message": "Password changed."}
        if action in ("ssh", "wifi_enabled"):
            fields(args, ("enabled",))
            if type(args["enabled"]) is not bool:
                raise DeviceError("Invalid access setting")
            enabled = args["enabled"]
            if action == "ssh":
                if enabled:
                    shadow = (self.root / "etc/shadow").read_text()
                    root_hash = next(line.split(":")[1] for line in shadow.splitlines() if line.startswith("root:"))
                    keys = self.root / "root/.ssh/authorized_keys"
                    if (not root_hash or root_hash.startswith(("!", "*"))) and not (keys.is_file() and keys.read_text().strip()):
                        raise DeviceError("Set a root password before enabling SSH")
                values["ssh_enabled"] = "yes" if enabled else "no"
                settings.suppress_boot_seeds(self.root, ("ssh_enabled",), self.parser)
                settings.save_preferences(self.root, self.parser, values)
                settings.atomic_text(self.root / "etc/satip-ssh-enabled", values["ssh_enabled"] + "\n", 0o644)
                self.command(["/etc/init.d/S50dropbear", "start" if enabled else "stop"])
            else:
                if not enabled and not self.has_address("eth0"):
                    raise DeviceError("Connect Ethernet before disabling Wi-Fi")
                settings.save_override(self.root, wifi_enabled=enabled)
                settings.atomic_text(self.root / "etc/satip-wifi-enabled", "yes\n" if enabled else "no\n", 0o644)
                self.restart_wifi()
            return {"message": "Saved."}
        if action == "wifi":
            fields(args, ("ssid", "password", "country"))
            if not settings.overrides(self.root).get("wifi_enabled", True):
                raise DeviceError("Enable Wi-Fi first")
            if any(not isinstance(v, str) for v in args.values()):
                raise DeviceError("Invalid Wi-Fi settings")
            if (not 1 <= len(args["ssid"].encode()) <= 32
                    or any(ord(c) < 32 or ord(c) == 127 for c in args["ssid"])
                    or any(ord(c) < 32 or ord(c) > 126 for c in args["password"])):
                raise DeviceError("Use a valid network name and printable Wi-Fi password")
            candidate = dict(values)
            for key, value in (("wifi_ssid", args["ssid"]), ("wifi_country", args["country"].upper())):
                candidate[key] = value
            candidate["wifi_psk"] = args["password"] or (values.get("wifi_psk", "") if args["ssid"] == values.get("wifi_ssid") else "")
            psk = candidate["wifi_psk"]
            if not (8 <= len(psk) <= 63 or re.fullmatch(r"[0-9a-fA-F]{64}", psk)):
                raise DeviceError("Wi-Fi password must have 8–63 characters or 64 hexadecimal digits")
            if not re.fullmatch(r"[A-Z]{2}", candidate["wifi_country"]):
                raise DeviceError("Enter a two-letter country code")
            self.parser["parse_config"]("".join(f"{k}={v}\n" for k, v in candidate.items()))
            config = self.root / "etc/wpa_supplicant.conf"
            identifier = secrets.token_hex(16)
            atomic_json(self.trial_path, {"id": identifier, "boot_id": boot_id(),
                       "deadline": self.clock() + TRIAL_SECONDS, "candidate": candidate,
                       "previous_preferences": values, "previous_config": config.read_text() if config.exists() else None}, 0o600)
            self.work = (self.clock() + 1, "wifi")
            return {"message": "Trying Wi-Fi settings. Confirm within 90 seconds.", "trial_id": identifier}
        if action in ("wifi_confirm", "wifi_revert"):
            fields(args, ("id",))
            trial = self.trial()
            if not trial or args["id"] != trial["id"]:
                raise DeviceError("Wi-Fi trial has ended. Reload settings.")
            if action == "wifi_revert":
                self.revert(restart=self.work is None)
            else:
                if (not trial.get("applied") or self.clock() >= trial["deadline"] or trial["boot_id"] != boot_id()
                        or self.wifi_link() != trial["candidate"]["wifi_ssid"] or not self.has_address("wlan0")):
                    raise DeviceError("The new Wi-Fi connection is not ready")
                settings.save_preferences(self.root, self.parser, trial["candidate"])
                self.trial_path.unlink()
                self.last_result = "Wi-Fi settings saved."
            return {"message": self.last_result}
        if action == "scan_wifi":
            fields(args, ())
            text = self.command(["/usr/sbin/iw", "dev", "wlan0", "scan"], timeout=8).stdout
            networks = {}
            signal = None
            for line in text.splitlines():
                match = re.search(r"signal: (-?[0-9.]+)", line)
                if match:
                    signal = float(match.group(1))
                elif line.strip().startswith("SSID: "):
                    ssid = line.strip()[6:]
                    if ssid and len(ssid.encode()) <= 32 and not any(ord(c) < 32 for c in ssid):
                        networks[ssid] = max(networks.get(ssid, -200), signal if signal is not None else -200)
            return {"networks": [{"ssid": k, "signal": v} for k, v in sorted(networks.items(), key=lambda item: -item[1])[:50]]}
        if action in ("restart_player", "reboot", "shutdown"):
            fields(args, ())
            self.work = (self.clock() + 2, action)
            return {"message": {"restart_player": "Restarting TV playback…", "reboot": "Rebooting…", "shutdown": "Shutting down…"}[action]}
        raise DeviceError("Unsupported device action")


async def serve():
    import asyncio
    device = Device()
    device.validate()
    directory = Path("/run/satip-device")
    directory.mkdir(mode=0o750, exist_ok=True)
    os.chown(directory, 0, 422)
    os.chmod(directory, 0o750)
    lock = (directory / "helper.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    device.backups.prepare()
    device.boot_prepare()
    path = directory / "control.sock"
    path.unlink(missing_ok=True)
    serial = asyncio.Lock()

    async def connection(reader, writer):
        try:
            peer = writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, uid, _ = struct.unpack("3i", peer)
            if uid not in (0, 422):
                return
            async with asyncio.timeout(5):
                raw = await reader.readline()
            if len(raw) > LIMIT:
                raise DeviceError("Device request too large")
            request = json.loads(raw)
            fields(request, ("action", "args"))
            async with serial:
                data = await asyncio.to_thread(device.handle, request["action"], request["args"])
            result = {"ok": True, "data": data}
        except DeviceError as error:
            result = {"ok": False, "message": str(error)}
        except (ValueError, KeyError, TypeError):
            # Never echo malformed input, passwords, subprocess output or hashes.
            result = {"ok": False, "message": "Invalid device settings or pending change"}
        except (OSError, subprocess.SubprocessError, TimeoutError):
            result = {"ok": False, "message": "Could not apply settings. Check the connection and retry."}
        finally:
            try:
                if "result" in locals():
                    writer.write(json.dumps(result).encode() + b"\n")
                    await writer.drain()
            except ConnectionError:
                pass  # Web may disconnect while configuration maintenance stops it.
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    server = await asyncio.start_unix_server(connection, path=str(path), limit=LIMIT)
    os.chown(path, 0, 422)
    os.chmod(path, 0o660)
    from . import onboarding, mdns
    background = [asyncio.create_task(onboarding.run()), asyncio.create_task(mdns.run())]
    async with server:
        while True:
            await asyncio.sleep(.5)
            async with serial:
                try:
                    await asyncio.to_thread(device.tick)
                except (OSError, ValueError, subprocess.SubprocessError):
                    # Keep the journal; retry rollback on the next tick.
                    device.last_result = "Device change failed. Retrying recovery."
                    if device.trial():
                        device.work = None
                        await asyncio.to_thread(device.revert)


def main():
    command = argparse.ArgumentParser()
    command.add_argument("action", choices=("run", "prepare"))
    action = command.parse_args().action
    if os.geteuid() != 0:
        raise SystemExit("Root required")
    if action == "prepare":
        device = Device()
        device.validate()
        device.boot_prepare()
    else:
        import asyncio
        asyncio.run(serve())


if __name__ == "__main__":
    main()
