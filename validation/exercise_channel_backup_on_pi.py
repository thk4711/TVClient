#!/usr/bin/env python3
"""Opt-in reversible channel backup API check; run as root on the development Pi.

Interrupts TV, rotates saved favourites, temporarily narrows the catalogue and
restores the original channel archive. Never prints credentials or archive bytes.
A private original archive/evidence file is retained under /root/satip-deployments.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

import httpx

from satip_controller.state import atomic_json, read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-interrupt", action="store_true")
    args = parser.parse_args()
    if not args.confirm_interrupt or os.geteuid() != 0:
        parser.error("Requires root and --confirm-interrupt; do not run alongside other tests or physical controls")
    store = Path("/data/store")
    login = read_json(store / "config/web/login.json")
    client = httpx.Client(base_url="http://127.0.0.1:8080", auth=(login["username"], login["password"]), timeout=120)
    directory = Path("/root/satip-deployments") / ("channel-validation-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    directory.mkdir(mode=0o700)
    report = {}
    original_library = read_json(store / "management/library.json")
    original_candidates = read_json(store / "web/candidates.json")
    protected = ["management/settings.json", "config/player/credentials.json", "config/web/login.json",
                 "config/web/tvheadend-admin.json", "config/web/preview-credentials.json", "identity/factory.json",
                 "identity/device.json", "identity/development.txt", "config/web/config.json", "config/player/config.json"]
    protected += [p.relative_to(store).as_posix() for p in (store / "tvheadend/input/satip").rglob("*") if p.is_file()]
    protected += [p.relative_to(store).as_posix() for p in (store / "tvheadend/input/dvb/networks").glob("*/config")]
    def hashes():
        from satip_appliance.backup import backend_record
        result = {}
        for name in protected:
            path = store / name
            if not path.is_file():
                continue
            content = path.read_bytes()
            if name.startswith("tvheadend/input/dvb/networks/"):
                record = backend_record(content)
                record.pop("pnetworkname", None)  # Broadcast-discovered name naturally updates during TV reception.
                content = json.dumps(record, sort_keys=True).encode()
            result[name] = hashlib.sha256(content).hexdigest()
        return result
    baseline = hashes()
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text()
    library_hash = hashlib.sha256((store / "management/library.json").read_bytes()).hexdigest()
    catalogue_hash = hashlib.sha256((store / "web/candidates.json").read_bytes()).hexdigest()
    def post(path, **kwargs):
        response = client.post(path, headers={"X-Satip-Action": "1", **kwargs.pop("headers", {})}, **kwargs)
        if response.status_code != 200:
            raise RuntimeError("API operation failed with HTTP " + str(response.status_code))
        return response.json()
    def action(name, token, confirm=False):
        return post("/api/device", json={"action": name, "args": {"id": token},
            **({"current_password": login["password"]} if confirm else {})})
    def wait(token, expected):
        end = time.monotonic() + 420
        while time.monotonic() < end:
            try:
                response = client.post("/api/device", headers={"X-Satip-Action": "1"},
                    json={"action": "backup_status", "args": {"id": token}})
                if response.status_code == 503:
                    time.sleep(2)
                    continue
                response.raise_for_status()
                result = response.json()
                if result["state"] == expected:
                    return result
                if result["state"] in ("error", "missing"):
                    raise RuntimeError("Maintenance failed or expired")
            except (httpx.TransportError, httpx.TimeoutException):
                pass
            time.sleep(2)
        raise RuntimeError("Maintenance timeout")
    changed = False
    archive = None
    def restore():
        token = uuid.uuid4().hex
        result = post("/api/device/channels/upload/" + token, content=archive,
                      headers={"Content-Type": "application/octet-stream"})
        assert result["kind"] == "channels"
        action("backup_channels_restore", token, confirm=True)
        wait(token, "restored")
    try:
        token = uuid.uuid4().hex
        start = time.monotonic()
        action("backup_channels_create", token)
        wait(token, "ready")
        response = client.get("/api/device/channels/download/" + token)
        response.raise_for_status()
        archive = response.content
        path = directory / "original-channels.tar"
        path.write_bytes(archive)
        path.chmod(0o600)
        report.update(archive_bytes=len(archive), snapshot_seconds=round(time.monotonic() - start, 2))
        token = uuid.uuid4().hex
        validation = post("/api/device/channels/upload/" + token, content=archive,
                          headers={"Content-Type": "application/octet-stream"})
        report["available"] = validation["available"]
        report["favourites"] = validation["favourites"]
        assert hashes() == baseline
        assert hashlib.sha256((store / "management/library.json").read_bytes()).hexdigest() == library_hash
        report["validation_without_restore_unchanged"] = True
        bad = client.post("/api/device/channels/upload/" + uuid.uuid4().hex, content=b"not an archive",
                          headers={"X-Satip-Action": "1", "Content-Type": "application/octet-stream"})
        assert bad.status_code == 409
        assert hashes() == baseline
        report["malformed_rejected"] = True
        live = client.get("/api/channels").json()
        assert len(live["favourites"]) >= 2
        services = live["favourites"][1:] + live["favourites"][:1]
        changed = True
        response = client.put("/api/favourites", headers={"X-Satip-Action": "1"}, json={
            "services": services, "base_revision": live["library_revision"], "save_id": uuid.uuid4().hex})
        response.raise_for_status()
        atomic_json(store / "web/candidates.json", {"channels": original_candidates["channels"][:1]})
        assert read_json(store / "management/library.json")["favourites"] != original_library["favourites"]
        assert len(client.get("/api/channels").json()["channels"]) < len(live["channels"])
        start = time.monotonic()
        restore()
        changed = False
        report["restore_seconds"] = round(time.monotonic() - start, 2)
        assert hashlib.sha256((store / "management/library.json").read_bytes()).hexdigest() == library_hash
        assert hashlib.sha256((store / "web/candidates.json").read_bytes()).hexdigest() == catalogue_hash
        assert hashes() == baseline
        assert Path("/proc/sys/kernel/random/boot_id").read_text() == boot_id
        report.update(original_channel_bytes_restored=True, protected_files_unchanged=len(baseline), no_reboot=True)
        end = time.monotonic() + 60
        while time.monotonic() < end:
            player = client.get("/api/status").json()["player"]
            if player.get("playing") and not player.get("error"):
                report["player_playing_without_error"] = True
                break
            time.sleep(2)
        else:
            raise RuntimeError("Player did not resume")
        journal = Path("/data/.satip-restore.json")
        assert not journal.exists()
        report["journal_cleared"] = True
        report["private_original_archive"] = str(path)
        atomic_json(directory / "evidence.json", report, 0o600)
        print(json.dumps(report, indent=2))
    finally:
        if changed and archive:
            restore()
        client.close()


if __name__ == "__main__":
    main()
