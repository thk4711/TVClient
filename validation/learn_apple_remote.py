"""Learn and validate an Apple A1294 in one guided rc-core session."""

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import select
import stat
import subprocess
import time


@dataclass(frozen=True)
class Button:
    name: str
    prompt: str
    key: str
    legacy_codes: tuple


# From the DBMandrake A1294 lircd.conf supplied by the user (2015).
BUTTONS = (
    Button("up", "Up / + (top of ring)", "KEY_CHANNELUP", (0x50,)),
    Button("down", "Down / - (bottom of ring)", "KEY_CHANNELDOWN", (0x30,)),
    Button("left", "Left (left of ring)", "KEY_VOLUMEDOWN", (0x90,)),
    Button("right", "Right (right of ring)", "KEY_VOLUMEUP", (0x60,)),
    Button("select", "Centre Select", "KEY_MUTE", (0x3A, 0xA0)),
    Button("menu", "Menu", "KEY_INFO", (0xC0,)),
    Button("playpause", "Play / Pause (separate bottom button)", "KEY_PLAYPAUSE", (0xFA, 0xA0)),
)


def reverse_byte(value):
    if not 0 <= value <= 255:
        raise ValueError("Expected an 8-bit value")
    return int(f"{value:08b}"[::-1], 2)


def legacy_to_nec32(code, post_data=0x2C):
    """Linux NEC32 uses aaAAddDD, not the legacy LIRC on-wire bit order."""
    address = reverse_byte(0x77)
    address_inverse = reverse_byte(0xE1)
    command = reverse_byte(code)
    data_inverse = reverse_byte(post_data)
    return address_inverse << 24 | address << 16 | data_inverse << 8 | command


# LIRC ignore_mask 0x80ff becomes native NEC32 mask 0x0000ff01:
# ignore the remote-ID byte and command bit 0. Do not modify remote pairing.
IGNORE_MASK = legacy_to_nec32(0x80, 0xFF) ^ legacy_to_nec32(0, 0)
APPLE_PREFIX = legacy_to_nec32(0) & 0xFFFF0000
COMMON_TRAILER = legacy_to_nec32(0xA0) & ~IGNORE_MASK


@dataclass(frozen=True)
class Frame:
    protocol: str
    scancode: int
    repeat: bool = False


def parse_frame(line):
    if "lirc protocol(" not in line or "scancode" not in line:
        return None  # Ignore the duplicate EV_MSC event.
    try:
        protocol = line.split("protocol(", 1)[1].split(")", 1)[0]
        token = line.split("scancode", 1)[1].split("=", 1)[1].strip().split()[0]
        return Frame(protocol, int(token, 16), "repeat" in line)
    except (ValueError, IndexError):
        return None


def normalized(code):
    if not 0 <= code <= 0xFFFFFFFF or code & 0xFFFF0000 != APPLE_PREFIX:
        raise ValueError("Received a non-Apple NEC32 address")
    return code & ~IGNORE_MASK


def classify_press(button, frames):
    fresh = [frame for frame in frames if not frame.repeat]
    if not fresh:
        raise ValueError("Only repeats received; release and tap once")
    if any(frame.protocol != "nec32" for frame in fresh):
        raise ValueError("Expected the Apple NEC32 protocol")
    codes = {normalized(frame.scancode) for frame in fresh}
    expected = normalized(legacy_to_nec32(button.legacy_codes[0]))
    if len(button.legacy_codes) > 1:
        codes.discard(COMMON_TRAILER)
    if codes != {expected}:
        received = ", ".join(hex(code) for code in sorted(codes)) or "only the shared trailer"
        raise ValueError(f"Wrong/incomplete button sequence ({received}); tap {button.prompt}")
    return {
        "normalized_primary": expected,
        "key": button.key,
        "frames": [asdict(frame) for frame in frames],
        "remote_ids": sorted({frame.scancode >> 8 & 0xFF for frame in fresh}),
    }


def build_keymap(buttons, all_remote_ids=False):
    mappings = {}
    for name, record in buttons.items():
        primary = record["normalized_primary"]
        if primary == COMMON_TRAILER:
            raise ValueError("Shared Select/Play trailer must not trigger a key")
        ids = range(256) if all_remote_ids else record["remote_ids"]
        for remote_id in ids:
            for flag in (0, 1):
                scancode = primary | remote_id << 8 | flag
                if scancode in mappings and mappings[scancode] != record["key"]:
                    raise ValueError(f"Conflicting mapping for {name}")
                mappings[scancode] = record["key"]
    return mappings


def parse_keytable(text):
    table = {}
    for line in text.splitlines():
        if not line.startswith("scancode "):
            continue
        parts = line.split()
        if len(parts) < 4 or parts[2] != "=":
            raise ValueError("Malformed keytable readback")
        table[int(parts[1], 16)] = parts[3]
    return table


def verify_keytable(expected, actual):
    missing = [hex(code) for code, key in expected.items() if actual.get(code) != key]
    if missing:
        raise RuntimeError("Incomplete/wrong kernel keytable: " + ", ".join(missing[:5]))
    if any(code & 0xFFFF0000 == APPLE_PREFIX and normalized(code) == COMMON_TRAILER
           and key != "KEY_RESERVED" for code, key in actual.items()):
        raise RuntimeError("An existing shared-trailer mapping would cause duplicate actions")


def load_keymap(receiver, keymap, mappings, output):
    # Keep stdout/stderr separate: protocol messages can corrupt mapping rows
    # when streams are merged. ir-keytable can also exit 0 after ioctl errors.
    previous = subprocess.run(
        ["ir-keytable", "-s", receiver.name, "-r"], capture_output=True, text=True, check=True
    )
    with (output / "previous-keytable.txt").open("x") as file:
        file.write(previous.stdout + "\n" + previous.stderr)
    loaded = subprocess.run(
        ["ir-keytable", "-s", receiver.name, "-w", str(keymap)], capture_output=True, text=True
    )
    if loaded.returncode or "Cannot allocate memory" in loaded.stdout + loaded.stderr:
        raise RuntimeError("Kernel rejected keymap; do not persist it. Existing-table backup is saved")
    readback = subprocess.run(
        ["ir-keytable", "-s", receiver.name, "-r"], capture_output=True, text=True, check=True
    )
    verify_keytable(mappings, parse_keytable(readback.stdout))


def render_toml(mappings, all_remote_ids):
    lines = [
        "# Generated from confirmed Apple A1294 captures.",
        "# Legacy ignore_mask is expanded into native NEC32 entries.",
        "# Common Select/Play trailer is intentionally unmapped.",
        f"# Remote IDs: {'all' if all_remote_ids else 'observed only'}",
        "[[protocols]]",
        'name = "apple-a1294-satip"',
        'protocol = "nec"',
        'variant = "nec32"',
        "[protocols.scancodes]",
    ]
    lines.extend(f'0x{code:08x} = "{key}"' for code, key in sorted(mappings.items()))
    return "\n".join(lines) + "\n"


def find_receiver(root=Path("/sys/class/rc")):
    devices = [
        device for device in root.glob("rc*")
        if any(p.read_text().strip() == "gpio_ir_recv" for p in device.glob("input*/name"))
    ]
    if len(devices) != 1:
        raise RuntimeError("Expected exactly one gpio_ir_recv; refusing to select HDMI CEC")
    if "[nec]" not in (devices[0] / "protocols").read_text():
        raise RuntimeError(f"Enable NEC first: ir-keytable -s {devices[0].name} -p nec")
    return devices[0]


class Capture:
    def __init__(self, receiver, log):
        self.process = subprocess.Popen(
            ["stdbuf", "-oL", "-eL", "ir-keytable", "-s", receiver.name, "-t"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        self.log = log
        self.pending = b""
        self.frames = []

    def next_frame(self, timeout):
        end = time.monotonic() + timeout
        while True:
            if self.frames:
                return self.frames.pop(0)
            remaining = end - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                return None
            data = os.read(self.process.stdout.fileno(), 4096)
            if not data:
                raise RuntimeError("ir-keytable stopped unexpectedly")
            self.pending += data
            while b"\n" in self.pending:
                raw, self.pending = self.pending.split(b"\n", 1)
                line = raw.decode("utf8", "replace")
                self.log.write(line + "\n")
                self.log.flush()
                frame = parse_frame(line)
                if frame:
                    self.frames.append(frame)

    def quiet(self, gap=0.35, maximum=4):
        end = time.monotonic() + maximum
        while self.next_frame(gap) is not None:
            if time.monotonic() > end:
                raise ValueError("Release the remote button before continuing")

    def press(self, timeout, gap=0.35):
        end = time.monotonic() + timeout
        while True:
            frame = self.next_frame(max(0, end - time.monotonic()))
            if frame is None:
                raise TimeoutError("No new press received")
            if not frame.repeat:
                break
        frames = [frame]
        end = time.monotonic() + 3
        while time.monotonic() < end:
            frame = self.next_frame(min(gap, end - time.monotonic()))
            if frame is None:
                return frames
            frames.append(frame)
        raise ValueError("Button held too long; release it and make one short tap")

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()


def save_json(path, data):
    temporary = path.with_suffix(".new")
    with temporary.open("x") as output:
        json.dump(data, output, indent=2)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=Path, help="Resume previously confirmed button captures")
    parser.add_argument("--output-dir", type=Path, default=Path("/var/lib/satip-validation"))
    parser.add_argument("--prompt-tty", help="Also show prompts on this local terminal, e.g. /dev/tty1")
    parser.add_argument("--timeout", type=float, default=60, help="Seconds allowed for each tap")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--this-remote-only", action="store_true", help="Default: use observed remote IDs")
    scope.add_argument("--all-remote-ids", action="store_true", help="Export all IDs; too large for this Pi's keytable")
    parser.add_argument("--apply", action="store_true", help="Load generated entries on the GPIO receiver only")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 120:
        parser.error("timeout must be between 1 and 120 seconds")
    if args.all_remote_ids and args.apply:
        parser.error("All-ID export exceeds this Pi's 512-entry limit; use the compact default for --apply")
    os.umask(0o077)
    receiver = find_receiver()
    output = args.output_dir / time.strftime("apple-remote-%Y%m%d-%H%M%S")
    output.mkdir(mode=0o700)
    terminal = None
    capture = None
    result = {"schema_version": 1, "status": "learning", "receiver": receiver.name, "buttons": {}}
    result_path = output / "learned.json"

    def say(text):
        print(text, flush=True)
        if terminal:
            terminal.write("\n" + text + "\n")
            terminal.flush()

    try:
        if args.prompt_tty:
            if not stat.S_ISCHR(os.stat(args.prompt_tty).st_mode):
                raise RuntimeError("Prompt destination must be a terminal, not a regular file")
            fd = os.open(args.prompt_tty, os.O_WRONLY | os.O_NOCTTY)
            if not os.isatty(fd):
                os.close(fd)
                raise RuntimeError("Prompt destination is not a terminal")
            terminal = os.fdopen(fd, "w", buffering=1)
        if args.seed:
            seed = json.loads(args.seed.read_text()).get("buttons", {})
            for button in BUTTONS:
                if button.name not in seed:
                    continue
                record = seed[button.name]
                if "frames" in record:
                    frames = [Frame(**frame) for frame in record["frames"]]
                else:
                    frames = [Frame(record["protocol"], int(record["scancode"], 16))]
                confirmed = classify_press(button, frames)
                confirmed["source"] = str(args.seed)
                result["buttons"][button.name] = confirmed
        save_json(result_path, result)
        say("APPLE REMOTE LEARNING: point at the Argon case. ONE short tap per prompt; release it.")
        say("No Enter or chat replies are needed. Centre/Play multi-code trailers are handled automatically.")
        with (output / "capture.log").open("x") as log:
            capture = Capture(receiver, log)
            for index, button in enumerate(BUTTONS, 1):
                if button.name in result["buttons"]:
                    say(f"{index}/7 {button.prompt}: already confirmed; skipped.")
                    continue
                for attempt in range(3):
                    capture.quiet()
                    say(f"{index}/7 TAP NOW: {button.prompt}")
                    try:
                        frames = capture.press(args.timeout)
                        record = classify_press(button, frames)
                    except ValueError as error:
                        say(f"Retry: {error}")
                        if attempt == 2:
                            raise
                        continue
                    result["buttons"][button.name] = record
                    save_json(result_path, result)
                    say("Confirmed: " + ", ".join(hex(f.scancode) for f in frames if not f.repeat))
                    break
            capture.close()
            capture = None
        mappings = build_keymap(result["buttons"], args.all_remote_ids)
        keymap = output / "apple-a1294-satip.toml"
        with keymap.open("x") as file:
            file.write(render_toml(mappings, args.all_remote_ids))
        result.update(status="learned", keymap=str(keymap), entries=len(mappings), applied=False)
        save_json(result_path, result)
        if args.apply:
            if find_receiver() != receiver:
                raise RuntimeError("Receiver changed; refusing to load a keymap")
            load_keymap(receiver, keymap, mappings, output)
            result["applied"] = True
            save_json(result_path, result)
        say(f"COMPLETE: {keymap} ({len(mappings)} entries). Boot configuration was not changed.")
        print("RESULT_JSON", result_path, flush=True)
        return 0
    except (RuntimeError, ValueError, TimeoutError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        result.update(status="incomplete", error=str(error))
        save_json(result_path, result)
        say(f"Stopped: {error}. Partial captures are saved in {result_path}.")
        return 1
    finally:
        if capture:
            capture.close()
        if terminal:
            terminal.close()


if __name__ == "__main__":
    raise SystemExit(main())
