"""First-run HDMI interaction. One helper owns input; network changes use its socket.

The public screen contains masks only. Wi-Fi secrets stay in memory and the
existing root-only trial journal; neither key events nor passwords are logged.
"""
import asyncio
import fcntl
import ipaddress
import os
from pathlib import Path
import socket
import struct
import time

from satip_controller.state import atomic_json, boot_id, read_json
from .device_client import request
from .device_settings import read_optional

SCREEN = Path('/run/satip-network/onboarding.json')
INPUT = Path('/run/satip-network/input-test.json')
# Linux evdev codes, deliberately independent of evdev imports for policy tests.
US = dict([[2,"1"],[3,"2"],[4,"3"],[5,"4"],[6,"5"],[7,"6"],[8,"7"],[9,"8"],[10,"9"],[11,"0"],[12,"-"],[13,"="],[16,"q"],[17,"w"],[18,"e"],[19,"r"],[20,"t"],[21,"y"],[22,"u"],[23,"i"],[24,"o"],[25,"p"],[26,"["],[27,"]"],[30,"a"],[31,"s"],[32,"d"],[33,"f"],[34,"g"],[35,"h"],[36,"j"],[37,"k"],[38,"l"],[39,";"],[40,"'"],[41,"`"],[43,"\\"],[44,"z"],[45,"x"],[46,"c"],[47,"v"],[48,"b"],[49,"n"],[50,"m"],[51,","],[52,"."],[53,"/"],[57," "],[86,"\\"]])
SHIFT = {"0":")","1":"!","2":"@","3":"#","4":"$","5":"%","6":"^","7":"&","8":"*","9":"(","-":"_","=":"+","[":"{","]":"}",";":":","`":"~","\\":"|",",":"<",".":">","/":"?","'":"\""}
DE = dict([[2,"1"],[3,"2"],[4,"3"],[5,"4"],[6,"5"],[7,"6"],[8,"7"],[9,"8"],[10,"9"],[11,"0"],[12,"ß"],[13,"´"],[16,"q"],[17,"w"],[18,"e"],[19,"r"],[20,"t"],[21,"z"],[22,"u"],[23,"i"],[24,"o"],[25,"p"],[26,"ü"],[27,"+"],[30,"a"],[31,"s"],[32,"d"],[33,"f"],[34,"g"],[35,"h"],[36,"j"],[37,"k"],[38,"l"],[39,"ö"],[40,"ä"],[41,"^"],[43,"#"],[44,"y"],[45,"x"],[46,"c"],[47,"v"],[48,"b"],[49,"n"],[50,"m"],[51,","],[52,"."],[53,"-"],[57," "],[86,"<"]])
DE_SHIFT = {"0":"=","1":"!","2":"\"","3":"§","4":"$","5":"%","6":"&","7":"/","8":"(","9":")","ß":"?","´":"`","+":"*","^":"°","#":"'",",":";",".":":","-":"_","<":">"," ":" "}
ALTGR = dict([[3,"²"],[4,"³"],[8,"{"],[9,"["],[10,"]"],[11,"}"],[12,"\\"],[16,"@"],[18,"€"],[27,"~"],[86,"|"]])

def character(code, held, layout, caps=False):
    value = (DE if layout == 'de' else US).get(code, '')
    if layout == 'de' and 100 in held:
        return ALTGR.get(code, '')
    shifted = bool({42, 54} & held)
    if value.isalpha():
        return value.upper() if shifted != caps else value
    return (DE_SHIFT if layout == 'de' else SHIFT).get(value, value) if shifted else value


def carrier(interface, root=Path('/')):
    try:
        return (root / f'sys/class/net/{interface}/carrier').read_text().strip() == '1'
    except OSError:
        return False


def confirmed_address(interface, root=Path('/')):
    """Require current-boot DHCP confirmation, carrier and the actual interface IP."""
    if not carrier(interface, root):
        return None
    node = read_optional(root / f'run/satip-network/{interface}.json')
    if node.get('state') != 'confirmed' or node.get('boot_id') != boot_id():
        return None
    try:
        ip = ipaddress.IPv4Address(node['ip'])
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            return None
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            actual = socket.inet_ntoa(fcntl.ioctl(client, 0x8915, struct.pack('256s', interface.encode()))[20:24])
        return str(ip) if actual == str(ip) else None
    except (OSError, ValueError, KeyError):
        return None


def input_devices():
    import evdev
    from satip_controller.input import is_usb_keyboard
    devices = []
    for path in evdev.list_devices():
        device = None
        try:
            device = evdev.InputDevice(path)
            keys = set(device.capabilities().get(1, []))
            keyboard = is_usb_keyboard(device, keys)
            remote = device.name == 'gpio_ir_recv'
            usb_remote = device.info.bustype == 3 and bool(keys & {103, 108, 352, 402, 403})
            if keyboard or remote or usb_remote:
                devices.append({'id': 'ir' if remote else f'{device.info.vendor:04x}:{device.info.product:04x}',
                                'path': path, 'name': 'Infrared receiver connected to GPIO' if remote else device.name.strip() or 'USB input device',
                                'keyboard': keyboard, 'remote': remote})
        except OSError:
            continue
        finally:
            if device is not None:
                device.close()
    return devices


def page(title, *lines, url=''):
    # Bound by UTF-8 bytes too: native transport has a fixed 2048-byte buffer.
    safe = [str(s).replace('\n', ' ').replace('\r', ' ')[:90] for s in (title, *lines)][:9]
    safe += [''] * (9 - len(safe))
    return '\n'.join(safe + [url])


class Onboarding:
    def __init__(self):
        self.phase = 'network'
        self.networks = []
        self.index = 0
        self.country_index = 0
        self.countries = [('DE', 'Germany'), ('AT', 'Austria'), ('CH', 'Switzerland'),
                          ('NL', 'Netherlands'), ('FR', 'France'), ('GB', 'United Kingdom'), ('US', 'United States')]
        try:
            rows = [line.split('\t') for line in Path('/usr/share/zoneinfo/iso3166.tab').read_text().splitlines()
                    if line and not line.startswith('#')]
            self.countries = sorted([(r[0], r[-1]) for r in rows], key=lambda item: item[1])
            self.country_index = next(i for i, c in enumerate(self.countries) if c[0] == 'DE')
        except (OSError, StopIteration):
            pass
        self.layout = 'de'
        self.password = ''
        self.message = ''
        self.selected = ''
        self.trial = None
        self.task = None
        self.began = time.monotonic()
        self.devices = {}
        self.held = {}
        self.caps = False
        self.keyboard = False
        self.active = False
        self.last_screen = None

    async def scan(self):
        self.phase = 'scanning'
        self.message = ''
        try:
            result = await request('setup_scan_wifi', {'country': self.countries[self.country_index][0]})
            self.networks = result['networks']
            self.index = 0
            self.phase = 'networks'
            if not self.networks:
                self.message = 'No networks found. Move closer to the router or connect Ethernet.'
        except (ValueError, OSError, TimeoutError):
            self.phase = 'networks'
            self.message = 'Wi-Fi scan failed. Press R to retry, or connect Ethernet.'

    async def connect(self):
        self.phase = 'connecting'
        password, self.password = self.password, ''
        try:
            result = await request('wifi', {'ssid': self.selected, 'password': password,
                                           'country': self.countries[self.country_index][0]})
            password = ''
            self.trial = result['trial_id']
            end = time.monotonic() + 85
            while time.monotonic() < end:
                status = await request('status')
                if not status.get('trial'):
                    break
                if status['trial'].get('applied') and status['connected_ssid'] == self.selected and confirmed_address('wlan0'):
                    await request('wifi_confirm', {'id': self.trial})
                    self.trial = None
                    self.phase = 'network'
                    return
                if confirmed_address('eth0'):
                    break
                await asyncio.sleep(1)
            self.message = 'Could not connect. Check the password and router, then try again.'
        except (ValueError, OSError, TimeoutError):
            self.message = 'Wi-Fi connection failed. Check your password and try again.'
        finally:
            password = ''
            if self.trial:
                try:
                    await request('wifi_revert', {'id': self.trial})
                except (ValueError, OSError, TimeoutError):
                    pass  # The helper's durable deadline also rolls this back.
                self.trial = None
        self.phase = 'password'

    def start(self, coroutine):
        if not self.task or self.task.done():
            self.task = asyncio.create_task(coroutine)

    def key(self, code, value, source):
        held = self.held.setdefault(source, set())
        if value == 0:
            held.discard(code)
            return
        held.add(code)
        if code == 58 and value == 1:
            self.caps = not self.caps
        if not self.active:
            if value == 1 and code in {1, 28, 103, 108, 105, 106, 113, 114, 115, 164, 352, 402, 403}:
                atomic_json(INPUT, {'path': source, 'code': code, 'time': time.monotonic()}, 0o644, durable=False)
            return
        if self.phase in ('connecting', 'scanning'):
            return
        if code == 1:
            self.password = ''
            self.phase = 'preferences'
            self.message = ''
        elif self.phase in ('network', 'preferences'):
            self.phase = 'preferences'
            if code in (103, 108):
                self.country_index = (self.country_index + (1 if code == 108 else -1)) % len(self.countries)
            elif code in (15, 105, 106):
                self.layout = 'us' if self.layout == 'de' else 'de'
            elif code == 28:
                self.start(self.scan())
        elif self.phase == 'networks':
            if code == 19:
                self.start(self.scan())
            elif self.networks and code in (103, 108):
                self.index = (self.index + (1 if code == 108 else -1)) % len(self.networks)
            elif self.networks and code == 28:
                network = self.networks[self.index]
                if not network.get('supported', True):
                    self.message = 'Choose a WPA/WPA2 Personal network, or connect Ethernet.'
                else:
                    self.selected = network['ssid']
                    self.password = ''
                    self.message = ''
                    self.phase = 'password'
        elif self.phase == 'password':
            if code == 14:
                self.password = self.password[:-1]
            elif code == 28:
                if 8 <= len(self.password) <= 63 or (len(self.password) == 64 and all(c in '0123456789abcdefABCDEF' for c in self.password)):
                    self.start(self.connect())
                else:
                    self.message = 'Use 8–63 password characters (or a 64-digit hexadecimal key).'
            elif len(self.password) < 64:
                char = character(code, held, self.layout, self.caps)
                if char and all(32 <= ord(c) <= 126 for c in char):
                    self.password += char
                elif char:
                    self.message = 'This receiver supports printable ASCII Wi-Fi passwords.'

    def screen(self):
        ethernet = confirmed_address('eth0')
        wifi = confirmed_address('wlan0') if not self.trial else None
        ip = ethernet or wifi
        self.active = not ip
        if ip:
            from .splash import listening
            if not listening(8080):
                return page('Connected to your home network', 'Starting the setup interface…')
            from .mdns import local_name
            mdns = read_optional(Path('/run/satip-network/mdns.json'))
            url = f'http://{ip}:8080/'
            return page('Welcome! Finish setup in your browser',
                        'On a phone or computer on the same home network, open:', url,
                        'Or scan the QR code with your phone. No password is needed.',
                        f'Also available at http://{local_name()}:8080/' if mdns else '', url=url)
        waiting = carrier('eth0') and time.monotonic() - self.began < 25
        if waiting:
            self.active = False
            return page('Ethernet connected', 'Waiting for your router to assign a network address…',
                        'Wi-Fi setup will be offered if Ethernet is unavailable.')
        if not self.keyboard:
            return page('Connect to your home network',
                        'Connect an Ethernet cable to your router,',
                        'or connect a USB keyboard to set up Wi-Fi.',
                        'We will detect either connection automatically.')
        if self.phase in ('network', 'preferences'):
            country, name = self.countries[self.country_index]
            return page('Connect Ethernet, or set up Wi-Fi',
                        'You can connect an Ethernet cable at any time.',
                        f'Country: {name} ({country}) — Up / Down to change',
                        f'Keyboard: {"Deutsch (QWERTZ)" if self.layout == "de" else "US English (QWERTY)"} — Tab to change',
                        'Press Enter to search for Wi-Fi networks.')
        if self.phase == 'scanning':
            return page('Searching for Wi-Fi networks…', 'You can also connect an Ethernet cable.')
        if self.phase == 'networks':
            start = max(0, self.index - 2)
            rows = [f'{"> " if start+i == self.index else "  "}{n["ssid"]}  ({n["signal"]:.0f} dBm){" — unsupported" if not n.get("supported", True) else ""}'
                    for i, n in enumerate(self.networks[start:start+5])]
            return page('Choose your Wi-Fi network', *rows, self.message,
                        'Up / Down: select   Enter: continue   R: rescan   Esc: back', 'Or connect an Ethernet cable.')
        if self.phase == 'connecting':
            return page('Connecting to Wi-Fi…', self.selected, 'Checking the password and waiting for a network address.',
                        'You can also connect an Ethernet cable.')
        return page('Enter your Wi-Fi password', self.selected, '*' * len(self.password),
                    f'Keyboard: {self.layout.upper()}' + (' — Caps Lock ON' if self.caps else ''),
                    self.message, 'Enter: connect   Backspace: erase   Esc: back', 'Or connect an Ethernet cable.')

    def inputs(self):
        import evdev
        available = {d['path']: d for d in input_devices()}
        self.keyboard = any(d['keyboard'] for d in available.values())
        for path in list(self.devices):
            if path not in available:
                self.devices.pop(path).close()
                self.held.pop(path, None)
        for path in available.keys() - self.devices.keys():
            try:
                device = evdev.InputDevice(path)
                device.grab()
                self.devices[path] = device
            except OSError:
                if 'device' in locals():
                    device.close()
        for path, device in list(self.devices.items()):
            try:
                for event in device.read():
                    if event.type == 0 and event.code == 3:
                        self.held.pop(path, None)
                    elif event.type == 1:
                        # Only an actual text keyboard can enter Wi-Fi secrets.
                        if not self.active or available[path]['keyboard']:
                            self.key(event.code, event.value, path)
            except BlockingIOError:
                pass
            except OSError:
                device.close()
                self.devices.pop(path, None)
                self.held.pop(path, None)

    async def run(self):
        try:
            while read_optional(Path('/data/store/installation.json')).get('state') == 'unconfigured':
                screen = self.screen()
                self.inputs()
                atomic_json(SCREEN, {'page': screen, 'time': time.monotonic(), 'boot_id': boot_id()}, 0o600, durable=False)
                await asyncio.sleep(.1)
        finally:
            self.password = ''
            SCREEN.unlink(missing_ok=True)
            INPUT.unlink(missing_ok=True)
            for device in self.devices.values():
                device.close()
            if self.task:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)


async def run():
    while True:
        try:
            if read_optional(Path('/data/store/installation.json')).get('state') == 'unconfigured':
                await Onboarding().run()
        except (OSError, ValueError):
            pass
        await asyncio.sleep(2)
