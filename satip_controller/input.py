"""USB keyboards, GPIO infrared and selected USB remotes; native repeat/hotplug."""
import asyncio
import logging
import time

# Linux input codes, kept independent of evdev so policy tests run on macOS.
BINDINGS = {103: "next", 108: "previous", 106: "volume_up", 105: "volume_down",
            402: "next", 403: "previous", 115: "volume_up", 114: "volume_down",
            113: "menu", 28: "menu", 352: "menu", 50: "pause", 164: "pause",
            358: "info", 23: "info", 1: "back", 139: "back"}
REPEATING = {"volume_up", "volume_down", "remote_right", "remote_left"}
LOG = logging.getLogger(__name__)


def binding(group, code):
    if group == "remote" and code in (105, 106, 114, 115):
        return "remote_right" if code in (106, 115) else "remote_left"
    if code == 164:
        return "pause"
    if group == "keyboard":
        extra = {57: "pause", 119: "pause", 168: "seek_back", 208: "seek_forward",
                 104: "seek_back", 109: "seek_forward", 102: "live"}
        if code in extra:
            return extra[code]
    # Remote centre stays menu; former keyboard mute starts/toggles time shift.
    return "pause" if group == "keyboard" and code == 113 else BINDINGS.get(code)


class Policy:
    def __init__(self, delay=.5, interval=.125, debounce=.08):
        self.delay, self.interval, self.debounce = delay, interval, debounce
        self.held = {}
        self.last_press = {}
        self.last_repeat = {}

    def remove(self, device):
        self.held = {k: v for k, v in self.held.items() if k[0] != device}

    def feed(self, device, group, code, value, now):
        # The remote centre shares KEY_MUTE with keyboard multimedia mute.
        action = binding(group, code)
        key = (device, code)
        if value == 0:
            self.held.pop(key, None)
            return None
        if not action:
            return None
        if value == 1:
            if key in self.held:
                return None
            self.held[key] = [now, now]
            previous = self.last_press.get((group, action), -100)
            if now - previous < self.debounce:
                return None
            self.last_press[group, action] = now
            return action
        if value == 2 and key in self.held and action in REPEATING:
            start, last = self.held[key]
            group_last = self.last_repeat.get((group, action), last)
            if now-start >= self.delay and now-last >= self.interval and now-group_last >= self.interval:
                self.held[key][1] = now
                self.last_repeat[group, action] = now
                return action
        return None


def is_usb_keyboard(device, keys):
    return getattr(device.info, "bustype", 0) == 3 and {16, 28, 30, 44, 57} <= keys


def selected(device, config):
    keys = set(device.capabilities().get(1, []))
    if not keys.intersection(BINDINGS):
        return None
    if device.name == "gpio_ir_recv":
        return "remote"
    if is_usb_keyboard(device, keys):
        return "keyboard"
    if device.info.vendor == config.keyboard_vendor and device.info.product == config.keyboard_product:
        return "keyboard"
    return None


class Inputs:
    def __init__(self, config, callback, on_press=None):
        self.config, self.callback = config, callback
        self.on_press = on_press
        self.policy = Policy(config.repeat_delay, config.repeat_interval, config.debounce)
        self.devices = {}
        self.tasks = {}
        self.denied = set()

    async def run(self):
        import evdev
        try:
            while True:
                paths = set(evdev.list_devices())
                for path in paths - self.devices.keys():
                    device = None
                    try:
                        device = evdev.InputDevice(path)
                        group = selected(device, self.config)
                        if not group:
                            device.close()
                            continue
                        # EVIOCSCLOCKID: debounce/repeat must use the time the
                        # kernel received each event, not when slow archive I/O
                        # finally lets the controller drain queued key presses.
                        import fcntl
                        import struct
                        fcntl.ioctl(device.fd, 0x400445a0, struct.pack("i", time.CLOCK_MONOTONIC))
                        if self.config.grab:
                            device.grab()
                        self.devices[path] = device
                        self.tasks[path] = asyncio.create_task(self._read(device, group))
                        self.denied.discard(path)
                        LOG.info("Input connected: %s", group)
                    except (OSError, SystemError):
                        if device:
                            device.close()
                        if path not in self.denied:
                            LOG.warning("Input device unavailable (permissions or busy)")
                            self.denied.add(path)
                for path in list(self.devices):
                    if path not in paths or self.tasks[path].done():
                        await self._remove(path)
                await asyncio.sleep(1)
        finally:
            for path in list(self.devices):
                await self._remove(path)

    async def _read(self, device, group):
        try:
            async for event in device.async_read_loop():
                if event.type == 0 and event.code == 3:  # SYN_DROPPED: do not retain phantom holds
                    self.policy.remove(device.path)
                if event.type != 1:
                    continue
                # Physical key-down only, including unmapped keys. Releases and
                # native repeats must not repeatedly request tuner handover.
                if event.value == 1 and self.on_press is not None:
                    if not await self.on_press(binding(group, event.code)):
                        continue
                action = self.policy.feed(device.path, group, event.code, event.value,
                                          getattr(event, "timestamp", time.monotonic)())
                if action:
                    await self.callback(action)
        except OSError:
            pass

    async def _remove(self, path):
        task = self.tasks.pop(path)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        device = self.devices.pop(path)
        try:
            if self.config.grab:
                device.ungrab()
        except OSError:
            pass
        device.close()
        self.policy.remove(path)
        LOG.info("Input disconnected")
