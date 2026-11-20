import ctypes
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from tests.test_buildroot_foundation import load, ROOT, BOARD

from satip_appliance import persistent, supervisor
from satip_controller.state import (atomic_json, read_json, runtime_path, lease_data,
                                    lease_active, retain_snapshots)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.chown = patch.object(persistent.os, "chown")
        self.chown.start()
        self.addCleanup(self.chown.stop)

    def initialize(self):
        persistent.publish(self.data)

    def test_initialize_is_explicit_and_no_example_library(self):
        self.initialize()
        self.assertEqual(read_json(self.data / "store/schema.json"), persistent.SCHEMA)
        self.assertEqual(read_json(self.data / "store/installation.json"), {"state": "unconfigured"})
        self.assertFalse((self.data / "store/management/library.json").exists())
        self.assertFalse((self.data / ".import-stage").exists())

    def test_existing_installation_is_never_overwritten(self):
        self.initialize()
        with self.assertRaises(ValueError):
            self.initialize()

    def test_interrupted_stage_is_retained_and_retry_refused(self):
        with patch.object(persistent, "permissions", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                self.initialize()
        self.assertFalse((self.data / "store").exists())
        self.assertTrue((self.data / ".import-stage").is_dir())
        with self.assertRaises(FileExistsError):
            self.initialize()

    def test_incomplete_import_is_not_published(self):
        source = self.root / "source"
        source.mkdir()
        with self.assertRaises(ValueError):
            persistent.publish(self.data, source, "fixture-revision")
        self.assertFalse((self.data / "store").exists())

    def test_import_preserves_durable_fixture_not_runtime(self):
        source = self.root / "source"
        for old in persistent.IMPORT:
            directory = source / old
            directory.mkdir(parents=True)
            atomic_json(directory / "fixture.json", {"uuid": "fixture-only"})
        atomic_json(source / "var/lib/satip-player/status.json", {"owner_pid": 123})
        atomic_json(source / "var/lib/satip-web/mux-rollback.json", {"uuid": "recovery"})
        persistent.publish(self.data, source, "fixture-revision")
        self.assertEqual(read_json(self.data / "store/player/fixture.json"), {"uuid": "fixture-only"})
        self.assertTrue((self.data / "store/web/mux-rollback.json").is_file())
        self.assertFalse((self.data / "store/player/status.json").exists())
        self.assertFalse(read_json(self.data / "store/backend.json")["qualified"])

    def test_symlink_in_snapshot_refused(self):
        source = self.root / "source"
        source.mkdir()
        (source / "linked").symlink_to("/etc/passwd")
        with self.assertRaises(ValueError):
            persistent.publish(self.data, source, "fixture")

    def test_incompatible_schema_does_not_make_ready(self):
        self.initialize()
        atomic_json(self.data / "store/schema.json", {"schema": 99})
        with self.assertRaises(ValueError):
            persistent.prepare(self.root, self.data)
        self.assertFalse((self.root / "run/satip-data-ready").exists())

    def test_boolean_schema_and_stale_ready_marker_are_refused(self):
        self.initialize()
        persistent.prepare(self.root, self.data)
        atomic_json(self.data / "store/schema.json", {**persistent.SCHEMA, "writer": True})
        with self.assertRaises(ValueError):
            persistent.prepare(self.root, self.data)
        self.assertFalse((self.root / "run/satip-data-ready").exists())

    def test_preparation_directory_links_and_setgid(self):
        self.initialize()
        persistent.prepare(self.root, self.data)
        self.assertEqual(os.readlink(self.root / "etc/satip-player"), "/data/store/config/player")
        self.assertEqual(os.readlink(self.root / "var/lib/satip-management"), "/data/store/management")
        self.assertEqual((self.data / "store/management").stat().st_mode & 0o7777, 0o2750)
        self.assertEqual((self.root / "run/satip-web").stat().st_mode & 0o777, 0o700)
        self.assertEqual(read_json(self.root / "run/satip-data-ready"), persistent.SCHEMA)
        persistent.prepare(self.root, self.data)

    def test_conflict_never_overwrites_root_slot_data(self):
        self.initialize()
        (self.root / "etc/satip-player").mkdir(parents=True)
        with self.assertRaises(ValueError):
            persistent.prepare(self.root, self.data)
        self.assertTrue((self.root / "etc/satip-player").is_dir())
        self.assertFalse((self.root / "run/satip-data-ready").exists())

    def test_boot_reconstructs_durable_recovery_flag(self):
        self.initialize()
        atomic_json(self.data / "store/web/mux-rollback.json", {"uuid": "fixture"})
        persistent.prepare(self.root, self.data)
        self.assertEqual(read_json(self.data / "store/management/recovery.json"), {"pending": True})

    def test_missing_and_readonly_mounts_refused(self):
        for info in ("", f"30 1 8:3 / {self.data} ro - ext4 /dev/test ro",
                     f"30 1 8:3 / {self.data} rw - tmpfs tmpfs rw"):
            with self.subTest(info=info), self.assertRaises(ValueError):
                persistent.validate_mount(self.data, info)

    def test_mount_device_and_space_are_verified(self):
        info = f"30 1 8:3 / {self.data} rw - ext4 /dev/mock rw"
        actual_stat = os.stat
        def fake_stat(path, *args, **kwargs):
            if str(path) == "/dev/mock":
                return SimpleNamespace(st_rdev=os.makedev(8, 3))
            if str(path) == "/":
                return SimpleNamespace(st_dev=-1)
            return actual_stat(path, *args, **kwargs)
        with patch.object(persistent.subprocess, "check_output", return_value="/dev/mock\n"), \
                patch.object(persistent.os, "stat", side_effect=fake_stat):
            persistent.validate_mount(self.data, info, probe=False)
            with patch.object(persistent.os, "statvfs", return_value=SimpleNamespace(f_bavail=0)):
                with self.assertRaises(ValueError):
                    persistent.validate_mount(self.data, info, probe=False)
            with self.assertRaises(ValueError):
                persistent.validate_mount(self.data, info.replace("8:3", "8:4"), probe=False)

    def test_atomic_write_through_directory_link_keeps_link(self):
        directory = self.root / "real"
        directory.mkdir()
        linked = self.root / "compat"
        linked.symlink_to(directory)
        atomic_json(linked / "library.json", {"version": 1})
        atomic_json(linked / "library.json", {"version": 2})
        self.assertTrue(linked.is_symlink())
        self.assertEqual(read_json(directory / "library.json"), {"version": 2})


class RuntimeTests(unittest.TestCase):
    def test_debian_paths_unchanged(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(runtime_path(Path("/var/lib/player"), "status.json"), Path("/var/lib/player/status.json"))
            self.assertTrue(lease_active(lease_data(10)))

    def test_buildroot_paths_separate_status_and_leases(self):
        with patch.dict(os.environ, {"SATIP_RUNTIME_ROOT": "/run"}):
            self.assertEqual(runtime_path(Path("/data/player"), "status.json"), Path("/run/satip-status/status.json"))
            self.assertEqual(runtime_path(Path("/data/management"), "epg.json"), Path("/run/satip-management/epg.json"))

    def test_snapshot_limits_preserve_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for number in range(20):
                atomic_json(directory / f"scan-backup-{number}.json", {"fixture": number})
            atomic_json(directory / "mux-rollback.json", {"pending": True})
            retain_snapshots(directory, count=3)
            self.assertEqual(len(list(directory.glob("scan-backup-*.json"))), 3)
            self.assertTrue((directory / "scan-backup-19.json").exists())
            self.assertTrue((directory / "mux-rollback.json").exists())
            retain_snapshots(directory, byte_limit=1)
            self.assertFalse(list(directory.glob("scan-backup-*.json")))

    @unittest.skipUnless(sys.platform == "linux", "Linux boot identity")
    def test_monotonic_lease_rejects_old_boot_and_wallclock(self):
        with patch.dict(os.environ, {"SATIP_RUNTIME_ROOT": "/run"}):
            node = lease_data(10)
            self.assertTrue(lease_active(node))
            self.assertFalse(lease_active({**node, "boot_id": "previous-boot"}))
            self.assertFalse(lease_active({"until": 99999999999}))
            self.assertFalse(lease_active(lease_data(-1)))


class PackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Match the build wrapper's canonical source paths (macOS /var is a link).
        self.root = Path(temporary.name).resolve()
        self.repo, self.target, self.output = [self.root / p for p in ("source", "target", "output")]
        for name in ("satip_controller", "satip_appliance"):
            shutil.copytree(ROOT / name, self.repo / name, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
            shutil.copytree(self.repo / name, self.target / "opt/satip" / name)
        for module in ("evdev", "zeroconf", "ifaddr", "fastapi", "uvicorn", "httpx", "starlette", "pydantic", "pydantic_core"):
            (self.target / "usr/lib/python3.14/site-packages" / module).mkdir(parents=True)
        (self.target / "usr/bin").mkdir(parents=True)
        (self.target / "usr/bin/tvheadend").write_bytes(b"\x7fELFfixture")
        self.configuration = self.output / "build/tvheadend-37453bc3fe5f9e10f3428ebb1abdc613f8b07186/build.linux/build.h"
        self.configuration.parent.mkdir(parents=True)
        self.configuration.write_text("#define ENABLE_SATIP_CLIENT 1\n#define ENABLE_SATIP_SERVER 0\n#define ENABLE_IPTV 1\n")
        for name in ("xml/parsers/expat.py", "lib-dynload/pyexpat.cpython-314-aarch64-linux-gnu.so",
                     "lib-dynload/zlib.cpython-314-aarch64-linux-gnu.so"):
            path = self.target / "usr/lib/python3.14" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        import hashlib
        import json
        patch_dir = self.repo / "buildroot/external/patches/tvheadend"
        shutil.copytree(ROOT / "buildroot/external/patches/tvheadend", patch_dir)
        manifest_path = patch_dir / "idle-mux-grace.json"
        manifest = json.loads(manifest_path.read_text())
        backend = self.configuration.parent.parent
        for name in manifest["files"]:
            source = backend / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"native source fixture")
            manifest["files"][name] = hashlib.sha256(source.read_bytes()).hexdigest()
            if name.endswith(".c"):
                obj = backend / "build.linux" / Path(name).with_suffix(".o")
                obj.parent.mkdir(parents=True, exist_ok=True)
                obj.write_bytes(b"\x7fELFfixture")
        manifest_path.write_text(json.dumps(manifest))
        self.validator = load("app_validator", BOARD / "validate-app-build.py")

    def test_all_static_sources_and_backend_features_are_recorded(self):
        result = self.validator.validate(self.target, self.output, self.repo, self.root)
        self.assertIn("satip_controller/static/hls-LICENSE.txt", result["files"])
        self.assertIn("satip_appliance/supervisor.py", result["files"])
        self.assertFalse(result["backend_database_qualified"])

    def test_stale_native_mux_grace_source_and_object_are_rejected(self):
        backend = self.configuration.parent.parent
        source = backend / "src/input/mpegts/mpegts_mux.c"
        source.write_text("stale")
        with self.assertRaisesRegex(ValueError, "missing/stale"):
            self.validator.validate_mux_grace(self.output, self.repo)
        source.write_bytes(b"native source fixture")
        obj = backend / "build.linux/src/input/mpegts/mpegts_mux.o"
        obj.write_bytes(b"not ELF")
        with self.assertRaisesRegex(ValueError, "object missing"):
            self.validator.validate_mux_grace(self.output, self.repo)

    def test_missing_httpx_zlib_dependency_is_rejected(self):
        (self.target / "usr/lib/python3.14/lib-dynload/zlib.cpython-314-aarch64-linux-gnu.so").unlink()
        with self.assertRaisesRegex(ValueError, "zlib"):
            self.validator.validate(self.target, self.output, self.repo, self.root)

    def test_bytecode_only_xml_runtime_is_accepted(self):
        stdlib = self.target / "usr/lib/python3.14"
        source = stdlib / "xml/parsers/expat.py"
        source.rename(source.with_suffix(".pyc"))
        self.validator.validate(self.target, self.output, self.repo, self.root)
        for name in ("xml/parsers/expat.pyc", "lib-dynload/pyexpat.cpython-314-aarch64-linux-gnu.so"):
            path = stdlib / name
            path.unlink()
            with self.assertRaisesRegex(ValueError, "XML parser"):
                self.validator.validate(self.target, self.output, self.repo, self.root)
            path.touch()

    def test_missing_discovery_xml_runtime_is_rejected(self):
        stdlib = self.target / "usr/lib/python3.14"
        for name in ("xml/parsers/expat.py", "lib-dynload/pyexpat.cpython-314-aarch64-linux-gnu.so"):
            path = stdlib / name
            path.unlink()
            with self.assertRaisesRegex(ValueError, "XML parser"):
                self.validator.validate(self.target, self.output, self.repo, self.root)
            path.touch()

    def test_stale_source_and_server_enabled_are_rejected(self):
        source = self.target / "opt/satip/satip_controller/state.py"
        original = source.read_bytes()
        source.write_text("stale")
        with self.assertRaises(ValueError):
            self.validator.validate(self.target, self.output, self.repo, self.root)
        source.write_bytes(original)
        self.configuration.write_text("#define ENABLE_SATIP_CLIENT 1\n#define ENABLE_SATIP_SERVER 1\n")
        with self.assertRaises(ValueError):
            self.validator.validate(self.target, self.output, self.repo, self.root)


class LifecycleTests(unittest.TestCase):
    def test_failed_supervisor_start_keeps_diagnostic_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            def spawn(*args, **kwargs):
                kwargs["stdout"].write(b"fixture startup failure\n")
                self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
                return SimpleNamespace(pid=999, poll=lambda: 1)
            with patch.object(supervisor.subprocess, "Popen", side_effect=spawn), \
                    patch.object(supervisor, "process_identity", return_value="owned"):
                with self.assertRaisesRegex(RuntimeError, "Supervisor failed to start"):
                    supervisor.direct_control("web", "start", directory)
            log = directory / "web.supervisor.log"
            self.assertEqual(log.read_text(), "fixture startup failure\n")
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_stale_pid_identity_does_not_signal_other_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "web.pid.json"
            atomic_json(path, {"pid": 999, "start": "old"})
            with patch.object(supervisor, "process_identity", return_value="new"), \
                    patch.object(supervisor.os, "pidfd_open", create=True) as pidfd:
                supervisor.direct_control("web", "stop", directory)
                pidfd.assert_not_called()
            self.assertFalse(path.exists())

    def test_stop_uses_pidfd_and_waits_for_identity_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            atomic_json(directory / "web.pid.json", {"pid": 999, "start": "owned"})
            with patch.object(supervisor, "process_identity", side_effect=["owned", "owned", "gone"]), \
                    patch.object(supervisor.os, "pidfd_open", return_value=123, create=True), \
                    patch.object(supervisor.signal, "pidfd_send_signal", create=True) as send, \
                    patch.object(supervisor.os, "close"):
                supervisor.direct_control("web", "stop", directory)
                send.assert_called_once_with(123, signal.SIGTERM)
            self.assertFalse((directory / "web.pid.json").exists())

    def test_backend_command_is_loopback_non_daemon_no_bootstrap(self):
        command = supervisor.SERVICES["tvheadend"][1]
        self.assertIn("127.0.0.1", command)
        self.assertIn("/data/store/tvheadend", command)
        self.assertNotIn("-f", command)
        self.assertNotIn("-C", command)

    @unittest.skipUnless(sys.platform == "linux", "Linux subreaper")
    def test_exact_group_escalation_reaps_orphan_and_preserves_other_group(self):
        self.assertEqual(ctypes.CDLL(None).prctl(36, 1, 0, 0, 0), 0)
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True)
        self.addCleanup(lambda: other.kill() if other.poll() is None else None)
        self.addCleanup(lambda: None)  # explicit wait below
        code = "import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); os.fork(); print('ready',flush=True); time.sleep(20)"
        child = subprocess.Popen([sys.executable, "-c", code], start_new_session=True, stdout=subprocess.PIPE)
        try:
            self.assertEqual(child.stdout.readline().strip(), b"ready")
            supervisor.cleanup(child.pid, grace=.1, final=2)
            self.assertFalse(supervisor.group_exists(child.pid))
            self.assertIsNotNone(os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT))
            self.assertIsNone(other.poll())
        finally:
            child.stdout.close()
            child.wait()
            other.kill()
            other.wait()

    @unittest.skipUnless(sys.platform == "linux", "Linux process identity")
    def test_process_identity_matches_starttime(self):
        self.assertTrue(supervisor.process_identity(os.getpid()).isdigit())

    @unittest.skipUnless(sys.platform == "linux", "Linux supervisor integration")
    def test_supervisor_restarts_after_cleanup_and_bounds_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            # This is an unprivileged isolated test process, not an appliance
            # service. Mock only device/data/account setup; use real processes,
            # sessions, waitid, pipe draining, signals and generation cleanup.
            script = r'''
import os,pwd,sys
from pathlib import Path
from unittest.mock import patch
from satip_appliance import supervisor as s
directory=Path(sys.argv[1])
code="""import os,signal,sys,time
from pathlib import Path
p=Path(sys.argv[1])/'starts'
n=int(p.read_text()) if p.exists() else 0
p.write_text(str(n+1))
if n==0:
    if os.fork()==0:
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        time.sleep(60)
    else:
        time.sleep(.1)
        raise SystemExit(7)
else:
    print('x'*(2*1024*1024),flush=True)
    os.kill(os.getppid(),signal.SIGTERM)
    time.sleep(60)
"""
s.SERVICES['web']=('fixture',[sys.executable,'-c',code,str(directory)])
original=s.subprocess.Popen
cleanup=s.cleanup
def spawn(*args,**kwargs):
    kwargs['cwd']=str(directory)
    return original(*args,**kwargs)
def bounded(pgid,**kwargs):
    return cleanup(pgid,grace=.1,final=2)
with patch.object(s,'ready',return_value=True), patch.object(s,'validate_mount'), \
     patch.object(s.pwd,'getpwnam',return_value=pwd.getpwuid(os.getuid())), \
     patch.object(s,'child_setup',side_effect=lambda *a:os.setsid()), \
     patch.object(s.subprocess,'Popen',side_effect=spawn), patch.object(s,'cleanup',side_effect=bounded):
    s.supervise('web',directory)
'''
            result = subprocess.run([sys.executable, "-c", script, str(directory)],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((directory / "starts").read_text(), "2")
            self.assertLessEqual((directory / "web.log").stat().st_size, 1024 * 1024)
            self.assertFalse((directory / "web.generation").exists())


if __name__ == "__main__":
    unittest.main()
