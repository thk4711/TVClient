#!/usr/bin/env python3
"""Interrupt HDMI briefly to verify finite idle-mux release and player restart."""
import argparse
import asyncio
import json
import os
import signal
from pathlib import Path
import time

from satip_controller.audio import normal_german
from satip_controller.backend import Backend
from satip_controller.config import Config, credentials
from satip_controller.coordination import preview_held
from satip_controller.lifecycle import complete
from satip_controller.mpv_ipc import MPVIPC
from satip_controller.state import read_json, runtime_path, lease_active


def active_inputs(rows):
    # Enabled idle frontends remain in status/inputs with no stream name.
    return [row for row in rows if row.get('stream')]


async def exercise(args):
    if os.geteuid() != 0 or not args.confirm_interrupt:
        raise ValueError('Root and --confirm-interrupt required; exclude physical controls and other validation')
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    config = Config.load(args.config)
    for name in ('scan-lease.json', 'preview-lease.json'):
        path = runtime_path(config.management_dir, name)
        lease = read_json(path) if path.exists() else {}
        if lease.get('recovery_pending') or (preview_held(lease) if name.startswith('preview') else lease_active(lease)):
            raise ValueError('Finish scan/preview/recovery first')
    def status():
        return read_json(runtime_path(config.state_dir, 'status.json'))
    original = status()
    if not original.get('playing') or original.get('paused') or original.get('error'):
        raise ValueError('Healthy live HDMI playback required')
    backend = Backend(credentials(Path(args.admin_credentials)), timeout=1)
    from validation.measure_switching_on_pi import MuxGrace
    native_grace = await MuxGrace(backend, args.frontend_uuid, 1000).read()
    if native_grace <= 0:
        raise ValueError('Enable native idle mux grace before this release check')
    if len(await backend.grid('status/subscriptions')) != 1 or len(active_inputs(await backend.grid('status/inputs'))) != 1:
        raise ValueError('Exactly one playback subscription and active input required')
    stopped = False
    no_subscription = released = None
    began = time.monotonic()
    async def stop_and_restore():
        nonlocal stopped, no_subscription, released
        process = await asyncio.create_subprocess_exec('/usr/bin/python3', '-m', 'satip_appliance.supervisor', 'player', 'stop')
        try:
            async with asyncio.timeout(15):
                while released is None:
                    subscriptions = await backend.grid('status/subscriptions')
                    inputs = active_inputs(await backend.grid('status/inputs'))
                    now = time.monotonic()
                    if len(subscriptions) > 1 or len(inputs) > 1:
                        raise ValueError('Unexpected tuner owner during release check')
                    if not subscriptions and no_subscription is None:
                        no_subscription = now
                    if no_subscription is not None:
                        if not inputs:
                            released = now
                        elif now - no_subscription > 3:
                            raise ValueError('Idle tuner was not released within the two-second bound plus observation margin')
                    await asyncio.sleep(.05)
        finally:
            # A single owned cleanup finishes stop AND restoration despite cancellation.
            async def restore_player():
                nonlocal stopped
                await process.wait()
                stopped = process.returncode == 0
                if stopped:
                    child = await asyncio.create_subprocess_exec('/usr/bin/python3', '-m', 'satip_appliance.supervisor', 'player', 'start')
                    if await child.wait() != 0:
                        raise ValueError('Player restart failed')
            await restore_player()
    await complete(asyncio.create_task(stop_and_restore()))
    if not stopped:
        raise ValueError('Player stop failed; inspect supervisor before another operation')
    async with asyncio.timeout(35):
        while True:
            state = status()
            if (state.get('owner_pid') != original['owner_pid'] and state.get('playing') == original['playing']
                    and not state.get('paused') and not state.get('error')):
                break
            await asyncio.sleep(.1)
    ipc = await MPVIPC.connect(config.runtime_dir / 'mpv.sock')
    try:
        tracks = await ipc.get('track-list')
        if not any(track.get('selected') and normal_german(track) for track in tracks):
            raise ValueError('Normal German audio was not restored')
        before = [await ipc.get(name) for name in ('time-pos', 'audio-pts')]
        await asyncio.sleep(1)
        after = [await ipc.get(name) for name in ('time-pos', 'audio-pts')]
        if any(type(a) not in (int, float) or type(b) not in (int, float) or b <= a for a, b in zip(before, after)):
            raise ValueError('Audio/video did not advance after restart')
    finally:
        await ipc.close()
    if len(await backend.grid('status/subscriptions')) != 1:
        raise ValueError('Single playback ownership not restored')
    print(json.dumps({'native_grace_ms': native_grace, 'released_after_subscription_zero_seconds': round(released - no_subscription, 3),
                      'released_after_stop_request_seconds': round(released - began, 3),
                      'restored_channel': original['playing'], 'audio_video_advanced': True,
                      'normal_german_audio': True, 'physical_picture_audio_verified': False}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm-interrupt', action='store_true')
    parser.add_argument('--frontend-uuid', required=True)
    parser.add_argument('--config', default='/etc/satip-player/config.json')
    parser.add_argument('--admin-credentials', default='/etc/satip-web/tvheadend-admin.json')
    asyncio.run(exercise(parser.parse_args()))
