#!/usr/bin/env python3
"""Deploy matching application, launchers and public docs without reflashing.

Requires an explicit IPv4 target and --apply. Stops TV services and pauses the
init-owned helper, retains a private rollback archive, verifies hashes and
compiles with target Python before activation. Never provisions device data or
restarts networking. Optional flags install the matching splash binary and mDNS
dependencies. Prepared first-setup and configured installations are supported.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import ipaddress
import json
from pathlib import Path
import shlex
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = "buildroot/external/board/satip/rootfs-overlay/"
LAUNCHERS = (
    "usr/sbin/satip-dev-provision", "usr/sbin/satip-device-helper",
    "etc/init.d/S30satip-dev-provision", "etc/init.d/S39satip-device",
    "etc/init.d/S40network", "etc/init.d/S89satip-watchdog", "etc/init.d/S90satip-services",
)


def deployment_files(root=ROOT):
    root = Path(root)
    files = {}
    for package in ("satip_controller", "satip_appliance"):
        for source in sorted((root / package).glob("*.py")):
            files[source] = "opt/satip/" + source.relative_to(root).as_posix()
    for source in sorted((root / "satip_controller/static").iterdir()):
        if source.suffix in (".html", ".js", ".css", ".txt"):
            files[source] = "opt/satip/" + source.relative_to(root).as_posix()
    for target in LAUNCHERS:
        files[root / OVERLAY / target] = target
    installer = root / "buildroot/external/board/satip/install-documentation.py"
    spec = importlib.util.spec_from_file_location("satip_documentation", installer)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for source in module.documentation_sources(root):
        files[source] = "usr/share/doc/satip/" + source.relative_to(root).as_posix()
    for source in files:
        if not source.is_file() or any(p.is_symlink() for p in (source, *source.parents)):
            raise ValueError("Missing or linked deployment source: " + str(source.relative_to(root)))
    return files


def deployment_archive(files):
    archive = io.BytesIO()
    hashes = {}
    with tarfile.open(fileobj=archive, mode="w") as out:
        for source, target in files.items():
            content = source.read_bytes()
            hashes[target] = hashlib.sha256(content).hexdigest()
            entry = tarfile.TarInfo(target)
            entry.size = len(content)
            entry.mode = 0o755 if target in LAUNCHERS or target in ('usr/bin/satip-splash', 'usr/bin/tvheadend') else 0o644
            entry.uid = entry.gid = 0
            out.addfile(entry, io.BytesIO(content))
        content = json.dumps(hashes, sort_keys=True).encode()
        entry = tarfile.TarInfo("manifest.json")
        entry.size, entry.mode = len(content), 0o600
        out.addfile(entry, io.BytesIO(content))
    return archive.getvalue(), hashes


# Run as a single bounded transaction on the receiver. Secrets stay on-device.
REMOTE = r'''
import fcntl, hashlib, json, os, select, shutil, signal, subprocess, sys, tarfile, time
from pathlib import Path
backup = Path(sys.argv[1])
os.umask(0o077)
env = dict(os.environ, PYTHONPATH='/opt/satip', SATIP_RUNTIME_ROOT='/run')
def run(*args):
    subprocess.run(args, env=env, check=True, timeout=120)
def services(action):
    run('/usr/bin/python3', '-m', 'satip_appliance.supervisor', 'all', action)
owned = ['opt/satip/satip_controller', 'opt/satip/satip_appliance', 'usr/share/doc/satip']
launchers, extras = json.loads((backup / 'deployment.json').read_text())
owned += launchers + extras + ['etc/udev/rules.d/99-satip-input.rules']
with open('/run/satip-deploy.lock', 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    sys.path.insert(0, '/opt/satip')
    from satip_controller.state import read_json, lease_active
    from satip_controller.coordination import preview_held
    if not Path('/run/satip-data-ready').is_file() or not Path('/run/satip-dev-ready').is_file():
        raise RuntimeError('Configured development appliance required')
    installation = read_json(Path('/data/store/installation.json')).get('state')
    if installation not in ('configured', 'qualified', 'unconfigured'):
        raise RuntimeError('Prepared appliance required')
    for name in ('scan-lease.json', 'preview-lease.json'):
        p = Path('/run/satip-management') / name
        d = read_json(p) if p.exists() else {}
        if d.get('recovery_pending') or (preview_held(d) if name.startswith('preview') else lease_active(d)):
            raise RuntimeError('Finish scan, preview or recovery first')
    if Path('/data/store/network/wifi-trial.json').exists() or Path('/data/store/web/mux-rollback.json').exists():
        raise RuntimeError('Pending Wi-Fi trial or scan recovery')
    # Refuse insufficient space before stopping services or modifying the app.
    previous_bytes = sum(p.stat().st_size for name in owned[:3]
                         for p in Path('/' + name).rglob('*') if p.is_file())
    required = (backup / 'incoming.tar').stat().st_size * 2 + previous_bytes + 4 * 1024**2
    if shutil.disk_usage(backup).free < required:
        raise RuntimeError('Insufficient deployment/rollback space; use --root-backup on a writable development receiver')
    stage = backup / 'staged'
    stage.mkdir(mode=0o700)
    with tarfile.open(backup / 'incoming.tar') as archive:
        archive.extractall(stage, filter='data')
    hashes = json.loads((stage / 'manifest.json').read_text())
    for target, digest in hashes.items():
        if hashlib.sha256((stage / target).read_bytes()).hexdigest() != digest:
            raise RuntimeError('Staged hash mismatch: ' + target)
    # Detect syntax errors with the actual target interpreter before interruption.
    run('/usr/bin/python3', '-m', 'compileall', '-q', str(stage / 'opt/satip'))
    if 'usr/bin/tvheadend' in extras:
        # Target OpenSSL/glibc may differ from the component build host.
        # Fail before stopping services if the staged backend cannot execute.
        run(str(stage / 'usr/bin/tvheadend'), '--version')
    present = [p for p in owned if Path('/' + p).exists()]
    with tarfile.open(backup / 'before.tar', 'w') as out:
        for p in present:
            out.add('/' + p, arcname=p)
    (backup / 'owned.json').write_text(json.dumps({'owned': owned, 'present': present}))
    (backup / 'manifest.json').write_text(json.dumps(hashes, sort_keys=True))
    gate = Path('/run/satip-dev-ready')
    held_gate = backup / 'dev-ready'
    paused = False
    watchdog_stopped = False
    changed = False
    try:
        # Moving the readiness gate makes the respawned wrapper wait, without
        # modifying inittab or the saved recovery/network configuration.
        shutil.copyfile(gate, held_gate)
        gate.unlink()
        paused = True
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():
                continue
            try:
                args = (proc / 'cmdline').read_bytes().split(b'\0')
                if args[1:4] != [b'-m', b'satip_appliance.device_helper', b'run']:
                    continue
                fd = os.pidfd_open(int(proc.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
            try:
                signal.pidfd_send_signal(fd, signal.SIGTERM)
                if not select.select([fd], [], [], 15)[0]:
                    raise RuntimeError('Device helper did not stop')
            finally:
                os.close(fd)
        services('stop')
        # Reload the feeder too: importing new modules does not update its loop.
        run('/etc/init.d/S89satip-watchdog', 'stop')
        watchdog_stopped = True
        changed = True
        # Only remove exact package-owned trees, never persistent device data.
        for p in owned[:3]:
            destination = Path('/' + p)
            if destination.is_symlink():
                raise RuntimeError('Linked installation tree refused')
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(stage / p, destination)
        for p in launchers:
            shutil.copyfile(stage / p, '/' + p)
            os.chmod('/' + p, 0o755)
        for p in extras:
            destination = Path('/' + p)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            # Atomic replacement also works when the previous renderer is running.
            temporary = destination.with_name(destination.name + '.deploy-next')
            shutil.copyfile(stage / p, temporary)
            temporary.chmod(0o755 if p in ('usr/bin/satip-splash', 'usr/bin/tvheadend') else 0o644)
            os.replace(temporary, destination)
        for p in owned[:3]:
            for directory, dirs, files in os.walk('/' + p):
                os.chmod(directory, 0o755)
                for name in files:
                    path = Path(directory) / name
                    os.chown(path, 0, 0)
                    os.chmod(path, 0o644)
        for target, digest in hashes.items():
            if hashlib.sha256(Path('/' + target).read_bytes()).hexdigest() != digest:
                raise RuntimeError('Installed hash mismatch: ' + target)
        run('/usr/bin/python3', '-m', 'compileall', '-q', '--invalidation-mode', 'checked-hash', '/opt/satip')
        for path in Path('/opt/satip').rglob('*.pyc'):
            path.chmod(0o644)
        run('/usr/bin/python3', '-c',
            'from pathlib import Path; from satip_controller.state import read_json; '
            'from satip_appliance.provision import apply_devices; '
            'p=Path("/data/store/identity/factory.json"); '
            'apply_devices(Path("/"), read_json(p)["settings"]) if p.exists() else None')
        run('/usr/bin/udevadm', 'control', '--reload-rules')
        run('/usr/bin/udevadm', 'trigger', '--subsystem-match=input', '--action=change')
        if installation != 'unconfigured':
            run('/bin/su', '-s', '/bin/sh', '-c',
                'PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/satip SATIP_RUNTIME_ROOT=/run python3 -m satip_controller --check', 'satip-player')
    except BaseException:
        if changed:
            for p in owned[:3]:
                destination = Path('/' + p)
                if destination.exists() and not destination.is_symlink():
                    shutil.rmtree(destination)
            for p in launchers + extras:
                if p not in present:
                    Path('/' + p).unlink(missing_ok=True)
            # before.tar is a private trusted local archive, potentially with
            # documentation/configuration symlinks. It is not an input download.
            with tarfile.open(backup / 'before.tar') as archive:
                archive.extractall('/', filter='fully_trusted')
            print('Deployment failed; previous files restored.', flush=True)
        raise
    finally:
        if paused:
            shutil.copyfile(held_gate, gate)
            gate.chmod(0o600)
            if watchdog_stopped:
                run('/etc/init.d/S89satip-watchdog', 'start')
            services('start')
    # Init respawns the helper after the gate is restored. Probe its readiness.
    from satip_appliance.device_client import request
    import asyncio
    async def ready():
        async with asyncio.timeout(30):
            while True:
                try:
                    await request('storage_status')
                    return
                except (OSError, ValueError, TimeoutError):
                    await asyncio.sleep(.2)
    asyncio.run(ready())
    (backup / 'complete').write_text('Files verified; physical acceptance still required.' + chr(10))
    print('Verified deployment:', len(hashes), 'files. Private rollback:', backup)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='Receiver IPv4 address; root SSH must already work')
    parser.add_argument('--apply', action='store_true', help='Deploy and interrupt TV; default only lists files')
    parser.add_argument('--root-backup', action='store_true', help='Stage/retain private rollback on the writable root filesystem when /data is too small')
    parser.add_argument('--tvheadend-binary', type=Path, help='Matching pinned AArch64 backend with SAT-IP client/IPTV and current native patches')
    parser.add_argument('--splash-binary', type=Path, help='Matching AArch64 native renderer')
    parser.add_argument('--mdns-dependencies', type=Path, help='Pure-Python pip target containing zeroconf and ifaddr')
    args = parser.parse_args()
    ipaddress.IPv4Address(args.host)
    files = deployment_files()
    extras = {}
    if args.tvheadend_binary:
        header = args.tvheadend_binary.read_bytes()[:20]
        if header[:4] != b'\x7fELF' or header[18:20] != b'\xb7\x00':
            parser.error('An AArch64 ELF backend is required')
        extras[args.tvheadend_binary] = 'usr/bin/tvheadend'
    if args.splash_binary:
        header = args.splash_binary.read_bytes()[:20]
        if header[:4] != b'\x7fELF' or header[18:20] != b'\xb7\x00':
            parser.error('An AArch64 ELF renderer is required')
        extras[args.splash_binary] = 'usr/bin/satip-splash'
    if args.mdns_dependencies:
        for source in args.mdns_dependencies.rglob('*'):
            if source.is_symlink():
                parser.error('Linked dependencies refused')
            relative = source.relative_to(args.mdns_dependencies)
            if '__pycache__' in relative.parts or not source.is_file():
                continue
            if relative.parts[0] not in ('zeroconf', 'ifaddr') and not relative.parts[0].startswith(('zeroconf-', 'ifaddr-')):
                parser.error('Unexpected mDNS dependency')
            if source.suffix in ('.so', '.dylib', '.pyc'):
                parser.error('Use pure-Python mDNS dependencies')
            extras[source] = 'opt/satip/' + relative.as_posix()
        if not all(any(p.startswith('opt/satip/' + name + '/') for p in extras.values()) for name in ('zeroconf', 'ifaddr')):
            parser.error('Both zeroconf and ifaddr are required')
    extras[ROOT / 'buildroot/external/board/satip/satip-config.txt'] = 'usr/share/satip/satip-config.txt'
    files.update(extras)
    data, hashes = deployment_archive(files)
    if not args.apply:
        print('Dry run:', len(hashes), 'files; no connection or changes. Use --apply to interrupt TV and deploy.')
        for target in hashes:
            print(target)
        return
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=8', 'root@' + args.host]
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    parent = '/root/satip-deployments' if args.root_backup else '/data/store/operations'
    backup = parent + '/appliance-update-' + stamp
    command = 'umask 077; test ! -L ' + parent + ' && mkdir -p ' + parent + ' && mkdir ' + backup + ' && cat > ' + backup + '/incoming.tar'
    subprocess.run(ssh + [command], input=data, check=True, timeout=120)
    print('Private staging/backup:', backup, flush=True)
    # Dropbear bounds command-line length. Stage the program and inventory over
    # stdin instead of embedding a growing transaction in the SSH command.
    subprocess.run(ssh + ['cat > ' + backup + '/deploy.py'], input=REMOTE.encode(), check=True, timeout=30)
    subprocess.run(ssh + ['cat > ' + backup + '/deployment.json'],
                   input=json.dumps([LAUNCHERS, list(extras.values())]).encode(), check=True, timeout=30)
    command = 'PYTHONPATH=/opt/satip SATIP_RUNTIME_ROOT=/run python3 ' + backup + '/deploy.py ' + backup
    subprocess.run(ssh + [command], check=True, timeout=300)


if __name__ == '__main__':
    main()
