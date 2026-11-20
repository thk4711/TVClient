#!/usr/bin/env python3
"""Run ON the development receiver, detached from Wi-Fi SSH, during a rehearsal.

Exercises an incorrect password/rollback, then the saved working password using
the real HDMI coordinator and DHCP checks. Reads credentials locally; never prints
them. Temporarily disconnects Wi-Fi. Requires an unconfigured rehearsal store.
"""
import asyncio
import os
from pathlib import Path
import secrets
import time

from satip_appliance.device_settings import saved_preferences
from satip_appliance.provision import parser
from satip_appliance.onboarding import Onboarding, confirmed_address
from satip_controller.state import read_json


async def wait_for_network():
    async with asyncio.timeout(40):
        while not confirmed_address('wlan0'):
            await asyncio.sleep(1)


async def exercise():
    if os.geteuid() != 0 or read_json(Path('/data/store/installation.json')).get('state') != 'unconfigured':
        raise ValueError('Run as root during a fresh-setup rehearsal')
    values = saved_preferences(Path('/'), parser())
    assert confirmed_address('wlan0') and values.get('wifi_ssid')
    flow = Onboarding()
    flow.selected = values['wifi_ssid']
    flow.country_index = next(i for i, c in enumerate(flow.countries) if c[0] == values['wifi_country'])
    print('Trying an incorrect Wi-Fi password; automatic rollback expected.', flush=True)
    began = time.monotonic()
    flow.password = secrets.token_urlsafe(24)
    await flow.connect()
    assert flow.phase == 'password' and not flow.password and flow.trial is None
    await wait_for_network()
    assert not Path('/data/store/network/wifi-trial.json').exists()
    print(f'Incorrect password rejected; original DHCP connection restored after {time.monotonic()-began:.1f}s.', flush=True)
    print('Trying the saved working password through the HDMI coordinator.', flush=True)
    began = time.monotonic()
    flow.password = values['wifi_psk']
    await flow.connect()
    assert flow.phase == 'network' and flow.trial is None and not flow.password
    await wait_for_network()
    assert not Path('/data/store/network/wifi-trial.json').exists()
    print(f'Wi-Fi associated, DHCP verified and settings committed after {time.monotonic()-began:.1f}s.', flush=True)
    print('PASS', flush=True)


if __name__ == '__main__':
    asyncio.run(exercise())
