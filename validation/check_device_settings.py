#!/usr/bin/env python3
"""Run on the test Pi. Read-only by default; changes require explicit flags.

Credentials are read privately on the Pi and never printed. The optional trial
uses the same SSID/password/country and lets the 90-second timer restore the
previous configuration. --exercise saves unchanged identity and scans Wi-Fi.
Do not use either changing mode during a scan or Live TV preview.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exercise", action="store_true")
    parser.add_argument("--wifi-trial", action="store_true")
    args = parser.parse_args()
    login = json.loads(Path("/etc/satip-web/login.json").read_text())
    auth = (login["username"], login["password"])
    with httpx.Client(base_url="http://127.0.0.1:8080", auth=auth, timeout=28,
                      headers={"X-Satip-Action": "1"}, trust_env=False) as client:
        def get(path):
            result = client.get(path)
            result.raise_for_status()
            return result.json()
        data = get("/api/device")
        assert get("/api/settings")["device_settings"] is True
        assert data["trial"] is None
        assert "password" not in data and "wifi_psk" not in data
        unauthenticated = httpx.get("http://127.0.0.1:8080/api/device", trust_env=False)
        assert unauthenticated.status_code == 401
        print("PASS: authenticated settings and secret-free status")
        if args.exercise:
            result = client.post("/api/device", json={"action": "reboot"})
            assert result.status_code == 403
            result = client.post("/api/device", json={"action": "device", "args": {"hostname": "bad/name", "timezone": data["timezone"]}})
            assert result.status_code == 409
            # A no-op save exercises the persistent writer without changing identity.
            result = client.post("/api/device", json={"action": "device", "args": {"hostname": data["hostname"], "timezone": data["timezone"]}})
            result.raise_for_status()
            result = client.post("/api/device", json={"action": "scan_wifi"})
            result.raise_for_status()
            print("PASS: validation/access guards, unchanged device save and Wi-Fi scan")
        player = get("/api/status")["player"]
        assert player.get("playing") and not player.get("error")
        print("PASS: HDMI player reports active playback")
        if args.wifi_trial:
            conf = Path("/etc/wpa_supplicant.conf")
            before = hashlib.sha256(conf.read_bytes()).hexdigest()
            result = client.post("/api/device", json={"action": "wifi", "args": {
                "ssid": data["wifi_ssid"], "password": "", "country": data["wifi_country"]}})
            result.raise_for_status()
            deadline = time.monotonic() + 125
            time.sleep(3)
            while time.monotonic() < deadline:
                try:
                    current = get("/api/device")
                except httpx.TransportError:
                    time.sleep(2)
                    continue
                if not current["trial"]:
                    break
                time.sleep(2)
            else:
                raise RuntimeError("Wi-Fi rollback did not complete")
            assert hashlib.sha256(conf.read_bytes()).hexdigest() == before
            assert get("/api/device")["wifi_ssid"] == data["wifi_ssid"]
            print("PASS: unconfirmed current-network trial automatically restored exact Wi-Fi configuration")


if __name__ == "__main__":
    main()
