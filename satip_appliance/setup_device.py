"""Root-owned, first-run-only device actions shared by HDMI and the web wizard."""
import re
from pathlib import Path

from satip_controller.state import atomic_json, read_json
from . import provision
from .device_settings import read_optional


def decode_ssid(value):
    # iw escapes non-ASCII bytes and literal backslashes.
    data = bytearray()
    while value:
        if value.startswith(chr(92) + 'x') and len(value) >= 4:
            data.append(int(value[2:4], 16)); value = value[4:]
        elif value.startswith(chr(92) * 2):
            data.append(92); value = value[2:]
        else:
            data.extend(value[0].encode()); value = value[1:]
    return data.decode('utf-8')


def networks(text):
    result = {}
    # Parse complete BSS blocks so security and signal belong to the correct SSID.
    for block in re.split(r'(?m)^BSS ', text)[1:]:
        match = re.search(r'(?m)^\s*SSID: (.*)$', block)
        signal = re.search(r'signal: (-?[0-9.]+)', block)
        if not match:
            continue
        try:
            ssid = decode_ssid(match.group(1))
            data = ssid.encode()
        except (ValueError, UnicodeError):
            continue
        if not ssid or len(data) > 32 or any(ord(c) < 32 or ord(c) == 127 for c in ssid):
            continue
        supported = bool(re.search(r'Authentication suites:.*PSK', block))
        item = {'ssid': ssid, 'signal': float(signal.group(1)) if signal else -200, 'supported': supported}
        if ssid not in result or item['signal'] > result[ssid]['signal']:
            result[ssid] = item
    return sorted(result.values(), key=lambda n: -n['signal'])[:50]


def handle(device, action, args):
    from .device_helper import DeviceError, fields
    from .onboarding import input_devices
    if read_json(device.store / 'installation.json').get('state') != 'unconfigured':
        raise DeviceError('First setup has already finished')
    if action == 'setup_scan_wifi':
        fields(args, ('country',))
        if not isinstance(args['country'], str) or not re.fullmatch('[A-Z]{2}', args['country']):
            raise DeviceError('Choose your Wi-Fi country')
        device.command(['/usr/sbin/iw', 'reg', 'set', args['country']])
        device.command(['/sbin/ip', 'link', 'set', 'wlan0', 'up'])
        text = device.command(['/usr/sbin/iw', 'dev', 'wlan0', 'scan'], timeout=12).stdout
        return {'networks': networks(text)}
    if action == 'setup_inputs':
        fields(args, ())
        devices = input_devices()
        unique = {d['id']: {k: v for k, v in d.items() if k != 'path'} for d in devices}
        factory = read_json(device.store / 'identity/factory.json')['settings']
        event = read_optional(device.root / 'run/satip-network/input-test.json')
        found = next((d for d in devices if d['path'] == event.get('path')), None)
        return {'devices': list(unique.values()),
                'selected': f"{factory['keyboard_vendor']}:{factory['keyboard_product']}",
                'ir_enabled': True,
                'test': {'device': found['name'], 'id': found['id'], 'code': event['code'], 'time': event['time']} if found else None}
    if action == 'setup_input':
        fields(args, ('id',))
        chosen = next((d for d in input_devices() if d['id'] == args['id']), None)
        if not chosen:
            raise DeviceError('Connect the selected keyboard or remote first')
        factory_path = device.store / 'identity/factory.json'
        factory = read_json(factory_path)
        settings = factory['settings']
        settings['ir_profile'] = 'apple-a1294'
        if not chosen['remote']:
            settings['keyboard_vendor'], settings['keyboard_product'] = chosen['id'].split(':')
        # The identity is the commit point; provisioning replays derived files after a crash.
        atomic_json(factory_path, factory)
        provision.owned_json(device.store / 'config/player/config.json', provision.player_config(settings), 421, 421)
        provision.owned_json(device.store / 'config/web/setup.json',
                             {'settings': settings, 'playback_acl_uuid': factory['ids']['playback_acl']}, 422, 422)
        provision.apply_devices(device.root, settings)
        if device.root == Path('/'):
            device.command(['/usr/bin/udevadm', 'control', '--reload-rules'])
            device.command(['/usr/bin/udevadm', 'trigger', '--subsystem-match=input', '--action=change'])
        return {'message': 'Control saved. Press an arrow or OK button to test it.'}
    raise DeviceError('Unknown setup action')
