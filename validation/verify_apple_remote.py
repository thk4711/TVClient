"""Verify mapped Apple Remote taps and volume repeats in one guided session."""

import argparse
import json
import os
from pathlib import Path
import select
import stat
import statistics
import time

from learn_apple_remote import BUTTONS, find_receiver, save_json


def summarize(events):
    presses = [e for e in events if e["value"] == 1]
    releases = [e for e in events if e["value"] == 0]
    repeats = [e for e in events if e["value"] == 2]
    if not presses or not releases:
        raise ValueError("A complete press/release pair was not received")
    result = {"presses": len(presses), "releases": len(releases), "repeats": len(repeats)}
    result["duration_ms"] = round((releases[-1]["time"] - presses[0]["time"]) * 1000, 1)
    if repeats:
        result["repeat_delay_ms"] = round((repeats[0]["time"] - presses[0]["time"]) * 1000, 1)
    if len(repeats) > 1:
        result["repeat_period_ms"] = round(statistics.median(
            (b["time"] - a["time"]) * 1000 for a, b in zip(repeats, repeats[1:])
        ), 1)
    return result


class Keys:
    def __init__(self, device, key_names, log):
        self.device, self.key_names, self.log = device, key_names, log
        self.pending = []

    def next(self, timeout):
        end = time.monotonic() + timeout
        while True:
            if self.pending:
                return self.pending.pop(0)
            remaining = end - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([self.device], [], [], remaining)
            if not ready:
                return None
            for event in self.device.read():
                if event.type != 1:  # EV_KEY only, not duplicate MSC_SCAN data.
                    continue
                item = {"code": event.code, "value": event.value, "time": event.timestamp(),
                        "key": self.key_names.get(event.code, str(event.code))}
                self.log.write(json.dumps(item) + "\n")
                self.log.flush()
                self.pending.append(item)

    def quiet(self):
        end = time.monotonic() + 8
        while self.next(0.35) is not None:
            if time.monotonic() >= end:
                raise ValueError("Release the remote button before continuing")

    def press(self, code, timeout):
        end = time.monotonic() + timeout
        while True:
            event = self.next(max(0, end - time.monotonic()))
            if event is None:
                raise TimeoutError("No press received")
            if event["value"] != 1:
                continue
            if event["code"] != code:
                raise ValueError("Wrong button: " + event["key"])
            break
        events = [event]
        released = False
        end = time.monotonic() + 10
        while time.monotonic() < end:
            wait = min(0.35 if released else 10, end - time.monotonic())
            event = self.next(wait)
            if event is None:
                if released:
                    return events
                raise TimeoutError("Release event not received")
            if event["code"] != code:
                raise ValueError("Unexpected additional key: " + event["key"])
            events.append(event)
            released = event["value"] == 0
        raise TimeoutError("Button held too long")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-tty")
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 120:
        parser.error("timeout must be between 1 and 120 seconds")
    import evdev  # Already installed on the Pi; not needed for pure tests.

    os.umask(0o077)
    rc = find_receiver()
    nodes = list(rc.glob("input*/event*"))
    if len(nodes) != 1:
        raise RuntimeError("Expected one event device for the GPIO receiver")
    device = evdev.InputDevice("/dev/input/" + nodes[0].name)
    directory = Path("/var/lib/satip-validation") / time.strftime("ir-key-test-%Y%m%d-%H%M%S")
    directory.mkdir(mode=0o700)
    path = directory / "result.json"
    result = {"status": "testing", "device": device.path, "buttons": {}, "warnings": []}
    terminal = None
    grabbed = False

    def say(text):
        print(text, flush=True)
        if terminal:
            terminal.write("\n" + text + "\n")
            terminal.flush()

    try:
        if device.name != "gpio_ir_recv":
            raise RuntimeError("Refusing to grab a non-GPIO input device")
        if args.prompt_tty:
            if not stat.S_ISCHR(os.stat(args.prompt_tty).st_mode):
                raise RuntimeError("Prompt destination is not a terminal")
            fd = os.open(args.prompt_tty, os.O_WRONLY | os.O_NOCTTY)
            if not os.isatty(fd):
                os.close(fd)
                raise RuntimeError("Prompt destination is not a terminal")
            terminal = os.fdopen(fd, "w", buffering=1)
        device.grab()  # Only this GPIO receiver, never the USB keyboard/CEC.
        grabbed = True
        names = {evdev.ecodes.ecodes[b.key]: b.key for b in BUTTONS}
        with (directory / "events.jsonl").open("x") as log:
            capture = Keys(device, names, log)
            say("MAPPED KEY TEST: one short tap per prompt. No TV control is implemented yet.")
            for index, button in enumerate(BUTTONS, 1):
                for attempt in range(3):
                    capture.quiet()
                    say(f"{index}/8 TAP ONCE AND WAIT: {button.prompt}")
                    try:
                        events = capture.press(evdev.ecodes.ecodes[button.key], args.timeout)
                        summary = summarize(events)
                    except ValueError as error:
                        say(f"Retry: {error}")
                        if attempt == 2:
                            raise
                        continue
                    result["buttons"][button.name] = summary
                    if summary["presses"] != 1 or summary["repeats"]:
                        result["warnings"].append(f"{button.name}: {summary}; controller debounce/repeat policy needed")
                    say(f"Received {button.key}: {summary}")
                    save_json(path, result)
                    break
            capture.quiet()
            say("8/8 HOLD RIGHT for about 3 seconds, then release.")
            events = capture.press(evdev.ecodes.KEY_VOLUMEUP, args.timeout)
            hold = summarize(events)
            result["volume_hold"] = hold
            if hold["repeats"] < 2 or hold["duration_ms"] < 1000:
                raise ValueError("Volume hold did not produce repeated events; repeat behaviour needs investigation")
            say(f"Volume hold: {hold}")
        result["status"] = "verified-with-notes" if result["warnings"] else "verified"
        save_json(path, result)
        say(f"INPUT TEST COMPLETE: {path}. Persistence/reboot testing is still pending.")
        print("RESULT_JSON", path, flush=True)
        return 0
    except (RuntimeError, ValueError, TimeoutError, OSError, KeyboardInterrupt) as error:
        result.update(status="incomplete", error=str(error))
        save_json(path, result)
        say(f"Stopped: {error}. Results are in {path}.")
        return 1
    finally:
        if grabbed:
            try:
                device.ungrab()
            except OSError:
                pass
        device.close()
        if terminal:
            terminal.close()


if __name__ == "__main__":
    raise SystemExit(main())
