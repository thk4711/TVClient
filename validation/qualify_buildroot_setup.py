#!/usr/bin/env python3
"""Native Linux bootstrap/API qualification, with no tuner or production data.

Requires a separately compiled native copy of the selected Tvheadend source.
This is not target-binary, imported-database or physical acceptance.
"""
import argparse
import asyncio
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from satip_appliance import persistent, provision
from satip_appliance.setup import verify_access
from satip_controller.backend import Backend
from satip_controller.state import read_json, atomic_json


def qualify(binary, scan_data):
    for port in (9981, 9982):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="satip-setup-native-") as temporary:
        root = Path(temporary)
        helper = root / "usr/sbin/satip-dev-provision"
        helper.parent.mkdir(parents=True)
        shutil.copyfile(repository / "buildroot/external/board/satip/rootfs-overlay/usr/sbin/satip-dev-provision", helper)
        timezone = root / "usr/share/zoneinfo/Etc/UTC"
        timezone.parent.mkdir(parents=True)
        shutil.copyfile("/usr/share/zoneinfo/Etc/UTC", timezone)
        data = root / "data"
        data.mkdir()
        with patch.object(persistent.os, "chown"):
            persistent.publish(data)
            persistent.prepare(root, data)
            provision.seed(root, data, {})
        store = data / "store"
        config = read_json(store / "tvheadend/config")
        config["muxconfpath"] = str(scan_data)
        atomic_json(store / "tvheadend/config", config)
        factory = read_json(store / "identity/factory.json")
        backend = Backend(factory["admin"])
        with (root / "backend.log").open("wb") as log:
            # --nosatip suppresses SSDP/tuner interactions for this host-only test.
            child = subprocess.Popen([str(binary), "-c", str(store / "tvheadend"),
                "--bindaddr", "127.0.0.1", "--nosyslog", "--nosatip"],
                stdout=log, stderr=subprocess.STDOUT)
            try:
                end = time.monotonic() + 15
                while True:
                    try:
                        backend._request("access/entry/grid", {"limit": 10000}, False)
                        break
                    except Exception:
                        if child.poll() is not None or time.monotonic() > end:
                            raise RuntimeError("Native backend did not become authenticated-ready") from None
                        time.sleep(.1)
                acl = asyncio.run(verify_access(backend, factory["ids"]["playback_acl"]))
                # Explicit negative checks: no anonymous API administration, and
                # restricted playback credentials cannot administer the backend.
                address = "http://127.0.0.1:9981/api/access/entry/grid"
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for headers in ({}, {"Authorization": Backend(factory["playback"]).header},
                                {"Authorization": Backend({"username": "satip-admin", "password": "invalid-fixture-password"}).header}):
                    try:
                        opener.open(urllib.request.Request(address, headers=headers), timeout=2)
                    except urllib.error.HTTPError as error:
                        if error.code not in (401, 403):
                            raise
                    else:
                        raise RuntimeError("Native backend accepted forbidden administration")
                created = backend._request("mpegts/network/create", {"class": "dvb_network_dvbs",
                    "conf": {"networkname": "SAT-IP native qualification", "orbital_pos": "19.2E",
                             "skipinitscan": True, "idlescan": False, "autodiscovery": False}}, True)
                network = created["uuid"]
                scanfiles = backend._request("dvb/scanfile/list", {"type": "dvbs", "satpos": 192}, False)["entries"]
                if not scanfiles:
                    raise RuntimeError("Pinned native backend has no Astra mux definitions")
                backend._request("idnode/save", {"node": {"uuid": network, "scanfile": scanfiles[0]["key"]}}, True)
                muxes = backend._request("mpegts/mux/grid", {"limit": 10000}, False)["entries"]
                if not muxes:
                    raise RuntimeError("Native mux definition creation failed")
                # No tuner, subscriptions, live stream or imported records.
                return {"schema": 1, "native_bootstrap_api_passed": True, "anonymous_admin_denied": True,
                        "playback_admin_denied": True, "wrong_password_denied": True,
                        "playback_connection_limit": acl["conn_limit"], "backend_schema": 24,
                        "astra_scanfiles": len(scanfiles), "generated_muxes": len(muxes),
                        "target_execution": False, "tuner_access": False, "hardware_acceptance": False}
            finally:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--scan-data", type=Path, required=True, help="Public pinned DVB scan definitions, never private backend data")
    args = parser.parse_args()
    print(json.dumps(qualify(args.backend.resolve(), args.scan_data.resolve()), indent=2))


if __name__ == "__main__":
    main()
