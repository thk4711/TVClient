"""Supervisor ownership tests; Linux tests use isolated subprocess trees."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from satip_appliance import guardian, watchdog


class ProgressTests(unittest.TestCase):
    def test_identity_boot_and_timestamp_are_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "progress"
            node = {"pid": 42, "start": "123", "boot_id": "boot", "monotonic": 100}
            with patch.object(guardian, "boot_id", return_value="boot"):
                path.write_text(json.dumps(node))
                self.assertTrue(guardian.fresh(path, 42, "123", 120))
                self.assertFalse(guardian.fresh(path, 42, "123", 146))
                for key, value in (("pid", 43), ("start", "old"), ("boot_id", "old"),
                                   ("monotonic", True), ("monotonic", float("nan")),
                                   ("monotonic", 121)):
                    path.write_text(json.dumps(dict(node, **{key: value})))
                    self.assertFalse(guardian.fresh(path, 42, "123", 120))
                path.write_text("[]")
                self.assertFalse(guardian.fresh(path, 42, "123", 120))
                path.unlink()
                path.symlink_to(Path(temporary) / "missing")
                self.assertFalse(guardian.fresh(path, 42, "123", 120))

    def test_progress_published_after_poll_clock_sample_is_healthy(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "player.guard.expected").write_text('{"began":0}')
            (directory / "player.guard.pid.json").write_text('{"pid":42,"start":"123"}')
            node = {"pid":42, "start":"123", "boot_id":"boot", "monotonic":101, "phase":"monitoring"}
            (directory / "player.guard.progress").write_text(json.dumps(node))
            with patch.object(watchdog, "alive", return_value=True), \
                 patch.object(guardian, "boot_id", return_value="boot"), \
                 patch.object(watchdog.time, "monotonic", side_effect=[100, 101]):
                self.assertTrue(watchdog.guardians_healthy(directory=directory))

    def test_watchdog_requires_enabled_guardians_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.assertTrue(watchdog.guardians_healthy(100, directory))
            expected = directory / "player.guard.expected"
            expected.write_text('{"began":100}')
            self.assertTrue(watchdog.guardians_healthy(105, directory))
            self.assertFalse(watchdog.guardians_healthy(116, directory))
            expected.write_text('[]')
            self.assertFalse(watchdog.guardians_healthy(105, directory))

    def test_feeder_escalation_does_not_magic_close(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "nowayout").write_text("0")
            device = root / "device"
            device.touch()
            original_open = os.open
            def open_device(path, flags, *args, **kwargs):
                return original_open(device if path == "/dev/watchdog0" else path, flags, *args, **kwargs)
            import signal
            handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
            try:
                with patch.object(watchdog, "ROOT", root), patch.object(watchdog, "STATE", root), \
                     patch.object(watchdog, "READY", root / "ready"), \
                     patch.object(watchdog, "configure", return_value=(15, 3)), \
                     patch.object(watchdog.os, "open", side_effect=open_device), \
                     patch.object(watchdog, "guardians_healthy", return_value=False), \
                     patch("satip_appliance.operations.best_effort"):
                    with self.assertRaises(RuntimeError):
                        watchdog.run()
                self.assertNotIn(b"V", device.read_bytes())
                self.assertFalse((root / "ready").exists())
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)

    def test_dead_guardian_cannot_be_replaced_or_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "web.guard.expected").write_text('{"began":0}')
            for action in ("start", "stop"):
                with self.assertRaises(RuntimeError):
                    guardian.control("web", action, directory)

    def test_verified_stop_ack_has_only_a_short_handoff_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "web.guard.expected").write_text('{"began":0}')
            (directory / "web.guard.pid.json").write_text('{"pid":42,"start":"123"}')
            node = {"pid":42, "start":"123", "boot_id":"boot", "monotonic":100, "phase":"stopped"}
            (directory / "web.guard.progress").write_text(json.dumps(node))
            with patch.object(watchdog, "alive", return_value=False), patch.object(guardian, "boot_id", return_value="boot"):
                self.assertTrue(watchdog.guardians_healthy(104, directory))
                self.assertFalse(watchdog.guardians_healthy(106, directory))

    def test_child_exit_during_signalling_is_not_cleanup_failure(self):
        with patch.object(guardian, "exited", side_effect=[False, True]), \
             patch.object(guardian.os, "pidfd_open", return_value=123, create=True), \
             patch.object(guardian.signal, "pidfd_send_signal", side_effect=ProcessLookupError, create=True), \
             patch.object(guardian.os, "close"):
            guardian.terminate(42, 1)

    def test_watchdog_cleanup_has_a_fixed_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "web.guard.expected").write_text('{"began":0}')
            (directory / "web.guard.pid.json").write_text('{"pid":42,"start":"123"}')
            node = {"pid":42, "start":"123", "boot_id":"boot", "monotonic":100, "phase":"cleanup"}
            (directory / "web.guard.progress").write_text(json.dumps(node))
            with patch.object(watchdog, "alive", return_value=True), patch.object(guardian, "boot_id", return_value="boot"):
                self.assertTrue(watchdog.guardians_healthy(190, directory))
                self.assertFalse(watchdog.guardians_healthy(201, directory))


@unittest.skipUnless(sys.platform == "linux", "Linux subreaper/pidfd integration")
class OwnershipTests(unittest.TestCase):
    def test_guardian_replaces_interrupted_launch_without_marker_pid(self):
        script = r'''
import os, subprocess, sys
from pathlib import Path
from unittest.mock import patch
from satip_controller.state import atomic_json
from satip_appliance import guardian as g, supervisor as s
root=Path(sys.argv[1]); count=0
code="""
import os,signal,sys,time
from pathlib import Path
from satip_appliance.guardian import publish
root=Path(sys.argv[1]); attempt=int(sys.argv[2])
if os.fork()==0:
    os.setsid()
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    (root/('descendant-'+str(attempt))).write_text(str(os.getpid()))
    time.sleep(60)
else:
    while not (root/('descendant-'+str(attempt))).exists(): time.sleep(.01)
    publish(root/'web.progress','launching')
    if attempt==1:
        os.kill(os.getpid(),signal.SIGKILL if sys.argv[3]=='crash' else signal.SIGSTOP)
    else:
        os.kill(os.getppid(),signal.SIGTERM)
    time.sleep(60)
"""
def start(service, action, directory):
    global count
    count+=1
    atomic_json(root/'web.generation',{'launching':True},durable=False)
    child=subprocess.Popen([sys.executable,'-c',code,str(root),str(count),sys.argv[2]],start_new_session=True)
    atomic_json(root/'web.pid.json',{'pid':child.pid,'start':g.identity(child.pid)},durable=False)
original_drain=g.drain
with patch.object(s,'direct_control',side_effect=start), patch.object(g,'PROGRESS_TIMEOUT',2), \
     patch.object(g,'drain',side_effect=lambda pid,grace:original_drain(pid,.1)), \
     patch('satip_appliance.operations.best_effort'):
    g.run('web',root)
assert count==2,count
assert g.children()==[],g.children()
assert not (root/'web.generation').exists()
for path in root.glob('descendant-*'):
    assert not Path('/proc/'+path.read_text()).exists()
'''
        for failure in ("crash", "freeze"):
            with tempfile.TemporaryDirectory() as temporary:
                result = subprocess.run([sys.executable, "-c", script, temporary, failure],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_crash_and_freeze_cleanup_preserves_unrelated_process(self):
        # Run in an isolated subreaper, never turn the test runner into one.
        script = r'''
import ctypes, os, signal, subprocess, sys, time
from pathlib import Path
from satip_appliance.guardian import drain, children
ctypes.CDLL(None).prctl(36,1,0,0,0)
root=Path(sys.argv[1])
# Unrelated work remains outside this subreaper (in the parent test process).
code="""
import os,signal,sys,time
from pathlib import Path
if os.fork()==0:
    os.setsid()
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    Path(sys.argv[1]).write_text(str(os.getpid()))
    time.sleep(60)
else:
    time.sleep(60)
"""
child=subprocess.Popen([sys.executable,'-c',code,str(root/'descendant')],start_new_session=True)
try:
    end=time.monotonic()+5
    while not (root/'descendant').exists() and time.monotonic()<end: time.sleep(.01)
    descendant=int((root/'descendant').read_text())
    os.kill(child.pid,signal.SIGKILL if sys.argv[2]=='crash' else signal.SIGSTOP)
    drain(child.pid,.1)
    assert children()==[],children()
    assert not Path('/proc/'+str(descendant)).exists()
finally:
    child.wait()
'''
        # The unrelated process belongs to this parent, outside the tested guardian.
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            for failure in ("crash", "freeze"):
                with tempfile.TemporaryDirectory() as temporary:
                    result = subprocess.run([sys.executable, "-c", script, temporary, failure],
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIsNone(other.poll())
        finally:
            other.terminate()
            other.wait()


if __name__ == "__main__":
    unittest.main()
