"""Deployment inventory checks; never connect to a receiver."""
import io
import json
from pathlib import Path
import runpy
import tarfile
import unittest

DEPLOY = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/deploy_appliance.py'))


class DeploymentTests(unittest.TestCase):
    def test_complete_source_and_documentation_inventory(self):
        files = DEPLOY['deployment_files']()
        targets = set(files.values())
        for package in ('satip_controller', 'satip_appliance'):
            expected = {'opt/satip/' + p.relative_to(DEPLOY['ROOT']).as_posix()
                        for p in (DEPLOY['ROOT'] / package).glob('*.py')}
            self.assertTrue(expected <= targets)
        self.assertIn('opt/satip/satip_controller/i18n.py', targets)
        self.assertIn('opt/satip/satip_controller/tuning.py', targets)
        self.assertIn('opt/satip/satip_appliance/wifi_scan.py', targets)
        self.assertIn('opt/satip/satip_appliance/guardian.py', targets)
        self.assertIn('etc/init.d/S89satip-watchdog', targets)
        self.assertIn('usr/share/doc/satip/README.md', targets)
        self.assertFalse(any(p.startswith(('data/', 'boot/')) for p in targets))
        self.assertFalse(any('credentials.json' in p or '__pycache__' in p for p in targets))

    def test_archive_contains_only_regular_manifested_files_with_public_modes(self):
        files = DEPLOY['deployment_files']()
        data, hashes = DEPLOY['deployment_archive'](files)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            members = archive.getmembers()
            self.assertEqual({m.name for m in members}, set(hashes) | {'manifest.json'})
            for member in members:
                self.assertTrue(member.isfile())
                self.assertFalse(member.name.startswith('/') or '..' in Path(member.name).parts)
                self.assertEqual(member.uid, 0)
                self.assertEqual(member.gid, 0)
                expected = 0o600 if member.name == 'manifest.json' else 0o755 if member.name in DEPLOY['LAUNCHERS'] else 0o644
                self.assertEqual(member.mode, expected)
            self.assertEqual(json.load(archive.extractfile('manifest.json')), hashes)

    def test_remote_transaction_compiles_and_uses_owned_lifecycle(self):
        remote = DEPLOY['REMOTE']
        compile(remote, '<deployment>', 'exec')
        self.assertIn("fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)", remote)
        self.assertIn("'satip_appliance.supervisor', 'all', action", remote)
        self.assertIn("pidfd_send_signal", remote)
        self.assertIn("'checked-hash'", remote)
        self.assertIn("previous files restored", remote)
        self.assertLess(remote.index("services('stop')"),
                        remote.index("run('/etc/init.d/S89satip-watchdog', 'stop')"))
        self.assertLess(remote.index("run('/etc/init.d/S89satip-watchdog', 'start')"),
                        remote.index("services('start')"))
        self.assertLess(remote.index("run(str(stage / 'usr/bin/tvheadend'), '--version')"),
                        remote.index("services('stop')"))
        self.assertNotIn('systemctl', remote)
        self.assertNotIn('kill -HUP 1', remote)
