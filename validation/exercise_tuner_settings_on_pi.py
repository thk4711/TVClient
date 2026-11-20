#!/usr/bin/env python3
"""Opt-in tuner round trip on the configured Pi; always restore the original tuner.

Run as root on the Pi with --confirm-interrupt. Uses the same queued device API
as the browser. Never scans, imports, replaces servers or changes favourites.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time
import urllib.request


def call(path, body=None):
    request = urllib.request.Request("http://127.0.0.1:8080" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-Satip-Action": "1"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def wait(path, condition, seconds=90):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            data = call(path)
            if condition(data):
                return data
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError("Timed out waiting for " + path)


def lineup():
    store = Path("/data/store")
    return {name: hashlib.sha256((store / name).read_bytes()).hexdigest()
            for name in ("management/library.json", "web/candidates.json", "config/web/config.json")}


def apply(frontend):
    result = call("/api/device", {"action": "satip_tuner", "args": {"frontend": frontend}})
    print(result["message"], flush=True)
    data = wait("/api/device", lambda d: d.get("message") == "SAT-IP tuner applied."
                or d.get("message", "").startswith("SAT-IP tuner change failed"))
    if data["message"] != "SAT-IP tuner applied.":
        raise RuntimeError(data["message"])
    source = wait("/api/sources", lambda d: d["satip"]["configured_frontend"] == frontend and d["satip"]["receiving"])
    print("Receiving:", source["satip"]["receiving"], flush=True)
    player = wait("/api/status", lambda d: d["player"].get("playing") and not d["player"].get("error"))
    print("Playback:", player["player"].get("channel_name"), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-interrupt", action="store_true")
    args = parser.parse_args()
    if not args.confirm_interrupt:
        parser.error("Use --confirm-interrupt to restart TV twice")
    if call("/api/device").get("require_auth", True):
        parser.error("This acceptance script requires a login-free test receiver")
    source = call("/api/sources")
    if source["active"] != "satip":
        parser.error("SAT-IP must be active")
    original = source["satip"]["configured_frontend"]
    other = next(t for t in source["satip"]["tuners"] if t["uuid"] != original)
    before = lineup()
    try:
        print("Testing tuner #" + str(other["number"]), flush=True)
        apply(other["uuid"])
        if lineup() != before:
            raise RuntimeError("Channel or source configuration changed")
    finally:
        print("Restoring original tuner", flush=True)
        apply(original)
    if lineup() != before:
        raise RuntimeError("Channel or source configuration changed")
    print("PASS: tuner round trip, receiving input, resumed playback and unchanged lineup", flush=True)


if __name__ == "__main__":
    main()
