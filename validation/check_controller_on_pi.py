"""Short explicit root-VT integration check. No scan or backend configuration changes."""
import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

from satip_controller.config import Config, library
from satip_controller.controller import Controller
from satip_controller.input import Inputs
from satip_controller.player import Player
from satip_controller.state import State, atomic_json, read_json


async def check(config, output):
    channels, favourites=library(read_json(config.library))
    state=State(config.state_dir/'state.json',favourites)
    player=Player(config,state)
    controller=Controller(config,player,state)
    inputs=Inputs(config,controller.action)
    tasks=[asyncio.create_task(controller.run()),asyncio.create_task(inputs.run())]
    result={'switches':[],'checks':{},'physical_input_verified':False}
    try:
        async with asyncio.timeout(75):
            for channel in (favourites[0],favourites[-1],favourites[1],favourites[0]):
                started=time.monotonic()
                controller.request(channel)
                while controller.playing!=channel:
                    if any(t.done() for t in tasks):
                        for task in tasks:
                            if task.done(): task.result()
                    if time.monotonic()-started > 8 and player.ipc:
                        snapshot = {'error':controller.error,'loading':controller.loading,'playing':controller.playing}
                        for prop in ('path','idle-active','time-pos','video-codec','audio-codec','track-list','aid'):
                            try: snapshot[prop] = await player.ipc.get(prop)
                            except Exception: snapshot[prop] = 'unavailable'
                        result['diagnostic'] = snapshot
                        if time.monotonic()-started > 15:
                            raise TimeoutError('Useful playback did not become ready')
                    await asyncio.sleep(.1)
                ready_seconds=round(time.monotonic()-started,2)
                await asyncio.sleep(1)
                result['switches'].append({'channel':channel,'seconds':ready_seconds,
                    'vf':await player.ipc.get('vf'), 'hwdec':await player.ipc.get('hwdec-current'),
                    'aid':await player.ipc.get('aid'), 'tracks':await player.ipc.get('track-list'),
                    'avsync':await player.ipc.get('avsync'), 'drops':await player.ipc.get('frame-drop-count')})
            await controller.action('volume_up')
            result['checks']['volume_up']=await player.ipc.get('volume')==40
            await controller.action('mute')
            result['checks']['mute']=await player.ipc.get('mute') is True
            await controller.action('mute');await controller.action('volume_down')
            await controller.action('menu');await controller.action('next')
            result['checks']['menu_no_tune']=controller.requested==favourites[0]
            await controller.action('back')
            result['checks']['menu_closed']=controller.menu_index is None
            result['checks']['input_devices']=len(inputs.devices)
            result['checks']['state_volume']=state.data['volume']==35
            await controller.osd.show('Österreich — literal \u0024{volume} {\\b1}',3000)
            await asyncio.sleep(3.5)
            result['passed']=all(v for v in result['checks'].values())
    except Exception as error:
        result['passed']=False
        result['error_type']=type(error).__name__
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        result['child_reaped']=player.process is None
        result['auth_removed']=not (config.runtime_dir/'auth.conf').exists()
        atomic_json(output,result)
    print(json.dumps(result,ensure_ascii=False))
    if not result['passed']: raise SystemExit(1)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    asyncio.run(check(Config.load(args.config),Path(args.output)))
