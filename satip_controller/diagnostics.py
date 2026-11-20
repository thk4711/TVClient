"""Read-only selected-device listing. No grabs, tuning, or authenticated dumps."""
import argparse
import json
from pathlib import Path

from .config import Config
from .input import selected


def main():
    import evdev
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/etc/satip-player/config.json')
    args = parser.parse_args()
    config = Config.load(args.config)
    result = {'drm_available': Path(config.drm_device).exists(), 'devices': []}
    for path in evdev.list_devices():
        device = None
        try:
            device = evdev.InputDevice(path)
            group = selected(device, config)
            if group:
                result['devices'].append({'path':path,'name':device.name,'group':group,
                    'vendor':device.info.vendor,'product':device.info.product})
        except OSError:
            continue
        finally:
            if device:
                device.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
