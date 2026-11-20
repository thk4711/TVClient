import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from satip_controller.enigma2 import Enigma2, address, service_reference, xml_rows
from satip_appliance.tv_source import validate_profile, validate_iptv_record
from satip_appliance.channel_backup import allowed, network_path

REF = "1:0:19:283D:3FB:1:C00000:0:0:0:"
BOUQUET = '1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "userbouquet.free.tv" ORDER BY bouquet'


class Enigma2Tests(unittest.TestCase):
    def test_private_addresses_and_ports_only(self):
        for host in ("192.168.1.2", "10.1.2.3", "172.16.1.2"):
            self.assertEqual(address(host), host)
        for host in ("127.0.0.1", "169.254.1.2", "8.8.8.8", "224.1.1.1", "0.0.0.0", "receiver.local", "::1", None):
            with self.subTest(host=host), self.assertRaises(ValueError):
                address(host)
        for port in (0, -1, 65536, True, "80"):
            with self.assertRaises(ValueError):
                Enigma2("192.168.1.2", port)

    def test_service_references_reject_non_dvb_sources(self):
        self.assertEqual(service_reference(REF.lower()), REF)
        for ref in (BOUQUET, REF.replace("1:0:19", "1:64:19"), REF.replace("1:0:19", "1:0:1F"),
                    REF.replace("1:0:19", "1:0:2"), REF + "http://evil/", "pipe:///bin/sh", REF.replace("283D", "0")):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                service_reference(ref)
        self.assertEqual(Enigma2("192.168.1.2").stream_url(REF), "http://192.168.1.2:8001/" + REF)

    def test_xml_entities_depth_and_size_fail_closed(self):
        self.assertEqual(xml_rows(b"<root><e2service><name>Example &amp; TV</name></e2service></root>", "e2service"),
                         [{"name": "Example & TV"}])
        for content in (b'<!DOCTYPE root [<!ENTITY x "secret">]><root>&x;</root>',
                        b"<x>" * 17 + b"</x>" * 17, b"x" * (2*1024*1024+1), b"<root>"):
            with self.assertRaises(ValueError):
                xml_rows(content, "e2service")

    def test_bouquet_selection_deduplicates_and_excludes_markers(self):
        receiver = Enigma2("192.168.1.2")
        bouquets = [{"e2servicename": "Free", "e2servicereference": BOUQUET}]
        services = [{"e2servicename": "Das Erste HD", "e2servicereference": REF},
                    {"e2servicename": "Duplicate", "e2servicereference": REF.lower()},
                    {"e2servicename": "<n/a>", "e2servicereference": REF.replace("283D", "283E")},
                    {"e2servicename": "Folder", "e2servicereference": BOUQUET}]
        with patch.object(receiver, "read", side_effect=[bouquets, services]):
            self.assertEqual(receiver.services(["Free"]), [{"name": "Das Erste HD", "reference": REF}])
        with patch.object(receiver, "read", return_value=bouquets), self.assertRaises(ValueError):
            receiver.services(["Missing"])

    def test_epg_rows_use_existing_sanitizer(self):
        receiver = Enigma2("192.168.1.2")
        rows = [{"e2eventstart": "1700000000", "e2eventduration": "1800", "e2eventtitle": "Title\x01",
                 "e2eventdescriptionextended": "Description"}, {"e2eventstart": "bad"}]
        with patch.object(receiver, "read", return_value=rows):
            events = receiver.events(REF)
        self.assertEqual(events[0]["title"], "Title")
        self.assertEqual(events[0]["stop"], 1700001800)
        self.assertEqual(len(events), 1)

    def test_backup_iptv_paths_are_exact(self):
        self.assertTrue(allowed("tvheadend/input/iptv/networks/" + "a"*32 + "/muxes/" + "b"*32))
        self.assertFalse(allowed("tvheadend/input/iptv/config"))
        self.assertEqual(network_path({"network_uuid": "a"*32, "input_type": "iptv"}),
                         "tvheadend/input/iptv/networks/" + "a"*32 + "/muxes/")

    def test_iptv_backup_rejects_arbitrary_urls_and_hooks(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Path(temp)
            (store / "web").mkdir()
            data = json.loads((Path(__file__).parent / "fixtures/library.json").read_text())
            profile = {"version": 1, "network_uuid": "a"*32, "library": data, "candidates": {"channels": []},
                       "enigma2": {"address": "192.168.1.2", "web_port": 80, "stream_port": 8001,
                                   "bouquets": ["Free"], "services": {"b"*32: REF}}}
            (store / "web/tv-source-enigma2.json").write_text(json.dumps(profile))
            network_name = "tvheadend/input/iptv/networks/" + "a"*32 + "/config"
            safe_network = {"max_streams": 1, "skipinitscan": True, "idlescan": False}
            validate_iptv_record(network_name, safe_network, store)
            for extra in ({"url": "http://192.168.1.3/list.m3u"}, {"remove_scrambled": True}, {"max_streams": 0}):
                with self.assertRaises(ValueError):
                    validate_iptv_record(network_name, {**safe_network, **extra}, store)
            name = "tvheadend/input/iptv/networks/" + "a"*32 + "/muxes/" + "b"*32
            record = {"config": {"iptv_url": "http://192.168.1.2:8001/" + REF}}
            validate_iptv_record(name, record, store)
            for url in ("pipe:///bin/sh", "http://127.0.0.1/", "http://192.168.1.3:8001/" + REF):
                with self.assertRaises(ValueError):
                    validate_iptv_record(name, {"config": {"iptv_url": url}}, store)
            with self.assertRaises(ValueError):
                validate_iptv_record(name, {"config": {**record["config"], "iptv_hdr": "Authorization: secret"}}, store)


if __name__ == "__main__":
    unittest.main()
