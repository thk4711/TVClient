import io
import unittest
from unittest.mock import AsyncMock, patch

from satip_appliance import discovery, registration, setup
from satip_controller.backend import BackendError
from satip_controller.state import atomic_json, read_json
from tests.test_setup import FreshFixture, FakeBackend, SERVER, FRONTEND, SCANFILE, serialized

URL = "http://192.168.1.10:38400/description.xml"
UDN = "uuid:28146e70-1dd2-11b2-8021-c19f52d1fd53"
XML = f'''<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>
<deviceType>{discovery.DEVICE}</deviceType><friendlyName>Digibit &amp; Twin</friendlyName>
<manufacturer>Telestar</manufacturer><modelName>Digibit Twin</modelName><UDN>{UDN}</UDN>
<satip:X_SATIPCAP xmlns:satip="urn:ses-com:satip">DVBS2-2</satip:X_SATIPCAP>
</device></root>'''.encode()


class DescriptionTests(unittest.TestCase):
    def test_direct_fallback_checks_only_conventional_descriptions(self):
        server = discovery.parse_description(XML, URL)
        with patch.object(discovery, "local_targets", return_value=["192.168.1.10"]), \
                patch.object(discovery.socket, "create_connection"), patch.object(discovery, "describe", return_value=server) as describe:
            self.assertEqual(discovery.direct_descriptions([]), [server])
            describe.assert_called_once_with(URL)

    def test_fallback_is_bounded_to_attached_small_private_networks(self):
        with patch.object(discovery.fcntl, "ioctl", return_value=bytes(20) + bytes([255, 255, 255, 0])):
            targets = discovery.local_targets([("eth0", "192.168.1.2")])
            self.assertEqual(len(targets), 253)
            self.assertNotIn("192.168.1.2", targets)
            self.assertEqual(discovery.local_targets([("eth0", "8.8.8.8")]), [])
        with patch.object(discovery.fcntl, "ioctl", return_value=bytes(20) + bytes([255, 255, 0, 0])):
            self.assertEqual(discovery.local_targets([("eth0", "192.168.1.2")]), [])

    def test_real_namespace_shape_and_advertised_capabilities(self):
        server = discovery.parse_description(XML, URL)
        self.assertEqual((server["udn"], server["name"], server["tuners"]), (UDN, "Digibit & Twin", 2))
        self.assertEqual(discovery.parse_description(XML.replace(b"DVBS2-2", b"DVBS-1,DVBT2-4,DVBS2-2"), URL)["tuners"], 3)

    def test_invalid_xml_entities_and_non_satellite_servers(self):
        for content in (b"not XML", XML[:-12], XML.replace(b"DVBS2-2", b"DVBT2-2"),
                        XML.replace(b"DVBS2-2", b"DVBS2-9999"),
                        XML.replace(b"<root ", b'<!DOCTYPE root [<!ENTITY a "secret">]><root '),
                        XML.replace(UDN.encode(), b"not-a-uuid"), b"x" * 65537):
            with self.subTest(content=content[:60]), self.assertRaises(ValueError):
                discovery.parse_description(content, URL)

    def test_url_boundary_including_digibit_port(self):
        self.assertEqual(discovery.description_url(URL), URL)
        self.assertEqual(discovery.description_url("http://192.168.1.10:80/desc.xml"), "http://192.168.1.10/desc.xml")
        for url in ("http://127.0.0.1/a", "http://example.org/a", "https://192.168.1.10/a",
                    "http://192.168.1.10:9981/a", "http://192.168.1.10:0/a",
                    "http://user:pass@192.168.1.10/a", URL + "?x=1", URL + "#fragment",
                    "http://192.168.1.10/../config", "http://169.254.1.1/a", URL + "\n"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                discovery.description_url(url)

    def test_description_fetch_is_bounded_and_has_no_proxy_or_redirect(self):
        with patch.object(discovery.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = io.BytesIO(XML)
            self.assertEqual(discovery.describe(URL)["udn"], UDN)
            proxy, redirect = build.call_args.args
            self.assertEqual(proxy.proxies, {})
            self.assertIsNone(redirect.redirect_request(None))
            build.return_value.open.return_value = io.BytesIO(b"x" * 65537)
            with self.assertRaises(ValueError):
                discovery.describe(URL)

    def test_ssdp_case_insensitive_headers_and_rejects_unrelated_devices(self):
        packet = f"HTTP/1.1 200 OK\r\nST: {discovery.DEVICE}\r\nLocation: {URL}\r\n\r\n".encode()
        self.assertEqual(discovery.response_location(packet), URL)
        self.assertIsNone(discovery.response_location(packet.replace(discovery.DEVICE.encode(), b"upnp:rootdevice")))
        self.assertIsNone(discovery.response_location(packet.replace(URL.encode(), b"http://127.0.0.1/a")))

    def test_network_not_ready_can_be_retried(self):
        with patch.object(discovery, "interfaces", return_value=[]):
            result = discovery.search_servers()
        self.assertEqual(result, {"servers": [], "interfaces": [], "warnings": []})


class RegistrationTests(FreshFixture):
    def request(self, url=URL):
        node = {"url": url, "udn": UDN, "request_id": "a" * 32}
        atomic_json(self.store / "web/server-request.json", node)
        return node

    def test_selection_is_persistent_and_idempotent(self):
        node = self.request()
        supervisor = registration.Registration(self.store)
        with patch.object(registration.Backend, "_request", return_value={"entries": []}) as backend:
            self.assertEqual(supervisor.refresh(), URL)
            self.assertEqual(supervisor.refresh(), URL)
            self.assertEqual(backend.call_count, 1)
        self.assertEqual(read_json(supervisor.accepted), node)
        self.assertEqual(registration.Registration(self.store).url, URL)
        self.assertEqual(read_json(self.store / "web/server-result.json")["state"], "accepted")

    def test_active_subscriptions_or_committed_setup_prevent_restart(self):
        self.request()
        supervisor = registration.Registration(self.store)
        with patch.object(registration.Backend, "_request", return_value={"entries": [{"id": 1}]}):
            self.assertIsNone(supervisor.refresh())
        self.assertFalse(supervisor.accepted.exists())
        atomic_json(self.store / "web/setup-state.json", {})
        self.assertIsNone(registration.Registration(self.store).refresh())
        self.assertIn("committed", read_json(self.store / "web/server-result.json")["error"])

    def test_backend_temporarily_unavailable_retries(self):
        self.request()
        supervisor = registration.Registration(self.store)
        with patch.object(registration.Backend, "_request", side_effect=[BackendError("restart"), {"entries": []}]):
            self.assertIsNone(supervisor.refresh())
            self.assertEqual(supervisor.refresh(), URL)

    def test_malformed_and_symlinked_requests_cannot_change_launch(self):
        path = self.store / "web/server-request.json"
        for node in ({"url": URL}, {"url": "http://127.0.0.1/a", "udn": UDN, "request_id": "a"*32},
                     {"url": URL, "udn": UDN, "request_id": "a"*32, "command": "sh"}):
            atomic_json(path, node)
            self.assertIsNone(registration.Registration(self.store).refresh())
        path.unlink()
        path.symlink_to(self.store / "identity/factory.json")
        self.assertIsNone(registration.Registration(self.store).refresh())
        self.assertFalse((self.store / "identity/satip-server.json").exists())


class SelectionTests(FreshFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = FakeBackend(self.factory)
        self.preferences = read_json(self.store / "config/web/setup.json")
        self.setup = setup.Setup(self.backend, self.store, self.preferences, ["testserver"])
        self.addAsyncCleanup(self.setup.close_discovery)
        self.server = discovery.parse_description(XML, URL)

    async def test_manual_server_registration_waits_for_real_frontends(self):
        with patch.object(discovery, "describe", return_value=self.server):
            await self.setup.add_server(URL)
            await self.setup.select_server(URL, UDN)
        self.assertEqual(self.setup.registration["state"], "connecting")
        with self.assertRaises(BackendError):
            await self.setup.configure(SERVER, FRONTEND, SCANFILE)
        node = read_json(self.store / "web/server-request.json")
        atomic_json(self.store / "web/server-result.json", {"request_id": node["request_id"], "state": "accepted"})
        # Registration must wait across an empty tree and backend restart.
        tuner = {"server_uuid": SERVER, "frontend_uuid": FRONTEND, "tuner_number": 2}
        with patch.object(self.setup, "discover", new=AsyncMock(side_effect=[[], BackendError("restart"), [tuner]])):
            await self.setup.registration_task
        self.assertEqual(self.setup.registration["state"], "ready")
        self.assertEqual(self.setup.available_tuners, [tuner])
        restored = setup.Setup(self.backend, self.store, self.preferences, ["testserver"])
        self.assertEqual(restored.selected["udn"], UDN)

    async def test_only_selected_server_frontends_are_returned(self):
        self.setup.selected = self.server
        self.assertEqual(await self.setup.discover(), [])  # Fake server has no matching UDN.
        # The pinned backend's actual serialized class is satip_client, not satip_device.
        root = [serialized(SERVER, "satip_client", addr="192.168.1.10", device_uuid=UDN[5:]),
                serialized("f"*32, "satip_client", device_uuid="unrelated")]
        frontend = [serialized(FRONTEND, "satip_frontend_dvbs", fe_number=2)]
        with patch.object(self.backend, "request", new=AsyncMock(side_effect=[root, frontend])) as request:
            tuners = await self.setup.discover()
        self.assertEqual(tuners[0]["frontend_uuid"], FRONTEND)
        self.assertEqual(request.await_count, 2)

    async def test_interrupted_metadata_publication_recovers_request(self):
        atomic_json(self.store / "web/server-request.json", {"url": URL, "udn": UDN, "request_id": "a" * 32})
        restored = setup.Setup(self.backend, self.store, self.preferences, ["testserver"])
        self.assertEqual(restored.selected["udn"], UDN)

    async def test_refresh_retains_manual_servers_and_coalesces_searches(self):
        self.setup.discovery["servers"] = [{**self.server, "manual": True}]
        with patch.object(discovery, "search_servers", return_value={"servers": [], "interfaces": ["wlan0"], "warnings": []}) as search:
            self.setup.start_discovery()
            self.setup.start_discovery()
            await self.setup.discovery_task
            self.assertEqual(search.call_count, 1)
        self.assertEqual(self.setup.discovery["servers"][0]["udn"], UDN)

    async def test_identity_change_is_rejected_before_registration(self):
        self.setup.discovery["servers"] = [self.server]
        other = {**self.server, "udn": "uuid:00000000-0000-0000-0000-000000000000"}
        with patch.object(discovery, "describe", return_value=other), self.assertRaises(BackendError):
            await self.setup.select_server(URL, UDN)
        self.assertFalse((self.store / "web/server-request.json").exists())
