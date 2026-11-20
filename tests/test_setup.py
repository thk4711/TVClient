import asyncio
import base64
import copy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from satip_appliance import persistent, provision, setup, web
from satip_controller.backend import BackendError
from satip_controller.config import Config, library
from satip_controller.scan import Manager
from satip_controller.state import atomic_json, read_json
from tests.test_buildroot_foundation import BOARD, provision as parser

SERVER, FRONTEND, OTHER, NETWORK, MUX, SERVICE, CHANNEL = [str(n) * 32 for n in range(1, 8)]
SCANFILE = "dvbs/fixture/Astra-19.2E"


class FreshFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.store = self.data / "store"
        helper = self.root / "usr/sbin/satip-dev-provision"
        helper.parent.mkdir(parents=True)
        shutil.copyfile(BOARD / "rootfs-overlay/usr/sbin/satip-dev-provision", helper)
        timezone = self.root / "usr/share/zoneinfo/Etc/UTC"
        timezone.parent.mkdir(parents=True)
        timezone.write_bytes(b"TZif\x00\xfffixture")
        self.addCleanup(patch.stopall)
        patch.object(persistent.os, "chown").start()
        persistent.publish(self.data)
        persistent.prepare(self.root, self.data)
        provision.seed(self.root, self.data, {})
        self.factory = read_json(self.store / "identity/factory.json")


class ProvisionTests(FreshFixture):
    def test_seed_matches_installed_backend_schema_without_migration(self):
        backend = read_json(self.store / "tvheadend/config")
        self.assertEqual(backend["version"], 24)
        self.assertEqual(backend["full_version"], "0.0.0~unknown")
        self.assertEqual(self.factory["settings"]["lan_access"], "login-free")

    def test_boot_failures_report_the_failing_stage_without_secrets(self):
        error = self.root / "run/satip-boot-error"
        with patch.object(persistent, "validate_mount", side_effect=ValueError("private disk details")):
            with self.assertRaises(ValueError):
                provision.boot(self.root, self.data)
        self.assertEqual(error.read_text(), "data-mount\n")
        (self.root / "boot").mkdir()
        config = self.root / "boot/satip-config.txt"
        config.write_text("root_password=x\nroot_password=private")
        with patch.object(persistent, "validate_mount"):
            with self.assertRaises(ValueError):
                provision.boot(self.root, self.data)
            self.assertEqual(error.read_text(), "config-duplicate:2\n")
            config.write_text("root_password=x")
            with patch.object(persistent, "prepare", side_effect=ValueError("private schema")):
                with self.assertRaises(ValueError):
                    provision.boot(self.root, self.data)
            self.assertEqual(error.read_text(), "data-prepare\n")
            with patch.object(provision, "seed", side_effect=ValueError("private backend")):
                with self.assertRaises(ValueError):
                    provision.boot(self.root, self.data)
            self.assertEqual(error.read_text(), "backend-init\n")

    def test_saved_legacy_allowlist_and_no_shell(self):
        self.assertEqual(parser.parse_config("satip_xml=http://192.168.1.10:38400/description.xml")["satip_xml"],
                         "http://192.168.1.10:38400/description.xml")
        values = parser.parse_config("initialize_data=yes\ntimezone=Europe/Berlin\nkeyboard_vendor=1234\n"
            "keyboard_product=5678\nir_profile=disabled\nsatellite=Astra-19.2E\nsatip_source=2\n"
            "tuner_number=2\nsatip_xml=http://192.168.1.10/desc.xml\nlan_access=authenticated")
        self.assertEqual(values["satip_source"], "2")
        for text in ("timezone=../etc", "initialize_data=no", "satip_source=5", "tuner_number=0",
                     "satellite=unknown", "keyboard_vendor=all",
                     "satip_xml=http://127.0.0.1/a", "satip_xml=http://192.168.1.1:9981/a",
                     "satip_xml=http://user:password@192.168.1.1/a", "satip_xml=http://example.com/a",
                     "audio_device=$(id)", "lan_access=yes"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parser.parse_config(text)

    def test_seed_is_unique_protected_and_repeatable(self):
        before = (self.store / "identity/factory.json").read_bytes()
        provision.seed(self.root, self.data, {"setup_password": "ignored-on-retry-secret"})
        self.assertEqual(before, (self.store / "identity/factory.json").read_bytes())
        passwords = [self.factory[role]["password"] for role in ("admin", "playback", "login")]
        self.assertEqual(len(set(passwords)), 3)
        self.assertTrue(all(len(p) >= 32 for p in passwords))
        self.assertEqual((self.store / "identity/factory.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.store / "management/library.json").exists())
        self.assertEqual(read_json(self.store / "installation.json"), {"state": "unconfigured"})
        Config.load(self.store / "config/player/config.json")

    def test_new_seed_imports_only_timezone_not_legacy_boot_preferences(self):
        other = self.root / "fresh-data"
        other.mkdir()
        persistent.publish(other)
        zone = self.root / "usr/share/zoneinfo/Europe/Berlin"
        zone.parent.mkdir(parents=True)
        zone.write_bytes(b"Berlin fixture")
        provision.seed(self.root, other, {
            "timezone": "Europe/Berlin", "setup_password": "obsolete-secret",
            "lan_access": "authenticated", "drm_connector": "HDMI-A-1",
            "drm_mode": "1280x720@50", "audio_device": "alsa/hdmi:CARD=vc4hdmi0,DEV=0",
            "keyboard_vendor": "1234", "keyboard_product": "5678", "ir_profile": "disabled",
            "satip_xml": "http://192.168.1.10/desc.xml", "satip_source": "2", "tuner_number": "2"})
        factory = read_json(other / "store/identity/factory.json")
        self.assertEqual(factory["settings"], {**provision.DEFAULTS, "timezone": "Europe/Berlin"})
        self.assertNotEqual(factory["login"]["password"], "obsolete-secret")
        self.assertEqual((self.root / "etc/localtime").read_bytes(), b"Berlin fixture")

    def test_bootstrap_has_current_schema_exact_acl_flags_and_password_encoding(self):
        ids = self.factory["ids"]
        acl = read_json(self.store / "tvheadend/accesscontrol" / ids["playback_acl"])
        self.assertFalse(acl["admin"])
        self.assertFalse(acl["webui"])
        self.assertEqual(acl["streaming"], ["basic"])
        self.assertEqual(acl["dvr"], [])
        self.assertEqual(acl["prefix"], "127.0.0.1/32")
        self.assertEqual((acl["channel_min"], acl["channel_max"]), (1000000, 3000000))
        admin = read_json(self.store / "tvheadend/accesscontrol" / ids["admin_acl"])
        self.assertEqual((admin["channel_min"], admin["channel_max"]), (0, 0))
        self.assertEqual(admin["streaming"], [])
        self.assertEqual(admin["prefix"], "127.0.0.1/32")
        self.assertIn("change_profiles", acl["change"])
        self.assertEqual(acl["profile"], [ids["pass"]])
        password = read_json(self.store / "tvheadend/passwd" / ids["playback_password"])
        self.assertEqual(base64.b64decode(password["password2"]).decode(), "TVHeadend-Hide-" + self.factory["playback"]["password"])
        self.assertEqual(read_json(self.store / "tvheadend/config")["version"], 24)

    def test_retry_does_not_overwrite_running_backend_database(self):
        config = self.store / "tvheadend/config"
        config.write_text("backend-written-state")
        provision.seed(self.root, self.data, {})
        self.assertEqual(config.read_text(), "backend-written-state")

    def test_unknown_backend_before_completion_refused(self):
        (self.store / "factory-ready.json").unlink()
        (self.store / "tvheadend/foreign").write_text("preserve")
        with self.assertRaises(ValueError):
            provision.seed(self.root, self.data, {})
        self.assertEqual((self.store / "tvheadend/foreign").read_text(), "preserve")

    def test_interrupted_bootstrap_reuses_credentials_and_retries(self):
        (self.store / "factory-ready.json").unlink()
        original = provision.owned_json
        def fail(path, *args, **kwargs):
            if path.name == self.factory["ids"]["playback_acl"]:
                raise OSError("injected full-media interruption")
            return original(path, *args, **kwargs)
        with patch.object(provision, "owned_json", side_effect=fail), self.assertRaises(OSError):
            provision.seed(self.root, self.data, {})
        self.assertFalse((self.store / "factory-ready.json").exists())
        provision.seed(self.root, self.data, {})
        self.assertTrue((self.store / "factory-ready.json").exists())
        self.assertEqual(read_json(self.store / "identity/factory.json"), self.factory)

    def test_activation_publishes_configured_not_hardware_qualified(self):
        acl = self.factory["ids"]["playback_acl"]
        atomic_json(self.store / "management/library.json", {"version": 1, "channels": [
            {"uuid": CHANNEL, "name": "Fixture", "service_uuid": SERVICE, "mux_uuid": MUX,
             "video": "h264", "audio_pid": 257}], "favourites": [CHANNEL]}, 0o640)
        atomic_json(self.store / "config/web/config.json", provision.web_config(NETWORK, acl, provision.DEFAULTS, ["testserver"]))
        atomic_json(self.store / "web/setup-complete.json", {"network_uuid": NETWORK, "playback_acl_uuid": acl})
        with patch.object(setup, "verify_access", new=AsyncMock()), \
                patch("satip_controller.backend.Backend._request", return_value={"entries": [{"uuid": NETWORK}]}):
            provision.activate(self.data)
        self.assertEqual(read_json(self.store / "installation.json"), {"state": "configured", "origin": "fresh", "hardware_qualified": False})
        self.assertEqual((self.store / "installation.json").stat().st_mode & 0o777, 0o644)
        provision.activate(self.data)

    def test_timezone_binary_and_both_input_sources_always_enabled(self):
        self.assertEqual((self.root / "etc/localtime").read_bytes(), b"TZif\x00\xfffixture")
        settings = {**provision.DEFAULTS, "keyboard_vendor": "1234", "keyboard_product": "5678", "ir_profile": "disabled"}
        provision.apply_devices(self.root, settings)
        rule = (self.root / "etc/udev/rules.d/99-satip-input.rules").read_text()
        self.assertIn("1234", rule)
        self.assertIn('ENV{ID_INPUT_KEYBOARD}=="1"', rule)
        self.assertIn('DRIVERS=="gpio_ir_recv", GROUP="satip-input", MODE="0660"', rule)
        self.assertNotIn('GROUP="root", MODE="0600"', rule)
        self.assertNotIn("04d9", rule)

    def test_imported_store_is_not_bootstrapped_or_qualified(self):
        atomic_json(self.store / "installation.json", {"state": "imported-unqualified"})
        before = (self.store / "identity/factory.json").read_bytes()
        provision.seed(self.root, self.data, {})
        provision.activate(self.data)
        self.assertEqual(before, (self.store / "identity/factory.json").read_bytes())
        self.assertEqual(read_json(self.store / "installation.json"), {"state": "imported-unqualified"})

    def test_activation_requires_library_and_recovery_resolution(self):
        atomic_json(self.store / "web/setup-complete.json", {"network_uuid": NETWORK, "playback_acl_uuid": self.factory["ids"]["playback_acl"]})
        with self.assertRaises(FileNotFoundError):
            provision.activate(self.data)
        atomic_json(self.store / "web/mux-rollback.json", {"uuid": MUX})
        with self.assertRaises(ValueError):
            provision.activate(self.data)
        self.assertEqual(read_json(self.store / "installation.json"), {"state": "unconfigured"})

    def test_fresh_boot_refuses_unmarked_and_foreign_data(self):
        other = self.root / "other-data"
        other.mkdir()
        with patch.object(persistent, "validate_mount"), self.assertRaises(ValueError):
            provision.boot(self.root, other)
        (self.root / "boot").mkdir()
        (self.root / "boot/satip-config.txt").write_text("hostname=test-pi")
        (other / ".satip-factory-empty").write_bytes(b"satip-fresh-data-v1\n")
        (other / "foreign").write_text("preserve")
        with patch.object(persistent, "validate_mount"), self.assertRaises(ValueError):
            provision.boot(self.root, other)
        self.assertFalse((other / "store").exists())
        self.assertEqual((other / "foreign").read_text(), "preserve")

    def test_factory_marked_empty_data_initializes_without_opt_in(self):
        other = self.root / "other-data"
        other.mkdir()
        (other / ".satip-factory-empty").write_bytes(b"satip-fresh-data-v1\n")
        with patch.object(persistent, "validate_mount"), patch.object(persistent, "prepare"), \
                patch.object(provision, "seed") as seed:
            provision.boot(self.root, other)
        self.assertEqual(read_json(other / "store/installation.json"), {"state": "unconfigured"})
        self.assertFalse((other / ".satip-factory-empty").exists())
        seed.assert_called_once_with(self.root, other, {})


def serialized(uuid, classname, **fields):
    return {"uuid": uuid, "class": classname, "params": [{"id": key, "value": value} for key, value in fields.items()]}


class FakeBackend:
    def __init__(self, factory):
        ids = factory["ids"]
        self.acls = [{"uuid": ids[role + "_acl"], "username": "satip-" + role, "enabled": True,
                      "prefix": "127.0.0.1/32", "admin": role == "admin", "webui": role == "admin",
                      "dvr": [], "streaming": ["basic"] if role == "playback" else [],
                      "profile": [ids["pass"]], "conn_limit": 1 if role == "playback" else 0,
                      "channel_min": 1000000, "channel_max": 3000000} for role in ("admin", "playback")]
        self.profiles = [{"key": ids["pass"], "val": "pass"}]
        self.frontends = {FRONTEND: False, OTHER: True}
        self.networks, self.muxes, self.channels, self.subscriptions, self.calls = [], [], [], [], []
        self.fail_restore = False

    async def grid(self, endpoint):
        return copy.deepcopy({"access/entry/grid": self.acls, "profile/list": self.profiles,
                              "status/subscriptions": self.subscriptions, "mpegts/network/grid": self.networks,
                              "mpegts/mux/grid": self.muxes, "channel/grid": self.channels,
                              "mpegts/service/grid": [{"uuid": SERVICE, "multiplex_uuid": MUX, "encrypted": False,
                                  "enabled": True, "svcname": "Fixture TV"}]}[endpoint])

    async def request(self, endpoint, write=False, **values):
        self.calls.append((endpoint, values))
        if endpoint == "hardware/satip/discover":
            return {}
        if endpoint == "hardware/tree":
            node = values["uuid"]
            if node == "root":
                return [serialized(SERVER, "satip_client", addr="192.168.1.10")]
            if node == SERVER:
                return [serialized(FRONTEND, "satip_frontend_dvbs", fe_number=2)]
            return [serialized("8" * 32, "satip_satconf", position=1)]
        if endpoint == "idnode/load":
            return {"entries": [serialized(key, "satip_frontend_dvbs", enabled=value) for key, value in self.frontends.items()]}
        if endpoint == "dvb/scanfile/list":
            return {"entries": [{"key": SCANFILE, "val": "Astra fixture"}]}
        if endpoint == "mpegts/network/create":
            self.networks.append({"uuid": NETWORK, **values["conf"]})
            return {"uuid": NETWORK}
        if endpoint == "channel/create":
            channel = {"uuid": CHANNEL, **values["conf"]}
            self.channels.append(channel)
            return {"uuid": CHANNEL}
        if endpoint == "service/streams":
            return {"streams": [{"type": "H264", "width": 1280, "height": 720},
                                {"type": "MPEG2AUDIO", "language": "ger", "audio_type": 0, "pid": 257}]}
        raise AssertionError(endpoint)

    async def save(self, **values):
        self.calls.append(("save", values))
        if values["uuid"] in self.frontends and "enabled" in values:
            self.frontends[values["uuid"]] = values["enabled"]
        if "scanfile" in values:
            self.muxes = [{"uuid": MUX, "network_uuid": NETWORK, "enabled": 0, "epg": 0, "scan_state": 0}]
        for node in [*self.acls, *self.muxes, *self.channels]:
            if node["uuid"] == values["uuid"]:
                node.update(values)


class SetupTests(FreshFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = FakeBackend(self.factory)
        self.preferences = read_json(self.store / "config/web/setup.json")
        self.setup = setup.Setup(self.backend, self.store, self.preferences, ["testserver"])

    async def configure(self):
        with patch.object(Manager, "scan", new=AsyncMock()):
            await self.setup.configure(SERVER, FRONTEND, SCANFILE)
            await self.setup.task

    async def test_discovery_uses_actual_tuner_and_network_identities(self):
        discovered = await self.setup.discover()
        self.assertEqual(discovered[0]["tuner_number"], 2)
        await self.configure()
        self.assertEqual(self.setup.state["network_uuid"], NETWORK)
        self.assertEqual(self.backend.frontends, {FRONTEND: True, OTHER: False})
        disable = next(i for i, (_, value) in enumerate(self.backend.calls) if value.get("uuid") == OTHER and value.get("enabled") is False)
        create = next(i for i, (endpoint, _) in enumerate(self.backend.calls) if endpoint == "mpegts/network/create")
        self.assertLess(disable, create)

    async def test_prepare_for_channel_restore_does_not_start_scan(self):
        with patch.object(self.setup, "run_scan", new=AsyncMock()) as scan:
            await self.setup.configure(SERVER, FRONTEND, SCANFILE, start_scan=False)
            scan.assert_not_called()
        self.assertIsNone(self.setup.task)
        self.assertTrue(read_json(self.setup.state_path)["prepared"])
        self.assertEqual(self.setup.state["network_uuid"], NETWORK)
        self.assertFalse((self.store / "management/library.json").exists())

    async def test_retry_reuses_network_and_refuses_changed_selection(self):
        await self.configure()
        await self.configure()
        self.assertEqual(len(self.backend.networks), 1)
        with self.assertRaises(ValueError):
            await self.setup.configure(SERVER, OTHER, SCANFILE)

    async def test_astra_list_is_automatic_and_reused_on_retry(self):
        with patch.object(Manager, "scan", new=AsyncMock()):
            await self.setup.configure(SERVER, FRONTEND)
            await self.setup.task
            self.assertEqual(self.setup.state["scanfile"], SCANFILE)
            await self.setup.configure(SERVER, FRONTEND)
            await self.setup.task
        self.assertEqual(len(self.backend.networks), 1)

    async def test_tuner_can_change_without_losing_network_or_channels(self):
        await self.configure()
        channels = {"channels": [{"service_uuid": SERVICE, "name": "Fixture TV"}]}
        atomic_json(self.store / "web/candidates.json", channels)
        # A preference is a default, not a prohibition on an explicit UI choice.
        self.preferences["settings"]["tuner_number"] = "2"
        alternative = [{"server_uuid": SERVER, "frontend_uuid": OTHER, "tuner_number": 1}]
        with patch.object(self.setup, "discover", new=AsyncMock(return_value=alternative)), \
             patch.object(Manager, "scan", new=AsyncMock()):
            await self.setup.configure(SERVER, OTHER)
            await self.setup.task
        self.assertEqual(self.setup.state["frontend_uuid"], OTHER)
        self.assertEqual(self.setup.state["network_uuid"], NETWORK)
        self.assertEqual(len(self.backend.networks), 1)
        self.assertEqual(self.backend.frontends, {FRONTEND: False, OTHER: True})
        self.assertEqual(read_json(self.store / "web/candidates.json"), channels)

    async def test_tuner_change_rejects_active_scan_recovery_and_subscriptions(self):
        await self.configure()
        original = self.setup.state.copy()
        with patch.object(self.setup, "task", new=Mock()) as task:
            task.done.return_value = False
            with self.assertRaises(BackendError):
                await self.setup.configure(SERVER, OTHER)
        atomic_json(self.store / "web/mux-rollback.json", {"uuid": MUX})
        with self.assertRaises(BackendError):
            await self.setup.configure(SERVER, OTHER)
        (self.store / "web/mux-rollback.json").unlink()
        alternative = [{"server_uuid": SERVER, "frontend_uuid": OTHER, "tuner_number": 1}]
        self.backend.subscriptions = [{"uuid": "other-client"}]
        with patch.object(self.setup, "discover", new=AsyncMock(return_value=alternative)):
            with self.assertRaises(BackendError):
                await self.setup.configure(SERVER, OTHER)
        self.assertEqual(self.setup.state, original)

    async def test_interrupted_tuner_preparation_blocks_lineup_publication(self):
        await self.configure()
        alternative = [{"server_uuid": SERVER, "frontend_uuid": OTHER, "tuner_number": 1}]
        with patch.object(self.setup, "discover", new=AsyncMock(return_value=alternative)), \
             patch.object(self.backend, "save", new=AsyncMock(side_effect=BackendError("fixture failure"))):
            with self.assertRaises(BackendError):
                await self.setup.configure(SERVER, OTHER)
        self.assertFalse(read_json(self.setup.state_path)["prepared"])
        with self.assertRaises(BackendError):
            await self.setup.finish([SERVICE])

    async def test_tuner_change_is_refused_after_lineup_is_saved(self):
        await self.configure()
        await self.setup.finish([SERVICE])
        with self.assertRaises(BackendError):
            await self.setup.configure(SERVER, OTHER)

    async def test_backend_scanfile_duplicates_are_removed(self):
        entry = {"key": SCANFILE, "val": "Astra"}
        with patch.object(self.backend, "request", new=AsyncMock(return_value={"entries": [entry, entry]})):
            self.assertEqual(await self.setup.scanfiles(), [entry])

    async def test_missing_astra_list_prevents_mutations(self):
        with patch.object(self.setup, "scanfiles", new=AsyncMock(return_value=[])):
            with self.assertRaises(ValueError):
                await self.setup.configure(SERVER, FRONTEND)
        self.assertFalse(self.backend.networks)

    async def test_competing_subscription_prevents_mutations(self):
        self.backend.subscriptions = [{"uuid": "other-client"}]
        with self.assertRaises(BackendError):
            await self.configure()
        self.assertFalse(self.backend.networks)

    async def test_unsafe_acl_and_profile_are_rejected(self):
        acl = self.backend.acls[1]
        for field, value in (("admin", True), ("streaming", ["basic", "advanced"]), ("prefix", "0.0.0.0/0"), ("conn_limit", 2), ("profile", [])):
            previous = acl[field]
            acl[field] = value
            with self.subTest(field=field), self.assertRaises(BackendError):
                await setup.verify_access(self.backend, acl["uuid"])
            acl[field] = previous

    async def test_setup_library_exception_is_not_available_to_normal_manager(self):
        await self.configure()
        self.assertEqual(self.setup.manager.current_library(), ({}, []))
        normal = Manager(self.backend, self.setup.manager.config)
        with self.assertRaises(FileNotFoundError):
            normal.current_library()

    async def test_first_lineup_publishes_valid_config_but_not_qualification(self):
        await self.configure()
        await self.setup.finish([SERVICE])
        data = read_json(self.store / "management/library.json")
        library(data)
        self.assertEqual(data["favourites"], [CHANNEL])
        self.assertFalse(read_json(self.store / "config/web/config.json")["require_auth"])
        self.assertEqual(read_json(self.store / "installation.json"), {"state": "unconfigured"})
        self.assertEqual(read_json(self.store / "web/setup-complete.json")["network_uuid"], NETWORK)

    async def test_pending_recovery_blocks_scan_and_lineup(self):
        await self.configure()
        atomic_json(self.store / "web/mux-rollback.json", {"uuid": MUX, "enabled": 0, "epg": 0, "scan_state": 0})
        with self.assertRaises(BackendError):
            await self.configure()
        with self.assertRaises(BackendError):
            await self.setup.finish([SERVICE])
        await self.setup.recover()
        self.assertFalse((self.store / "web/mux-rollback.json").exists())

    async def test_unknown_service_does_not_publish_completion(self):
        await self.configure()
        with self.assertRaises(ValueError):
            await self.setup.finish(["f" * 32])
        self.assertFalse((self.store / "web/setup-complete.json").exists())

    async def test_failed_restore_retains_journal_and_prevents_retry(self):
        await self.configure()
        journal = self.store / "web/mux-rollback.json"
        atomic_json(journal, {"uuid": MUX, "enabled": 0, "epg": 0, "scan_state": 0})
        with patch.object(self.backend, "save", new=AsyncMock(side_effect=BackendError("fixture failure"))):
            with self.assertRaises(BackendError):
                await self.setup.recover()
        self.assertTrue(journal.exists())
        with self.assertRaises(BackendError):
            await self.configure()

    async def test_preparing_state_cannot_finish_after_interruption(self):
        await self.configure()
        self.setup.state.pop("prepared")
        with self.assertRaises(BackendError):
            await self.setup.finish([SERVICE])


class SetupWebTests(FreshFixture):
    def test_stale_setup_routes_close_after_activation(self):
        with self.client() as client:
            atomic_json(self.store / "installation.json", {"state": "configured"})
            self.assertEqual(client.post("/api/setup/discover", json={}, headers={"x-satip-action": "1"}).status_code, 409)

    def test_restore_requires_origin_empty_payload_and_root_confirmation(self):
        path = "/api/setup/config/restore"
        with self.client() as client:
            self.assertEqual(client.post(path, json={}).status_code, 403)
            client.auth = ("satip", self.factory["login"]["password"])
            self.assertEqual(client.post(path, json={}).status_code, 403)
            client.headers["x-satip-action"] = "1"
            self.assertEqual(client.post(path, json={}, headers={"origin": "http://evil.invalid"}).status_code, 403)
            self.assertEqual(client.post(path, json={"path": "/etc/shadow"}).status_code, 422)
            from satip_appliance.config_restore import ConfigRestore
            worker = ConfigRestore(self.store)
            async def consume(_):
                worker.refresh()
            with patch.object(web.asyncio, "sleep", side_effect=consume), \
                    patch("satip_appliance.config_restore.restore") as restore:
                self.assertEqual(client.post(path, json={}).json(), {"state": "restored"})
                restore.assert_called_once()
                restore.side_effect = OSError("secret details")
                response = client.post(path, json={})
                self.assertEqual(response.status_code, 409)
                self.assertNotIn("secret details", response.text)
            with patch.object(web.asyncio, "sleep", new=AsyncMock()):
                self.assertEqual(client.post(path, json={}).status_code, 504)

    def client(self):
        patch.object(setup.discovery, "search_servers", return_value={"servers": [], "interfaces": [], "warnings": []}).start()
        app = web.create_setup_app(self.store, FakeBackend(self.factory), ["testserver"])
        return TestClient(app)

    def test_setup_health_is_local_only_and_setup_is_password_free(self):
        patch.object(setup.discovery, "search_servers", return_value={"servers": [], "interfaces": [], "warnings": []}).start()
        app = web.create_setup_app(self.store, FakeBackend(self.factory), ["testserver"])
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            self.assertEqual(client.get("/healthz").status_code, 200)
            self.assertEqual(client.get("/api/setup/status").status_code, 200)
        with TestClient(app, client=("192.168.1.2", 50000)) as client:
            self.assertEqual(client.get("/healthz").status_code, 404)

    def test_live_scan_journal_is_not_reported_as_recovery(self):
        with self.client() as client:
            client.auth = ("satip", self.factory["login"]["password"])
            instance = client.app.state.setup
            atomic_json(self.store / "web/mux-rollback.json", {"uuid": MUX})
            with patch.object(instance, "task") as task:
                task.done.return_value = False
                self.assertFalse(client.get("/api/setup/status").json()["recovery_pending"])
                task.done.return_value = True
                self.assertTrue(client.get("/api/setup/status").json()["recovery_pending"])
            self.assertTrue(client.get("/api/setup/status").json()["recovery_pending"])

    def test_scan_request_no_longer_requires_checkbox_or_mux_selection(self):
        selection = web.Selection(server_uuid=SERVER, frontend_uuid=FRONTEND)
        self.assertIsNone(selection.scanfile)
        self.assertTrue(selection.confirm_scan)

    def test_ui_works_without_uuid_library_or_login(self):
        with self.client() as client:
            self.assertEqual(client.get("/").status_code, 200)
            client.auth = ("satip", self.factory["login"]["password"])
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("first setup", response.text)
            self.assertEqual(client.get("/api/setup/status").json()["candidates"], [])
            self.assertEqual(client.get("/setup.js").status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertNotIn(self.factory["login"]["password"], response.text)

    def test_mutations_require_action_header_same_origin_and_valid_host(self):
        with self.client() as client:
            client.auth = ("satip", self.factory["login"]["password"])
            self.assertEqual(client.post("/api/setup/discover", json={}).status_code, 403)
            self.assertEqual(client.post("/api/setup/discover", json={}, headers={"x-satip-action": "1", "origin": "http://evil.invalid"}).status_code, 403)
            self.assertEqual(client.post("/api/setup/discover", json={}, headers={"x-satip-action": "1", "origin": "http://testserver"}).status_code, 200)
            self.assertEqual(client.get("/", headers={"host": "evil.invalid"}).status_code, 400)

    def test_payloads_are_bounded_and_unknown_fields_refused(self):
        with self.client() as client:
            client.auth = ("satip", self.factory["login"]["password"])
            client.headers["x-satip-action"] = "1"
            self.assertEqual(client.post("/api/setup/finish", content="x" * 32769).status_code, 413)
            response = client.post("/api/setup/scan", json={"server_uuid": SERVER, "frontend_uuid": FRONTEND,
                "scanfile": SCANFILE, "confirm_scan": True, "command": "arbitrary"})
            self.assertEqual(response.status_code, 422)

    def test_login_free_setup_needs_no_generated_credential(self):
        preference = read_json(self.store / "config/web/setup.json")
        preference["settings"]["lan_access"] = "login-free"
        atomic_json(self.store / "config/web/setup.json", preference)
        with self.client() as client:
            self.assertEqual(client.get("/api/setup/status").status_code, 200)


if __name__ == "__main__":
    unittest.main()
