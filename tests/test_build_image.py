import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import os
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_image", ROOT / "scripts/build_image.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
INVENTORY = runpy.run_path(str(ROOT / "scripts/source_manifest.py"))


class BuildImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        # Developer-local settings must not change test defaults or execution mode.
        config = patch.object(build, "CONFIG", self.root / "absent.ini")
        config.start()
        self.addCleanup(config.stop)

    def repository(self, name="repo"):
        """Copy the reviewed inputs, never local caches or private evidence."""
        repo = self.root / name
        for relative in INVENTORY["BUILD_FILES"]:
            target = repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        return repo

    def config(self, text):
        path = self.root / "settings.ini"
        path.write_text(text)
        return ["--config", str(path)]

    def test_ini_cli_precedence_and_boolean(self):
        args = build.parse_args(self.config("[build_image]\njobs=8\nzip_image=no\n") + ["--jobs", "16", "--zip"])
        self.assertEqual(args.jobs, 16)
        self.assertTrue(args.zip_image)
        args = build.parse_args(self.config("[build_image]\nexecution=remote\nhost=papa@example\nremote_root=/home/papa/embedded/satip\n"))
        self.assertEqual(args.output_dir, "/home/papa/embedded/satip/output/rpi4-64")
        self.assertEqual(args.download_cache, "/home/papa/embedded/dl")

    def test_bad_ini_is_rejected(self):
        for text in ["[build_image]\nunknown=1\n", "[other]\njobs=1\n",
                     "[DEFAULT]\njobs=1\n[build_image]\n", "[build_image]\nzip_image=perhaps\n",
                     "[build_image]\ninstall_host_packages=perhaps\n"]:
            with self.subTest(text=text), self.assertRaises(build.BuildError):
                build.parse_args(self.config(text))

    def test_host_packages_ini_cli_precedence_and_default(self):
        self.assertFalse(build.parse_args(self.config("[build_image]\n")).install_host_packages)
        for value in ("true", "yes", "on", "1"):
            with self.subTest(value=value):
                argv = self.config(f"[build_image]\ninstall_host_packages={value}\n")
                self.assertTrue(build.parse_args(argv).install_host_packages)
                self.assertFalse(build.parse_args(argv + ["--no-apt"]).install_host_packages)
        for value in ("false", "no", "off", "0"):
            with self.subTest(value=value):
                argv = self.config(f"[build_image]\ninstall_host_packages={value}\n")
                self.assertFalse(build.parse_args(argv).install_host_packages)
                self.assertTrue(build.parse_args(argv + ["--install-host-packages"]).install_host_packages)

    def host_args(self, execution="local"):
        return build.parse_args(self.config("[build_image]\n") + [
            "--execution", execution, "--host", "builder@example",
            "--remote-root", "/home/builder/satip", "--install-host-packages"])

    def test_host_packages_run_on_selected_host_after_platform_check(self):
        for execution in ("local", "remote"):
            with self.subTest(execution=execution), patch.object(build, "run") as run, \
                    patch.object(build, "ssh") as ssh:
                args = self.host_args(execution)
                build.prepare_host(args)
                calls = (ssh if execution == "remote" else run).call_args_list
                (run if execution == "remote" else ssh).assert_not_called()
                self.assertEqual(len(calls), 3)
                commands = [call.args[1] if execution == "remote" else shlex.join(call.args[0])
                            for call in calls]
                self.assertIn("native amd64 Debian required", commands[0])
                self.assertIn("sudo -n", commands[1])
                self.assertIn("DEBIAN_FRONTEND=noninteractive", commands[1])
                self.assertIn("install -y", commands[1])
                for package in build.HOST_PACKAGES:
                    self.assertIn(package, commands[1])
                self.assertIn("missing build tools", commands[2])

    def test_host_packages_disabled_only_checks_host(self):
        args = self.host_args()
        args.install_host_packages = False
        with patch.object(build, "run") as run:
            build.prepare_host(args)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(call.args[0][0] == "python3" for call in run.call_args_list))

    def test_platform_or_install_failure_stops_preparation(self):
        for failures, count in (([build.BuildError("unsupported host")], 1),
                                (["", build.BuildError("apt failed")], 2)):
            with self.subTest(count=count), patch.object(build, "run", side_effect=failures) as run:
                with self.assertRaises(build.BuildError):
                    build.prepare_host(self.host_args())
                self.assertEqual(run.call_count, count)

    def test_install_script_with_fake_apt_and_sudo(self):
        with patch.object(build, "run") as run:
            build.prepare_host(self.host_args())
        command = run.call_args_list[1].args[0]
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        scripts = {
            "id": 'printf "%s\\n" "$FAKE_UID"',
            "sudo": 'printf "%s\\n" "$*" >> "$SUDO_LOG"\n'
                    '[ "$SUDO_FAIL" = 0 ] || exit 1\n'
                    '[ "$1" = -n ] || exit 2\nshift\nexec "$@"',
            "apt-get": '[ "$DEBIAN_FRONTEND" = noninteractive ] || exit 2\n'
                       'printf "%s\\n" "$*" >> "$APT_LOG"\n'
                       '[ "$APT_FAIL" != "$1" ] || exit 1',
        }
        for name, script in scripts.items():
            path = fake_bin / name
            path.write_text("#!/bin/sh\n" + script + "\n")
            path.chmod(0o755)
        for index, (uid, sudo_fail, apt_fail, success) in enumerate((
                ("0", "0", "", True), ("1000", "0", "", True),
                ("1000", "1", "", False), ("1000", "0", "update", False),
                ("1000", "0", "install", False))):
            with self.subTest(uid=uid, sudo_fail=sudo_fail, apt_fail=apt_fail):
                apt_log = self.root / f"apt-{index}.log"
                sudo_log = self.root / f"sudo-{index}.log"
                env = {**os.environ, "PATH": str(fake_bin) + os.pathsep + os.defpath,
                       "FAKE_UID": uid, "SUDO_FAIL": sudo_fail, "APT_FAIL": apt_fail,
                       "APT_LOG": str(apt_log), "SUDO_LOG": str(sudo_log)}
                result = subprocess.run(command, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, success, result.stderr)
                if success:
                    self.assertEqual(apt_log.read_text().splitlines(), [
                        "update", "install -y " + " ".join(build.HOST_PACKAGES)])
                if uid == "0":
                    self.assertFalse(sudo_log.exists())
                if sudo_fail == "1":
                    self.assertFalse(apt_log.exists())
                    self.assertIn("non-interactive sudo", result.stderr)
                if apt_fail == "update":
                    self.assertEqual(apt_log.read_text().splitlines(), ["update"])

    def test_originating_git_identity_is_bound_to_inputs(self):
        expected = {"example": "a" * 64}
        commit = "b" * 40
        with patch.object(build.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, commit + "\n"),
                subprocess.CompletedProcess([], 0, "")]):
            identity = build.source_identity(self.root, expected)
        self.assertEqual(identity["source_commit"], commit)
        self.assertIs(identity["source_dirty"], False)
        self.assertEqual(identity["build_inputs_sha256"], hashlib.sha256(
            json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        with patch.object(build.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "")):
            unknown = build.source_identity(self.root, expected)
        self.assertIsNone(unknown["source_commit"])
        self.assertIsNone(unknown["source_dirty"])

    def test_staged_metadata_rejects_invalid_or_mismatched_origin(self):
        module = __import__("runpy").run_path(str(ROOT / "buildroot/build-metadata.py"))
        identity = {"source_commit": "a" * 40, "source_dirty": False,
                    "build_inputs_sha256": "b" * 64}
        validate = module["source_identity"]
        self.assertEqual(validate(json.dumps(identity), "b" * 64), identity)
        for changes in ({"source_commit": "bad"}, {"source_dirty": 0},
                        {"build_inputs_sha256": "c" * 64}, {"extra": "refused"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate(json.dumps({**identity, **changes}), "b" * 64)
        with self.assertRaises(ValueError):
            validate("[]", "b" * 64)

    def test_bad_arguments_paths_and_overlap(self):
        base = ["--execution", "remote", "--host", "papa@example", "--remote-root", "/home/papa/satip"]
        for extra in [["--jobs", "0"], ["--ssh-port", "65536"], ["--host", "-bad"],
                      ["--host", "user@host;touch"], ["--output-dir", "/"],
                      ["--output-dir", "/home/a b"], ["--output-dir", "/home/a/../b"],
                      ["--output-dir", "/home/papa/satip/source/output"]]:
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises((build.BuildError, SystemExit)):
                    build.parse_args(base + extra)

    def test_dry_run_is_read_only(self):
        repo = self.repository()
        with patch.object(build, "run", side_effect=AssertionError("command executed")), \
                patch.object(build, "guard", side_effect=AssertionError("lock created")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(build.main(["--dry-run", "--install-host-packages", "--repository", str(repo),
                                         "--artifacts-dir", str(self.root / "absent")]), 0)
        self.assertFalse((self.root / "absent").exists())

    def test_device_hotfixes_are_in_image_inputs(self):
        actual = build.inputs(self.repository())
        for name in (
                "satip_appliance/discovery.py", "satip_appliance/registration.py",
                "satip_appliance/setup.py", "satip_appliance/config_restore.py",
                "satip_appliance/provision.py", "satip_appliance/supervisor.py",
                "satip_controller/static/setup.js", "satip_controller/static/setup.html",
                "satip_controller/scan.py", "satip_controller/player.py",
                "satip_controller/mpv_ipc.py", "satip_controller/controller.py",
                "buildroot/external/patches/ffmpeg/0001-rpi-v4l2-m2m.patch",
                "buildroot/external/patches/ffmpeg/rpi-v4l2-m2m.json"):
            self.assertEqual(actual[name], build.sha256(ROOT / name))
        manifest = json.loads((ROOT / "buildroot/external/patches/ffmpeg/rpi-v4l2-m2m.json").read_text())
        self.assertEqual(manifest["patch_sha256"], actual["buildroot/external/patches/ffmpeg/0001-rpi-v4l2-m2m.patch"])
        config = (ROOT / "buildroot/external/configs/satip_rpi4_64_defconfig").read_text()
        self.assertIn('$(BR2_EXTERNAL_SATIP_PATH)/patches', config)
        dependencies = (ROOT / "buildroot/external/package/satip-app/Config.in").read_text()
        for key in ("BR2_PACKAGE_PYTHON3_PYEXPAT", "BR2_PACKAGE_PYTHON3_ZLIB"):
            self.assertIn(key + "=y", config)
            self.assertIn("select " + key, dependencies)

    def test_inputs_exclude_private_files_and_reject_embedded_credentials(self):
        repo = self.repository()
        (repo / ".baseline-private").mkdir()
        (repo / ".baseline-private/secret").write_text("secret")
        actual = build.inputs(repo)
        self.assertFalse(any("secret" in name for name in actual))
        self.assertIn("buildroot/README.md", actual)
        overlay = repo / "buildroot/external/board/satip/rootfs-overlay"
        (overlay / "root/.ssh").mkdir(parents=True)
        key = overlay / "root/.ssh/authorized_keys"
        key.write_text("private-test-identity")
        with self.assertRaises(build.BuildError):
            build.inputs(repo)

    def test_inventory_rejects_exports_generated_files_and_unreviewed_sources_before_reading(self):
        overlay = "buildroot/external/board/satip/rootfs-overlay/"
        candidates = [
            overlay + "etc/shadow", overlay + "etc/ssh/ssh_host_ed25519_key",
            overlay + "data/store/identity/factory.json", overlay + "etc/.env",
            overlay + "etc/wpa_supplicant.conf", overlay + "usr/sbin/unreviewed-helper",
            overlay + "usr/sbin/__pycache__/satip-dev-provision.pyc",
            overlay + "etc/.DS_Store",
            "buildroot/external/board/satip/credentials.json",
            "buildroot/external/package/satip-splash/src/generated.c",
            "buildroot/external/patches/ffmpeg/backup.tar.gz",
            "satip_controller/static/cookies.txt", "satip_controller/local_credentials.py",
            "satip_appliance/tuning.py.bak", "satip_controller/player.pyc",
            "satip_controller/__pycache__/credentials.json",
            overlay + "usr/sbin/__pycache__/unreviewed.cpython-314.pyc",
            "satip_controller/__pycache__/player.cpython-314.pyc/private",
            "doc/diagnostics.md", "doc/images/private.png", "doc/history/private.md",
        ]
        marker = "synthetic-content-must-not-be-read-or-reported"
        for index, relative in enumerate(candidates):
            with self.subTest(path=relative):
                repo = self.repository(f"candidate-{index}")
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(marker)
                with patch.object(Path, "read_bytes", side_effect=AssertionError("content read before validation")):
                    with self.assertRaises(build.BuildError) as raised:
                        build.inputs(repo)
                self.assertNotIn(marker, str(raised.exception))

    def test_inventory_rejects_missing_files_empty_directories_and_nonregular_entries(self):
        repo = self.repository()
        missing = repo / "LICENSE"
        missing.unlink()
        with self.assertRaises(build.BuildError):
            build.inputs(repo)
        shutil.copyfile(ROOT / "LICENSE", missing)
        extra = repo / "buildroot/external/board/satip/rootfs-overlay/empty-export"
        extra.mkdir()
        with self.assertRaises(build.BuildError):
            build.inputs(repo)
        extra.rmdir()
        fifo = repo / "satip_controller/static/cookies.txt"
        os.mkfifo(fifo)
        with self.assertRaises(build.BuildError):
            build.inputs(repo)

    def test_inventory_never_follows_source_or_cache_links(self):
        for index, relative in enumerate((
                "buildroot/external/board", "satip_controller",
                "satip_appliance", "doc/images", "scripts")):
            with self.subTest(path=relative):
                repo = self.repository(f"linked-{index}")
                path = repo / relative
                outside = self.root / f"outside-{index}"
                path.rename(outside)
                path.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(build.BuildError):
                    build.inputs(repo)
        repo = self.repository()
        cache = repo / "satip_controller/__pycache__"
        cache.symlink_to(self.root / "missing", target_is_directory=True)
        with self.assertRaises(build.BuildError):
            build.inputs(repo)
        cache.unlink()
        cache.mkdir()
        (cache / "player.cpython-314.pyc").symlink_to(self.root / "missing")
        with self.assertRaises(build.BuildError):
            build.inputs(repo)

    def test_inventory_excludes_local_caches_and_unrelated_private_roots(self):
        repo = self.repository()
        before = build.inputs(repo)
        (repo / "doc/history").mkdir()
        for relative in ("satip_controller/__pycache__/player.cpython-314.pyc",
                         "satip_controller/__pycache__/renamed_module.cpython-313.pyc",
                         "buildroot/external/board/satip/__pycache__/install-hdmi-edid.cpython-314.pyc",
                         "buildroot/external/board/satip/rootfs-overlay/usr/sbin/__pycache__/satip-dev-provisioncpython-314.pyc",
                         "satip_controller/.DS_Store", ".baseline-private/snapshot.json",
                         "data/store/identity/factory.json", "cookies.txt"):
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic-private-data")
        self.assertEqual(build.inputs(repo), before)

    def test_inventory_rejects_customized_provisioning_without_echoing_values(self):
        repo = self.repository()
        path = repo / "buildroot/external/board/satip/satip-config.txt"
        for text in ("hostname=satip-dev\nroot_password=synthetic-private-value\n",
                     "synthetic-private-value=unknown\n"):
            path.write_text(text)
            with self.assertRaises(build.BuildError) as raised:
                build.inputs(repo)
            self.assertNotIn("synthetic-private-value", str(raised.exception))

    def test_staged_metadata_has_identical_inventory_and_rejects_contamination(self):
        repo = self.repository()
        output = self.root / "metadata"
        output.mkdir()
        (output / ".config").write_text("BR2_aarch64=y\n")
        command = [sys.executable, str(repo / "buildroot/build-metadata.py"),
                   str(repo), str(repo), str(output)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = output / "satip-build.json"
        files = json.loads(metadata.read_text())["build_inputs"]
        self.assertEqual(files, build.inputs(repo))
        self.assertIn("scripts/source_manifest.py", files)
        self.assertIn("examples/README.md", files)
        documentation = runpy.run_path(str(repo / "buildroot/external/board/satip/install-documentation.py"))
        for path in documentation["documentation_sources"](repo):
            self.assertIn(path.relative_to(repo).as_posix(), files)
        self.assertFalse(any(name.startswith("deploy/") for name in files))
        metadata.unlink()
        path = repo / "buildroot/external/board/satip/rootfs-overlay/etc/shadow"
        path.write_text("synthetic-private-value")
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("synthetic-private-value", result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(metadata.exists())

    def test_local_image_metadata_requires_cleanup_of_copied_overlay_caches(self):
        repo = self.repository()
        output = self.root / "output"
        copied = output / "target/usr/sbin/__pycache__/satip-dev-provisioncpython-314.pyc"
        copied.parent.mkdir(parents=True)
        copied.write_bytes(b"synthetic-workstation-bytecode")
        (output / ".config").write_text("BR2_aarch64=y\n")
        result = subprocess.run(
            [sys.executable, str(repo / "buildroot/build-metadata.py"),
             str(repo), str(repo), str(output)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("remove the source overlay cache", result.stderr)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", result.stderr)
        self.assertNotIn("synthetic-workstation-bytecode", result.stderr)
        self.assertFalse((output / "satip-build.json").exists())
        self.assertTrue(copied.exists())

    def guard_request(self):
        return {"action": "acquire", "locks": [str(self.root / "lock")], "token": "test-token",
                "paths": [str(self.root / "output")], "output": str(self.root / "output"),
                "output_owner": "test-owner"}

    def call_guard(self, request):
        return subprocess.run([sys.executable, "-c", build.GUARD, json.dumps(request)],
                              capture_output=True, text=True)

    def release(self, request):
        result = self.call_guard({**request, "action": "release"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_locks_exclude_concurrent_build_and_preserve_owner(self):
        request = self.guard_request()
        self.assertEqual(self.call_guard(request).returncode, 0)
        self.assertNotEqual(self.call_guard({**request, "token": "foreign"}).returncode, 0)
        self.assertEqual((self.root / "lock/token").read_text(), "test-token")
        self.assertNotEqual(self.call_guard({**request, "action": "release", "token": "foreign"}).returncode, 0)
        self.release(request)
        self.assertFalse((self.root / "lock").exists())

    def test_unowned_output_and_linked_path_refused(self):
        request = self.guard_request()
        output = self.root / "output"
        output.mkdir()
        sentinel = output / "foreign-marker"
        sentinel.write_text("preserve")
        self.assertNotEqual(self.call_guard(request).returncode, 0)
        self.assertEqual(sentinel.read_text(), "preserve")
        self.assertFalse((self.root / "lock").exists())
        request["paths"] = [str(self.root / "linked")]
        (self.root / "linked").symlink_to(output)
        self.assertNotEqual(self.call_guard(request).returncode, 0)

    def test_source_adoption_requires_exact_hashes_and_no_foreign_files(self):
        source = self.root / "source"
        (source / "buildroot").mkdir(parents=True)
        file = source / "buildroot/build.sh"
        file.write_text("test")
        request = {**self.guard_request(), "source": str(source), "owner": "repo",
                   "inputs": {"buildroot/build.sh": build.sha256(file)}, "adopt": False}
        self.assertNotEqual(self.call_guard(request).returncode, 0)
        request["adopt"] = True
        self.assertEqual(self.call_guard(request).returncode, 0)
        self.release(request)
        file.write_text("updated")
        request["adopt"] = False
        self.assertEqual(self.call_guard(request).returncode, 0)
        self.release(request)
        (source / "foreign").write_text("preserve")
        self.assertNotEqual(self.call_guard(request).returncode, 0)
        self.assertEqual((source / "foreign").read_text(), "preserve")

    def artifacts(self):
        expected = {"buildroot/example": "a" * 64}
        (self.root / "resolved.config").write_text("test-config")
        metadata = {"product": "satip", "release": False, "buildroot_commit": build.PIN,
                    "hardware": "rpi4-aarch64", "profile": "development-foundation",
                    "resolved_config_sha256": build.sha256(self.root / "resolved.config"),
                    "build_inputs": expected, "build_inputs_sha256": hashlib.sha256(
                        json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
        for name in build.ARTIFACTS:
            (self.root / name).write_bytes(b"fake-artifact")
        (self.root / "satip-build.json").write_text(json.dumps(metadata))
        self.sums()
        return expected

    def sums(self):
        (self.root / "SHA256SUMS").write_text("".join(
            f"{build.sha256(self.root / name)}  {name}\n" for name in build.ARTIFACTS))

    def test_artifact_checksums_and_metadata(self):
        expected = self.artifacts()
        self.assertEqual(set(build.verify(self.root, expected)), set(build.ARTIFACTS))
        (self.root / "Image").write_bytes(b"corrupt")
        with self.assertRaises(build.BuildError):
            build.verify(self.root, expected)
        self.sums()
        with self.assertRaises(build.BuildError):
            build.verify(self.root, {"other": "b" * 64})
        manifest = self.root / "SHA256SUMS"
        manifest.write_text(manifest.read_text() + "a" * 64 + "  ../secret\n")
        with self.assertRaises(build.BuildError):
            build.verify(self.root, expected)

    def test_zip_integrity_and_no_overwrite(self):
        (self.root / "sdcard.img").write_bytes(b"test-image" * 100)
        build.package_image(self.root, build.sha256(self.root / "sdcard.img"))
        self.assertTrue((self.root / "ZIP_SHA256SUMS").exists())
        with self.assertRaises(FileExistsError):
            build.package_image(self.root, build.sha256(self.root / "sdcard.img"))

    def test_zip_only_artifacts_are_verified_without_extraction(self):
        expected = self.artifacts()
        raw_digest = build.sha256(self.root / "sdcard.img")
        build.package_image(self.root, raw_digest)
        (self.root / "sdcard.img").unlink()
        sums = build.verify(self.root, expected, zip_image=True)
        self.assertNotIn("sdcard.img", sums)
        self.assertEqual(sums["sdcard.img.zip"], build.sha256(self.root / "sdcard.img.zip"))
        self.assertFalse((self.root / "sdcard.img").exists())
        (self.root / "sdcard.img.zip").write_bytes(b"corrupt")
        with self.assertRaisesRegex(build.BuildError, "checksum mismatch"):
            build.verify(self.root, expected, zip_image=True)

    def test_zip_member_digest_inventory_and_crc_are_checked(self):
        import zipfile
        archive = self.root / "sdcard.img.zip"
        digest = hashlib.sha256(b"expected-image").hexdigest()
        for names in (("sdcard.img",), ("other.img",), ("sdcard.img", "extra")):
            with self.subTest(names=names):
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zipped:
                    for name in names:
                        zipped.writestr(name, b"wrong-image")
                with self.assertRaises(build.BuildError):
                    build.verify_zip(archive, digest)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zipped:
            zipped.writestr("sdcard.img", b"expected-image")
        archive.write_bytes(archive.read_bytes().replace(b"expected-image", b"modified-image"))
        with self.assertRaises(zipfile.BadZipFile):
            build.verify_zip(archive, digest)

    def test_artifact_origin_must_match_local_commit(self):
        expected = self.artifacts()
        identity = {"source_commit": "a" * 40, "source_dirty": False,
                    "build_inputs_sha256": json.loads((self.root / "satip-build.json").read_text())["build_inputs_sha256"]}
        with self.assertRaises(build.BuildError):
            build.verify(self.root, expected, identity)
        path = self.root / "satip-build.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), **identity}))
        self.sums()
        build.verify(self.root, expected, identity)
        with self.assertRaises(build.BuildError):
            build.verify(self.root, expected, {**identity, "source_dirty": True})

    def test_ssh_uses_no_stdin_and_nonstandard_port(self):
        args = build.parse_args(["--execution", "remote", "--host", "papa@example",
                                 "--remote-root", "/home/papa/satip", "--ssh-port", "2323"])
        with patch.object(build, "run") as run:
            build.ssh(args, "printf test")
        argv = run.call_args.args[0]
        self.assertIn("-n", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertEqual(argv[argv.index("-p") + 1], "2323")
        self.assertEqual(argv[-1], "printf test")

    def fake_build(self, argv, *, capture=False, log=None, env=None):
        if len(argv) > 2 and argv[2] == build.GUARD:
            result = subprocess.run(argv, capture_output=True, text=True)
            if result.returncode:
                raise build.BuildError(result.stderr)
            return result.stdout
        if log is None:
            return ""  # Simulated native-host preflight.
        out = Path(env["SATIP_OUTPUT_DIR"])
        images = out / "images"
        images.mkdir(parents=True)
        expected = build.inputs(Path(argv[0]).parents[1])
        (out / ".config").write_text("fake-config")
        metadata = {"product": "satip", "hardware": "rpi4-aarch64",
                    "profile": "development-foundation", "release": False,
                    "buildroot_commit": build.PIN, "build_inputs": expected,
                    "build_inputs_sha256": hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                    "resolved_config_sha256": build.sha256(out / ".config")}
        metadata.update(json.loads(env["SATIP_SOURCE_IDENTITY"]))
        for name in build.ARTIFACTS:
            (images / name).write_bytes(b"fake-image")
        (images / "satip-build.json").write_text(json.dumps(metadata))
        (images / "rootfs.ext4").rename(images / "rootfs.ext2")
        (images / "rootfs.ext4").symlink_to("rootfs.ext2")
        (images / "SHA256SUMS").write_text("".join(f"{build.sha256(images / name)}  {name}\n" for name in build.ARTIFACTS))
        Path(log).write_text("simulated build complete\n")
        return ""

    def local_args(self):
        repo = self.repository()
        return ["--repository", str(repo), "--output-dir", str(self.root / "output"),
                "--buildroot-dir", str(self.root / "upstream"), "--download-cache", str(self.root / "dl"),
                "--artifacts-dir", str(self.root / "artifacts")]

    def test_local_orchestration_copies_symlink_and_verifies_zip(self):
        args = self.local_args()
        with patch.object(build, "run", side_effect=self.fake_build), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(build.main(args), 0)
        result, = (self.root / "artifacts").iterdir()
        self.assertTrue((result / "VERIFIED.json").exists())
        self.assertTrue((result / "sdcard.img.zip").exists())
        self.assertFalse((result / "sdcard.img").exists())
        self.assertTrue((self.root / "output/images/sdcard.img").exists())
        manifest = build.read_checksums(result / "SHA256SUMS",
                                        ("sdcard.img.zip", *build.ARTIFACTS[1:]))
        self.assertEqual(manifest, json.loads((result / "VERIFIED.json").read_text())["artifacts"])
        build.verify(result, build.inputs(Path(args[1])), zip_image=True)
        self.assertFalse((result / "rootfs.ext4").is_symlink())
        self.assertFalse((self.root / ".output.satip-build.lock").exists())
        self.assertFalse((self.root / ".repo.satip-source.lock").exists())

    def test_local_no_zip_collects_raw_image_only(self):
        args = self.local_args() + ["--no-zip"]
        with patch.object(build, "run", side_effect=self.fake_build), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(build.main(args), 0)
        result, = (self.root / "artifacts").iterdir()
        self.assertTrue((result / "sdcard.img").exists())
        self.assertFalse((result / "sdcard.img.zip").exists())
        self.assertFalse((result / "ZIP_SHA256SUMS").exists())
        build.verify(result, build.inputs(Path(args[1])))

    def test_remote_transfers_only_selected_image_format(self):
        for zip_image in (True, False):
            with self.subTest(zip_image=zip_image):
                root = self.root / str(zip_image)
                repo = self.repository("repo-" + str(zip_image))
                remote_root = root / "remote"
                output = remote_root / "output/rpi4-64"
                args = ["--execution", "remote", "--host", "builder@example",
                        "--repository", str(repo), "--remote-root", str(remote_root),
                        "--artifacts-dir", str(root / "artifacts"),
                        "--zip" if zip_image else "--no-zip"]
                transferred = []
                def remote(args, command, **kwargs):
                    parts = shlex.split(command)
                    if kwargs.get("log"):
                        environment = dict(part.split("=", 1) for part in parts[1:parts.index("python3")])
                        return self.fake_build([str(repo / "buildroot/build.sh"), "--jobs", "16"],
                                               env=environment, log=kwargs["log"])
                    if parts[:2] == ["python3", "-c"] and ("destination.mkdir()" in parts[2] or "p.rmdir()" in parts[2]):
                        result = subprocess.run(parts, capture_output=True, text=True)
                        if result.returncode:
                            raise build.BuildError(result.stderr)
                    return ""
                def transfer(argv, **kwargs):
                    if "--files-from=" in " ".join(argv):
                        return ""  # Staging is independent of this simulated artifact transfer.
                    source = Path(shlex.split(argv[-2].split(":", 1)[1])[0])
                    transferred.append(source.name)
                    shutil.copyfile(source, argv[-1])
                    return ""
                with patch.object(build, "ssh", side_effect=remote), patch.object(build, "run", side_effect=transfer), \
                        patch.object(build, "guard"), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(build.main(args), 0)
                result, = (root / "artifacts").iterdir()
                selected = "sdcard.img.zip" if zip_image else "sdcard.img"
                other = "sdcard.img" if zip_image else "sdcard.img.zip"
                self.assertIn(selected, transferred)
                self.assertNotIn(other, transferred)
                self.assertTrue((result / selected).exists())
                self.assertFalse((result / other).exists())
                self.assertTrue((output / "images/sdcard.img").exists())
                self.assertEqual(list(output.glob(".satip-zip-*")), [])
                build.verify(result, build.inputs(repo), zip_image=zip_image)

    def test_packaging_failure_never_marks_artifacts_verified(self):
        args = self.local_args()
        with patch.object(build, "run", side_effect=self.fake_build), \
                patch.object(build, "package_image", side_effect=build.BuildError("ZIP failed")), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(build.BuildError, "ZIP failed"):
                build.main(args)
        result, = (self.root / "artifacts").iterdir()
        self.assertFalse((result / "VERIFIED.json").exists())
        self.assertFalse((result / "sdcard.img").exists())
        self.assertTrue((self.root / "output/images/sdcard.img").exists())
        self.assertFalse((self.root / ".output.satip-build.lock").exists())

    def test_local_build_failure_releases_locks_and_never_marks_verified(self):
        args = self.local_args()
        def failure(argv, **kwargs):
            if kwargs.get("log"):
                Path(kwargs["log"]).write_text("failed\n")
                raise build.BuildError("simulated failure")
            return self.fake_build(argv, **kwargs)
        with patch.object(build, "run", side_effect=failure), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(build.BuildError):
                build.main(args)
        result, = (self.root / "artifacts").iterdir()
        self.assertTrue((result / "build.log").exists())
        self.assertFalse((result / "VERIFIED.json").exists())
        self.assertFalse((self.root / ".output.satip-build.lock").exists())

    def test_remote_uncertain_failure_retains_locks(self):
        args = ["--execution", "remote", "--host", "papa@example",
                "--repository", str(self.repository()),
                "--remote-root", str(self.root / "remote"), "--artifacts-dir", str(self.root / "artifacts")]
        def remote(args, command, **kwargs):
            if kwargs.get("log"):
                raise build.BuildError("SSH disconnected")
            return ""
        with patch.object(build, "ssh", side_effect=remote), patch.object(build, "guard") as guard, \
                patch.object(build, "run", return_value=""), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(build.BuildError):
                build.main(args)
        self.assertEqual(guard.call_count, 1)
        self.assertEqual(guard.call_args.args[1]["action"], "acquire")


if __name__ == "__main__":
    unittest.main()
