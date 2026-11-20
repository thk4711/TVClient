"""Passive, secret-free mpv/cache/rendering trace; no stream, input or tuning changes."""
import argparse
import asyncio
import contextlib
import json
import time

from satip_controller.config import Config
from satip_controller.mpv_ipc import MPVIPC
from satip_controller.state import runtime_path


async def monitor(config, seconds):
    reader, writer = await asyncio.open_unix_connection(str(config.runtime_dir / 'mpv.sock'))
    ipc = await MPVIPC.connect(config.runtime_dir / 'mpv.sock')
    properties = ['time-pos', 'paused-for-cache', 'cache-buffering-state', 'avsync',
                  'frame-drop-count', 'decoder-frame-drop-count', 'vo-delayed-frame-count',
                  'audio-buffer', 'demuxer-cache-state', 'aid', 'video-codec']
    latest = {}
    started = time.monotonic()
    events = []
    samples = []

    async def collect():
        while line := await reader.readline():
            message = json.loads(line)
            event = message.get('event')
            at = round(time.monotonic() - started, 4)
            if event == 'property-change' and message.get('name') in properties:
                name, data = message['name'], message.get('data')
                if name == 'demuxer-cache-state' and isinstance(data, dict):
                    data = {key: data.get(key) for key in
                            ('cache-duration', 'underrun', 'raw-input-rate', 'fw-bytes', 'eof')}
                latest[name] = data
                if name in ('paused-for-cache', 'aid'):
                    events.append({'at': at, 'property': name, 'value': data})
            elif event in ('start-file', 'file-loaded', 'playback-restart', 'end-file', 'audio-reconfig', 'video-reconfig'):
                events.append({'at': at, 'event': event})

    collector = asyncio.create_task(collect())
    try:
        for index, name in enumerate(properties):
            writer.write((json.dumps({'command': ['observe_property', index, name]}) + chr(10)).encode())
        await writer.drain()
        last_gpu = 0
        gpu = {}
        while time.monotonic() - started < seconds:
            now = time.monotonic()
            if now - last_gpu >= .5:
                with contextlib.suppress(Exception):
                    passes = await ipc.get('vo-passes')
                    gpu = {kind: [{'desc': p['desc'], 'last_ms': round(p['last'] / 1e6, 3),
                                   'avg_ms': round(p['avg'] / 1e6, 3), 'peak_ms': round(p['peak'] / 1e6, 3)}
                                  for p in rows] for kind, rows in passes.items()}
                last_gpu = now
            files = {}
            for name, path in [('state', config.state_dir / 'state.json'),
                               ('status', runtime_path(config.state_dir, 'status.json')),
                               ('epg', runtime_path(config.management_dir, 'epg.json'))]:
                with contextlib.suppress(OSError):
                    files[name] = path.stat().st_mtime_ns
            samples.append({'at': round(time.monotonic() - started, 4), **latest,
                            'file_mtimes': files, 'gpu': gpu})
            if collector.done():
                collector.result()
                raise ValueError('mpv event connection ended')
            await asyncio.sleep(.1)
    finally:
        collector.cancel()
        await asyncio.gather(collector, return_exceptions=True)
        writer.close()
        await writer.wait_closed()
        await ipc.close()
    print(json.dumps({'seconds': seconds, 'events': events, 'samples': samples}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/satip-player/config.json')
    parser.add_argument('--seconds', type=int, choices=range(1, 301), default=60)
    args = parser.parse_args()
    asyncio.run(monitor(Config.load(args.config), args.seconds))
