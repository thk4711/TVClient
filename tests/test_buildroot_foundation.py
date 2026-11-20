import base64
import json
import io
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "buildroot/external/board/satip"


def load(name, path):
    module = types.ModuleType(name)
    exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
    return module


provision = load("dev_provision", BOARD / "rootfs-overlay/usr/sbin/satip-dev-provision")
config = load("foundation_config", ROOT / "buildroot/validate-config.py")
PUBLIC_KEY = "ssh-ed25519 " + base64.b64encode(
    b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))).decode()
PASSWORD_HASH = "$6$testsalt$" + "a" * 86


class DevProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "etc").mkdir()
        (self.root / "etc/shadow").write_text("root:*:1:0:99999:7:::\nother:*:1:0:99999:7:::\n")

    def test_generic_template_contains_no_access_identity(self):
        self.assertEqual(provision.parse_boot_config((BOARD / "satip-config.txt").read_text()),
                         {"hostname": "satip-dev"})

    def test_boot_file_allowlist_has_only_seven_parameters(self):
        values = {"hostname": "test-pi", "timezone": "Europe/Berlin",
                  "ssh_enabled": "yes", "root_password": "literal:$=password",
                  "wifi_country": "DE", "wifi_ssid": " Netzwerk ", "wifi_psk": " password "}
        self.assertEqual(provision.BOOT_KEYS, set(values))
        (self.root / "boot").mkdir()
        path = self.root / "boot/satip-config.txt"
        path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
        self.assertEqual(provision.read_config(path), values)

    def test_boot_file_rejects_every_legacy_key_without_exposing_values(self):
        (self.root / "boot").mkdir()
        path = self.root / "boot/satip-config.txt"
        for key in provision.KEYS - provision.BOOT_KEYS:
            with self.subTest(key=key):
                path.write_text(f"# comment\n{key}=private-obsolete-value\n")
                with self.assertRaises(provision.ConfigError) as raised:
                    provision.read_config(path)
                self.assertEqual(str(raised.exception), "config-unknown:2")
                with patch("sys.stderr", new_callable=io.StringIO) as log:
                    self.assertEqual(provision.main(self.root), 1)
                self.assertNotIn("private-obsolete-value", log.getvalue())
                self.assertFalse((self.root / "etc/satip-dev-provisioned").exists())
                self.assertEqual((self.root / "etc/shadow").read_text(),
                                 "root:*:1:0:99999:7:::\nother:*:1:0:99999:7:::\n")

    def test_boot_parameters_still_validate_before_writes(self):
        for text, code in (
                ("hostname=first\nhostname=second", "config-duplicate:2"),
                ("timezone=../etc", "config-value:1"),
                ("ssh_enabled=maybe", "config-value:1"),
                ("root_password=privät", "config-characters:1"),
                ("wifi_ssid=only", "config-wifi:1"),
                ("wifi_country=de\nwifi_ssid=test\nwifi_psk=longpassword", "config-value:1"),
                ("wifi_country=DE\nwifi_ssid=test\nwifi_psk=short", "config-value:3"),
                ("#" + "a" * 16384, "config-size:0")):
            with self.subTest(code=code), self.assertRaises(provision.ConfigError) as raised:
                provision.parse_boot_config(text)
            self.assertEqual(str(raised.exception), code)

    def test_saved_legacy_settings_and_key_access_survive_boot_restriction(self):
        (self.root / "run").mkdir()
        (self.root / "run/satip-data-ready").touch()
        (self.root / "boot").mkdir()
        (self.root / "boot/satip-config.txt").write_text("hostname=satip-dev")
        saved = self.root / "data/store/identity/development.txt"
        saved.parent.mkdir(parents=True)
        saved.write_text(f"hostname=legacy-pi\nssh_authorized_key={PUBLIC_KEY}\n"
                         f"recovery_password_hash={PASSWORD_HASH}\nsetup_password=legacy-password\n"
                         "lan_access=authenticated\ndrm_connector=HDMI-A-1\nsatip_source=2\n")
        with patch.object(provision.subprocess, "run"):
            self.assertEqual(provision.main(self.root), 0)
        self.assertEqual((self.root / "etc/hostname").read_text(), "legacy-pi\n")
        self.assertEqual((self.root / "root/.ssh/authorized_keys").read_text(), PUBLIC_KEY + "\n")
        self.assertEqual((self.root / "etc/satip-ssh-enabled").read_text(), "yes\n")
        self.assertEqual(provision.parse_config(saved.read_text())["satip_source"], "2")

    def test_public_key_and_console_password_modes(self):
        provision.provision(self.root, f"hostname=test-pi\nssh_authorized_key={PUBLIC_KEY} private-comment\n"
                            f"recovery_password_hash={PASSWORD_HASH}\n")
        key = self.root / "root/.ssh/authorized_keys"
        self.assertEqual(key.read_text(), PUBLIC_KEY + "\n")
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(key.parent.stat().st_mode & 0o777, 0o700)
        self.assertIn("root:" + PASSWORD_HASH + ":", (self.root / "etc/shadow").read_text())
        self.assertIn("other:*:", (self.root / "etc/shadow").read_text())
        self.assertEqual((self.root / "etc/shadow").stat().st_mode & 0o777, 0o600)

    def test_quoted_wifi_and_idempotent_retry(self):
        text = 'wifi_country=DE\nwifi_ssid=network \"name\nwifi_psk=test\\password\n'
        provision.provision(self.root, text)
        wifi = self.root / "etc/wpa_supplicant.conf"
        first = wifi.read_bytes()
        provision.provision(self.root, text)
        self.assertEqual(first, wifi.read_bytes())
        self.assertIn('country=DE', first.decode())
        self.assertEqual(wifi.stat().st_mode & 0o777, 0o600)

    def test_hex_psk_is_not_quoted(self):
        provision.provision(self.root, "wifi_country=DE\nwifi_ssid=test\nwifi_psk=" + "a" * 64)
        self.assertIn("psk=" + "a" * 64, (self.root / "etc/wpa_supplicant.conf").read_text())

    def test_passwords_have_no_length_policy(self):
        for password in ("x", "short", "a" * 256, 'literal:$="#\\value'):
            for key in ("setup_password", "root_password"):
                self.assertEqual(provision.parse_config(f"{key}={password}")[key], password)

    def test_config_diagnostics_include_reason_and_line_but_no_values(self):
        for text, code in (
                ("# comment\nsetup_password=x\nsetup_password=private", "config-duplicate:3"),
                ("# comment\nunknown-private=private", "config-unknown:2"),
                ("# comment\nprivate", "config-format:2"),
                ("# comment\nsetup_password=privät", "config-characters:2"),
                ("# comment\nssh_enabled=private", "config-value:2"),
                ("# comment\nwifi_ssid=private", "config-wifi:2"),
                ("satip_xml=http://[private", "config-value:1")):
            with self.subTest(code=code), self.assertRaises(provision.ConfigError) as raised:
                provision.parse_config(text)
            self.assertEqual(str(raised.exception), code)

    def test_failed_boot_publishes_actionable_secret_free_diagnostic(self):
        (self.root / "boot").mkdir()
        (self.root / "boot/satip-config.txt").write_text("root_password=x\nroot_password=private")
        with patch("sys.stderr", new_callable=io.StringIO) as log:
            self.assertEqual(provision.main(self.root), 1)
        self.assertEqual((self.root / "run/satip-boot-error").read_text(), "config-duplicate:2\n")
        self.assertNotIn("private", log.getvalue())
        self.assertFalse((self.root / "etc/satip-dev-provisioned").exists())

    def test_plaintext_root_password_is_hashed_via_stdin(self):
        with patch.object(provision.subprocess, "run", return_value=types.SimpleNamespace(stdout=PASSWORD_HASH + "\n")) as run:
            values = provision.provision(self.root, "ssh_enabled=yes\nroot_password=x")
        run.assert_called_once_with(
            ["/bin/busybox", "mkpasswd", "-m", "sha512", "-P", "0"],
            input="x\n", text=True, capture_output=True, check=True, timeout=10)
        self.assertNotIn("root_password", values)
        self.assertEqual(values["recovery_password_hash"], PASSWORD_HASH)
        self.assertIn("root:" + PASSWORD_HASH + ":", (self.root / "etc/shadow").read_text())
        self.assertEqual((self.root / "etc/satip-ssh-enabled").read_text(), "yes\n")
        self.assertFalse((self.root / "root/.ssh/authorized_keys").exists())

    def test_root_password_alone_does_not_enable_ssh_on_new_card(self):
        with patch.object(provision.subprocess, "run", return_value=types.SimpleNamespace(stdout=PASSWORD_HASH + "\n")):
            provision.provision(self.root, "root_password=x")
        self.assertEqual((self.root / "etc/satip-ssh-enabled").read_text(), "no\n")

    def test_hash_failure_does_not_write_credentials(self):
        before = (self.root / "etc/shadow").read_bytes()
        for error in (subprocess.CalledProcessError(1, "mkpasswd"), subprocess.TimeoutExpired("mkpasswd", 10)):
            with patch.object(provision.subprocess, "run", side_effect=error):
                with self.assertRaises(subprocess.SubprocessError):
                    provision.provision(self.root, "ssh_enabled=yes\nroot_password=x")
            self.assertEqual((self.root / "etc/shadow").read_bytes(), before)
            self.assertFalse((self.root / "etc/satip-dev-provisioned").exists())
        with patch.object(provision.subprocess, "run", return_value=types.SimpleNamespace(stdout="invalid")):
            with self.assertRaises(ValueError):
                provision.provision(self.root, "root_password=x")
        self.assertEqual((self.root / "etc/shadow").read_bytes(), before)

    def test_recovery_settings_can_change_after_first_boot_and_persist(self):
        (self.root / "run").mkdir()
        (self.root / "run/satip-data-ready").touch()
        (self.root / "boot").mkdir()
        config = self.root / "boot/satip-config.txt"
        saved = self.root / "data/store/identity/development.txt"
        config.write_text("hostname=test-pi\nssh_enabled=yes\nroot_password=x")
        with patch.object(provision.subprocess, "run", return_value=types.SimpleNamespace(stdout=PASSWORD_HASH + "\n")):
            self.assertEqual(provision.main(self.root), 0)
            self.assertNotIn("root_password=", saved.read_text())
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            config.write_text("hostname=satip-dev\nssh_enabled=no")
            self.assertEqual(provision.main(self.root), 0)
            self.assertEqual((self.root / "etc/hostname").read_text(), "test-pi\n")
            self.assertEqual((self.root / "etc/satip-ssh-enabled").read_text(), "no\n")
            config.unlink()
            self.assertEqual(provision.main(self.root), 0)
            self.assertEqual((self.root / "etc/satip-ssh-enabled").read_text(), "no\n")
            config.write_text("ssh_enabled=yes\nroot_password=y")
            new_hash = PASSWORD_HASH.replace("testsalt", "newsalt")
            with patch.object(provision.subprocess, "run", return_value=types.SimpleNamespace(stdout=new_hash + "\n")):
                self.assertEqual(provision.main(self.root), 0)
            config.unlink()
            self.assertEqual(provision.main(self.root), 0)
            self.assertEqual((self.root / "etc/satip-ssh-enabled").read_text(), "yes\n")
            self.assertIn(new_hash, (self.root / "etc/shadow").read_text())

    def test_explicit_ssh_disable_overrides_legacy_key(self):
        provision.provision(self.root, f"ssh_authorized_key={PUBLIC_KEY}\nssh_enabled=no")
        self.assertEqual((self.root / "etc/satip-ssh-enabled").read_text(), "no\n")

    def test_invalid_input_is_rejected_before_writes(self):
        invalid = ["hostname=$(touch /tmp/no)", "unknown=secret",
                   "hostname=first\nhostname=second", "wifi_ssid=only",
                   "wifi_country=DE\nwifi_ssid=test\nwifi_psk=short",
                   "ssh_authorized_key=ssh-ed25519 !!!",
                   "ssh_authorized_key=command=bad " + PUBLIC_KEY,
                   "recovery_password_hash=plaintext-password",
                   "ssh_enabled=maybe", "root_password=x\nrecovery_password_hash=" + PASSWORD_HASH,
                   "wifi_country=de\nwifi_ssid=test\nwifi_psk=longpassword",
                   "hostname=" + "a" * 64, "#" + "a" * 16384]
        for text in invalid:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    provision.provision(self.root, text)
                self.assertFalse((self.root / "etc/hostname").exists())
                self.assertFalse((self.root / "etc/satip-dev-provisioned").exists())

    def test_symlink_destination_does_not_modify_external_file(self):
        outside = self.root / "outside"
        outside.write_text("original")
        (self.root / "etc/hostname").symlink_to(outside)
        with self.assertRaises(ValueError):
            provision.provision(self.root, "hostname=test")
        self.assertEqual(outside.read_text(), "original")

    def test_interrupted_write_leaves_no_completion_and_can_retry(self):
        actual = provision.atomic_write
        def interrupted(root, relative, text, mode=0o600):
            if relative == "etc/wpa_supplicant.conf":
                raise OSError("injected failure")
            return actual(root, relative, text, mode)
        text = "wifi_country=DE\nwifi_ssid=test\nwifi_psk=longpassword"
        with patch.object(provision, "atomic_write", side_effect=interrupted):
            with self.assertRaises(OSError):
                provision.provision(self.root, text)
        self.assertFalse((self.root / "etc/satip-dev-provisioned").exists())
        provision.provision(self.root, text)
        self.assertTrue((self.root / "etc/satip-dev-provisioned").is_file())


class FoundationConfigTests(unittest.TestCase):
    def test_recovery_launcher_sets_its_own_pythonpath(self):
        # rcS executes each script separately. No PYTHONPATH from S25 survives.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "run").mkdir()
            package = root / "opt/satip/satip_appliance"
            package.mkdir(parents=True)
            (package / "__init__.py").touch()
            (package / "device_settings.py").write_text(
                "def recovery_values(root, current):\n    return current\n")
            (package / "config_restore.py").write_text("def cleanup_import(root): pass\n")
            for name in ("etc", "boot", "data/store/identity", "usr/sbin"):
                (root / name).mkdir(parents=True, exist_ok=True)
            (root / "run/satip-data-ready").touch()
            (root / "data/store/identity/development.txt").write_text("hostname=test-pi\n")
            helper = root / "usr/sbin/satip-dev-provision"
            source = (BOARD / "rootfs-overlay/usr/sbin/satip-dev-provision").read_text()
            source = source.replace("sys.exit(main())",
                "subprocess.run = lambda *args, **kwargs: None\n"
                f"    sys.exit(main(Path({str(root)!r})))")
            helper.write_text(source)
            script = root / "recovery-service"
            launcher = (BOARD / "rootfs-overlay/etc/init.d/S30satip-dev-provision").read_text()
            for prefix in ("/opt/", "/run/", "/usr/sbin/"):
                launcher = launcher.replace(prefix, str(root) + prefix)
            # Disable editable-install/site hooks so only the target package path works.
            launcher = launcher.replace("/usr/bin/python3", f'"{sys.executable}" -S')
            env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
            script.write_text("\n".join(line for line in launcher.splitlines()
                                        if not line.startswith("export PYTHONPATH=")) + "\n")
            broken = subprocess.run(["sh", str(script), "start"], env=env,
                                    capture_output=True, text=True)
            self.assertNotEqual(broken.returncode, 0)
            self.assertIn("ModuleNotFoundError", broken.stderr)
            self.assertFalse((root / "run/satip-dev-ready").exists())
            recovered = subprocess.run(["sh", str(script), "start"],
                                       env={**env, "PYTHONPATH": str(package.parent)},
                                       capture_output=True, text=True)
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertTrue((root / "run/satip-dev-ready").is_file())
            (root / "run/satip-boot-error").unlink()
            script.write_text(launcher)
            result = subprocess.run(["sh", str(script), "start"], env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "run/satip-dev-ready").is_file())
            self.assertEqual((root / "etc/hostname").read_text(), "test-pi\n")

    def test_post_build_removes_only_upstream_wifi_example(self):
        hook = (BOARD / "post-build.sh").read_text()
        code = hook.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream, destination = root / "upstream.conf", root / "target.conf"
            example = b"#ctrl_interface=/var/run/wpa_supplicant\nap_scan=1\n\nnetwork={\n  key_mgmt=NONE\n}\n"
            upstream.write_bytes(example)
            def run():
                return subprocess.run(["python3", "-", str(destination), str(upstream)],
                                      input=code, capture_output=True, text=True)
            destination.write_bytes(example.replace(b"#ctrl_interface", b"ctrl_interface"))
            self.assertEqual(run().returncode, 0)
            self.assertFalse(destination.exists())
            self.assertEqual(run().returncode, 0)
            destination.write_text("private-test-config")
            result = run()
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(destination.read_text(), "private-test-config")
            self.assertNotIn("private-test-config", result.stderr)
            destination.unlink()
            destination.symlink_to(upstream)
            self.assertNotEqual(run().returncode, 0)
            self.assertEqual(upstream.read_bytes(), example)

    def test_requested_selections_and_safety(self):
        requested = config.settings(ROOT / "buildroot/external/configs/satip_rpi4_64_defconfig")
        resolved = {**requested, "BR2_TOOLCHAIN_USES_GLIBC": "y"}
        self.assertEqual(config.validate(resolved, requested), [])
        for key in ("BR2_INIT_SYSTEMD", "BR2_TARGET_ENABLE_ROOT_LOGIN", "BR2_PACKAGE_FFMPEG_NONFREE"):
            self.assertTrue(config.validate({**resolved, key: "y"}, requested))
        self.assertTrue(config.validate({**resolved, "BR2_PACKAGE_EUDEV_MODULE_LOADING": "n"}, requested))

    def test_boot_contract_and_no_arbitrary_commands(self):
        line = (BOARD / "cmdline.txt").read_text().splitlines()
        self.assertEqual(len(line), 1)
        self.assertEqual(sum(arg.startswith("root=") for arg in line[0].split()), 1)
        for name in ("S30satip-dev-provision", "S40network", "S50dropbear"):
            path = BOARD / "rootfs-overlay/etc/init.d" / name
            subprocess.run(["sh", "-n", str(path)], check=True)
            self.assertNotIn("killall", path.read_text())
        ssh = (BOARD / "rootfs-overlay/etc/init.d/S50dropbear").read_text()
        self.assertIn("-R -j -k", ssh)
        self.assertIn("/run/satip-dev-ready", ssh)

    def test_ssh_service_requires_enable_and_allows_password_login_without_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "run").mkdir()
            (root / "etc").mkdir()
            keys = root / "run/satip-recovery-dropbear"
            keys.mkdir()
            (keys / "dropbear_ed25519_host_key").touch()
            binary = root / "bin"
            binary.mkdir()
            launch = root / "launch"
            stub = binary / "start-stop-daemon"
            stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{launch}"\n')
            stub.chmod(0o755)
            script = root / "ssh-service"
            source = (BOARD / "rootfs-overlay/etc/init.d/S50dropbear").read_text()
            for prefix in ("/run/", "/etc/", "/data/"):
                source = source.replace(prefix, str(root) + prefix)
            script.write_text(source)
            for ready, enabled, expected in ((False, "yes", False), (True, "no", False), (True, "yes", True)):
                if ready:
                    (root / "run/satip-dev-ready").touch()
                (root / "etc/satip-ssh-enabled").write_text(enabled + "\n")
                subprocess.run(["sh", str(script), "start"], check=True,
                               env={**os.environ, "PATH": str(binary) + ":" + os.environ["PATH"]})
                self.assertEqual(launch.exists(), expected)
            arguments = launch.read_text().splitlines()
            self.assertNotIn("-s", arguments)
            self.assertNotIn("-g", arguments)
            self.assertNotIn("-w", arguments)

    def test_metadata_handles_unborn_git_and_excludes_build_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo, output = root / "repo", root / "output"
            repo.mkdir(); output.mkdir()
            inventory = runpy.run_path(str(ROOT / "scripts/source_manifest.py"))
            for name in inventory["build_inputs"](ROOT):
                destination = repo / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, destination)
            (repo / "buildroot/output").mkdir()
            (repo / "buildroot/output/secret.txt").write_text("private")
            (output / ".config").write_text("BR2_aarch64=y\n")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["python3", str(repo / "buildroot/build-metadata.py"),
                            str(repo), str(repo), str(output)], check=True)
            data = json.loads((output / "satip-build.json").read_text())
            self.assertIsNone(data["source_commit"])
            self.assertTrue(data["source_dirty"])
            self.assertFalse(data["release"])
            self.assertNotIn("buildroot/output/secret.txt", data["build_inputs"])


class RecoveryPackagingTests(unittest.TestCase):
    def test_watchdog_inputs_and_lifecycle_are_included(self):
        self.assertIn("CONFIG_BCM2835_WDT=y", (BOARD / "linux-devices.fragment").read_text())
        self.assertIn("CONFIG_WATCHDOG=y", (BOARD / "busybox.fragment").read_text())
        self.assertIn("dtparam=watchdog=on", (BOARD / "config.txt").read_text())
        service = BOARD / "rootfs-overlay/etc/init.d/S89satip-watchdog"
        self.assertTrue(service.stat().st_mode & 0o111)
        self.assertIn("satip_appliance.watchdog start", service.read_text())
        post_build = (BOARD / "post-build.sh").read_text()
        self.assertIn("S89satip-watchdog", post_build)
        self.assertIn("CONFIG_BCM2835_WDT=y", post_build)
        self.assertIn("CONFIG_WATCHDOG_NOWAYOUT=y", post_build)


if __name__ == "__main__":
    unittest.main()
