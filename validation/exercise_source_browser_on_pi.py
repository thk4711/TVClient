#!/usr/bin/env python3
"""Opt-in source browser/first-setup acceptance on the Pi; no source mutation.

The fresh-setup mode uses the existing restorable rehearsal and always restores
its saved installation. Run as root on the Pi, with --confirm-interrupt.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request


def call(path, body=None, timeout=40):
    request = urllib.request.Request("http://127.0.0.1:8080" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-Satip-Action": "1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def wait_ready(path, condition, seconds=60):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            data = call(path)
            if condition(data):
                return data
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    raise RuntimeError("Timed out waiting for " + path)


def rehearsal(action):
    subprocess.run(["python3", "-m", "satip_appliance.setup_rehearsal", action, "--name", "source-browser"], check=True,
        timeout=120, env={**os.environ, "PYTHONPATH": "/opt/satip", "SATIP_RUNTIME_ROOT": "/run"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-interrupt", action="store_true")
    parser.add_argument("--first-setup", action="store_true")
    parser.add_argument("--address", required=True)
    parser.add_argument("--bouquet", action="append", required=True)
    parser.add_argument("--timeout", type=int, default=5, choices=range(5, 91))
    args = parser.parse_args()
    if not args.confirm_interrupt or os.geteuid() != 0:
        parser.error("Run as root with --confirm-interrupt")
    started = False
    try:
        if args.first_setup:
            if Path("/data/.satip-setup-rehearsal-source-browser/original").exists():
                raise RuntimeError("The named source-browser rehearsal is already active; inspect it first")
            started = True
            rehearsal("start")
            wait_ready("/api/setup/status", lambda s: s["state"] == "unconfigured")
        receiver = {"address": args.address, "web_port": 80, "stream_port": 8001}
        bouquets = call("/api/sources/bouquets", receiver)["bouquets"]
        if not set(args.bouquet) <= {b["name"] for b in bouquets}:
            raise RuntimeError("Requested bouquets are missing")
        print("Bouquet listing:", len(bouquets), "TV bouquets", flush=True)
        call("/api/sources/discover", {"source": "enigma2"})
        result = wait_ready("/api/sources", lambda s: s["discovery"]["enigma2"]["state"] != "searching")
        if args.address not in {s["address"] for s in result["discovery"]["enigma2"]["servers"]}:
            raise RuntimeError("Configured receiver was not found by LAN discovery")
        print("LAN discovery found the receiver", flush=True)
        call("/api/sources/import", {**receiver, "bouquets": args.bouquet,
                                    "timeout": args.timeout, "confirm_pause": True})
        result = wait_ready("/api/sources", lambda s: s["import"]["state"] in ("complete", "error", "cancelled"),
                            seconds=500 * (args.timeout + 4) + 60)
        scan = result["import"]
        print("Import:", {k: scan.get(k) for k in ("state", "total", "completed", "found", "failed_muxes", "error")}, flush=True)
        if scan["state"] != "complete" or scan.get("recovery_pending"):
            raise RuntimeError("Import did not complete cleanly")
        profile = json.loads(Path("/data/store/web/tv-source-enigma2.json").read_text())
        refs = profile["enigma2"]["services"]
        if len(refs.values()) != len(set(refs.values())):
            raise RuntimeError("Duplicate mux service references")
        print("Deduplicated profile:", len(refs), "mux references", flush=True)
        if args.first_setup:
            status = call("/api/setup/status")
            if status.get("source") != "enigma2" or not status["prepared"] or status["tuners"]:
                raise RuntimeError("First setup did not prepare Enigma2 independently")
            chosen = [c["service_uuid"] for c in status["candidates"] if c["name"] in ("Das Erste HD", "ZDF HD")]
            call("/api/setup/finish", {"services": chosen or [status["candidates"][0]["service_uuid"]]})
            wait_ready("/api/setup/status", lambda s: s["state"] == "configured", seconds=90)
            selected = call("/api/sources")
            if selected["active"] != "enigma2" or selected["available"] != ["enigma2"]:
                raise RuntimeError("Direct Enigma2 completion invented a SAT-IP lineup")
            print("Direct Enigma2 first setup reached configured management", flush=True)
            player = wait_ready("/api/status", lambda s: s["player"].get("playing") and not s["player"].get("error"), seconds=90)
            print("Player:", player["player"].get("channel_name"), flush=True)
    finally:
        if started and Path("/data/.satip-setup-rehearsal-source-browser/original").exists():
            rehearsal("restore")
            wait_ready("/api/setup/status", lambda s: s["state"] == "configured", seconds=90)
            print("Original installation restored", flush=True)


if __name__ == "__main__":
    main()
