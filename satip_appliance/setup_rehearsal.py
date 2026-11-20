"""Development-only fresh-setup rehearsal with a restorable on-device snapshot.

Run as root: python3 -m satip_appliance.setup_rehearsal start|restore|status
Network and SSH access are retained. This is not a factory-reset feature.
"""
import argparse
import fcntl
import os
import re
from pathlib import Path
import select
import shutil
import signal
import subprocess
import tarfile
import time

from . import persistent, provision
from satip_controller.state import read_json, atomic_json, lease_active

BASE = Path('/data/.satip-setup-rehearsal')
STORE = Path('/data/store')
ROOT_FILES = ('etc/hostname', 'etc/timezone', 'etc/localtime', 'etc/wpa_supplicant.conf',
              'etc/udev/rules.d/99-satip-input.rules', 'boot/satip-config.txt')


def run(*command):
    subprocess.run(command, check=True, timeout=90)


def quiesce():
    Path('/run/satip-dev-ready').unlink(missing_ok=True)
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            args = (proc / 'cmdline').read_bytes().split(b'\0')
            if (args[1:4] != [b'-m', b'satip_appliance.device_helper', b'run']
                    and args[1:3] != [b'-m', b'satip_appliance.splash']):
                continue
            fd = os.pidfd_open(int(proc.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
        try:
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            if not select.select([fd], [], [], 15)[0]:
                raise RuntimeError('Device helper or splash monitor did not stop')
        finally:
            os.close(fd)
    run('/usr/bin/python3', '-m', 'satip_appliance.supervisor', 'all', 'stop')
    run('/etc/init.d/S89satip-watchdog', 'stop')


def resume():
    persistent.prepare()
    Path('/run/satip-dev-ready').write_text('ready\n')
    run('/etc/init.d/S89satip-watchdog', 'start')
    run('/usr/bin/python3', '-m', 'satip_appliance.supervisor', 'all', 'start')


def start(clear_wifi=False):
    persistent.validate_mount(Path('/data'))
    if read_json(STORE / 'installation.json').get('state') not in ('configured', 'qualified'):
        raise ValueError('Start from a configured receiver')
    if (BASE / 'original').exists():
        raise ValueError('A rehearsal is already active; restore it first')
    if BASE.exists():
        if not (BASE / 'restored').is_file():
            raise ValueError('Inspect the previous interrupted rehearsal first')
        BASE.rename(BASE.with_name(BASE.name + '-' + str(time.time_ns())))
    for filename in ('scan-lease.json', 'preview-lease.json'):
        path = Path('/run/satip-management') / filename
        if path.exists() and lease_active(read_json(path)):
            raise ValueError('Stop channel scanning and browser playback first')
    if (STORE / 'web/mux-rollback.json').exists() or (STORE / 'network/wifi-trial.json').exists():
        raise ValueError('Finish pending scan or Wi-Fi recovery first')
    BASE.mkdir(mode=0o700)
    with tarfile.open(BASE / 'root-before.tar', 'w') as archive:
        for name in ROOT_FILES:
            if Path('/' + name).exists():
                archive.add('/' + name, arcname=name, recursive=False)
    atomic_json(BASE / 'transaction.json', {'phase': 'prepared'})
    quiesce()
    STORE.rename(BASE / 'original')
    atomic_json(BASE / 'transaction.json', {'phase': 'original-saved'})
    persistent.publish(Path('/data'))
    original = BASE / 'original'
    # Copy only connectivity/recovery identity, never tuner state or a lineup.
    for relative in ('identity/development.txt', 'identity/dropbear', 'identity/root-access.json',
                     'identity/authorized_keys', 'identity/hostname', 'network/wpa_supplicant.conf'):
        source, target = original / relative, STORE / relative
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        elif source.is_file():
            shutil.copyfile(source, target)
    persistent.prepare()
    from .device_settings import saved_preferences, save_preferences
    parser = provision.parser()
    values = saved_preferences(Path('/'), parser)
    connectivity = {'hostname', 'wifi_country', 'wifi_ssid', 'wifi_psk',
                    'ssh_enabled', 'ssh_authorized_key', 'recovery_password_hash'}
    save_preferences(Path('/'), parser, {k: v for k, v in values.items() if k in connectivity})
    settings = read_json(original / 'identity/factory.json').get('settings', {})
    display = {k: settings[k] for k in ('drm_connector', 'drm_mode', 'audio_device') if k in settings}
    provision.seed(Path('/'), Path('/data'), display)
    if clear_wifi:
        from .device_settings import saved_preferences, save_preferences
        parser = provision.parser()
        values = saved_preferences(Path('/'), parser)
        save_preferences(Path('/'), parser, {k: v for k, v in values.items() if not k.startswith('wifi_')})
        Path('/etc/wpa_supplicant.conf').unlink(missing_ok=True)
        (STORE / 'network/wpa_supplicant.conf').unlink(missing_ok=True)
    from .splash import release
    release()
    # A fresh preparation cycle needs a new renderer/monitor after the old handoff.
    for name in ('finished', 'retain'):
        (Path('/run/satip-splash') / name).unlink(missing_ok=True)
    with open('/run/satip-splash.log', 'ab') as log:
        subprocess.Popen(['/usr/bin/satip-splash'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=log, start_new_session=True)
    with open('/run/satip-splash-monitor.log', 'ab') as log:
        subprocess.Popen(['/usr/bin/python3', '-m', 'satip_appliance.splash'], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=log, start_new_session=True)
    resume()
    atomic_json(BASE / 'transaction.json', {'phase': 'running'})
    suffix = " --name " + BASE.name.removeprefix('.satip-setup-rehearsal-') if BASE.name != '.satip-setup-rehearsal' else ""
    print('Fresh setup is active. Restore with: python3 -m satip_appliance.setup_rehearsal restore' + suffix, flush=True)
    if clear_wifi:
        print('Stopping saved Wi-Fi. Continue using HDMI and a USB keyboard, or Ethernet.', flush=True)
        # Detach from the SSH connection before intentionally dropping its link.
        subprocess.Popen(['/etc/init.d/S40network', 'restart-wifi'], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def restore():
    if not (BASE / 'original/installation.json').is_file():
        raise ValueError('No saved installation to restore')
    quiesce()
    if STORE.exists():
        STORE.rename(BASE / ('trial-' + str(time.time_ns())))
    (BASE / 'original').rename(STORE)
    with tarfile.open(BASE / 'root-before.tar') as archive:
        # Private archive created above, including the original localtime symlink.
        for member in archive:
            destination = Path('/') / member.name
            if destination.is_symlink():
                destination.unlink()
            archive.extract(member, '/', filter='fully_trusted')
    provision.apply_devices(Path('/'), read_json(STORE / 'identity/factory.json')['settings'])
    run('/usr/bin/udevadm', 'control', '--reload-rules')
    run('/usr/bin/udevadm', 'trigger', '--subsystem-match=input', '--action=change')
    resume()
    (BASE / 'restored').write_text('Original installation restored.\n')
    print('Original receiver configuration restored. Trial results are retained privately on /data.')


def main():
    global BASE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'restore', 'status'))
    parser.add_argument('--clear-wifi', action='store_true', help='With start: discard trial Wi-Fi preferences and disconnect Wi-Fi for HDMI bootstrap testing')
    parser.add_argument('--name', help='Separate development snapshot; leaves the default rehearsal untouched')
    args = parser.parse_args()
    if args.name is not None:
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,31}', args.name):
            parser.error('Use a short lowercase rehearsal name, not a path')
        BASE = Path('/data/.satip-setup-rehearsal-' + args.name)
    action = args.action
    if args.clear_wifi and action != 'start':
        parser.error('--clear-wifi applies only to start')
    if os.geteuid() != 0:
        raise SystemExit('Root required')
    os.umask(0o077)
    with open('/run/satip-deploy.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if action == 'status':
            print('Rehearsal active' if (BASE / 'original').exists() else 'Original installation active')
        else:
            start(args.clear_wifi) if action == 'start' else restore()


if __name__ == '__main__':
    main()
