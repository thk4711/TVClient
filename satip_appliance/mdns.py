"""Publish the receiver's local web address using the packaged zeroconf library."""
import asyncio
import re
import socket
from pathlib import Path

from satip_controller.state import atomic_json


def local_name():
    # A short hardware suffix makes several freshly flashed receivers coexist.
    try:
        suffix = (Path('/sys/class/net/eth0/address').read_text().strip().replace(':', '')[-6:])
        if re.fullmatch(r'[0-9a-f]{6}', suffix):
            return 'satip-' + suffix + '.local'
    except OSError:
        pass
    return socket.gethostname() + '.local'


async def advertise():
    from .onboarding import confirmed_address
    from zeroconf import IPVersion, ServiceInfo
    from zeroconf.asyncio import AsyncZeroconf
    previous = None
    service = zeroconf = None
    path = Path('/run/satip-network/mdns.json')
    try:
        while True:
            addresses = tuple(filter(None, (confirmed_address('eth0'), confirmed_address('wlan0'))))
            if addresses != previous:
                path.unlink(missing_ok=True)
                if zeroconf:
                    await zeroconf.async_unregister_service(service)
                    await zeroconf.async_close()
                    zeroconf = None
                if addresses:
                    zeroconf = AsyncZeroconf(interfaces=list(addresses), ip_version=IPVersion.V4Only)
                    service = ServiceInfo('_http._tcp.local.', local_name()[:-6] + '._http._tcp.local.',
                                          addresses=[socket.inet_aton(a) for a in addresses],
                                          port=8080, properties={'path': '/'}, server=local_name() + '.')
                    await zeroconf.async_register_service(service, allow_name_change=True)
                    atomic_json(path, {'name': local_name()}, 0o644, durable=False)
                previous = addresses
            await asyncio.sleep(2)
    finally:
        path.unlink(missing_ok=True)
        if zeroconf:
            await zeroconf.async_close()


async def run():
    while True:
        try:
            await advertise()
        except (OSError, ValueError, ImportError):
            # Numeric-address onboarding remains available while discovery retries.
            await asyncio.sleep(10)
