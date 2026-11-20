"""Capture one button from the explicitly identified GPIO IR receiver."""

import argparse
import json
import os
from pathlib import Path
import select
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "button", choices=["up", "down", "left", "right", "select", "menu", "playpause"]
    )
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 300:
        parser.error("timeout must be between 1 and 300 seconds")

    os.umask(0o077)
    directory = Path("/var/lib/satip-validation")
    state = json.loads(Path("/run/satip-validation/ir-device.json").read_text())
    device = Path("/sys/class/rc") / state["rc_device"]
    if not any(p.read_text().strip() == "gpio_ir_recv" for p in device.glob("input*/name")):
        raise RuntimeError("Selected device is not the GPIO IR receiver")

    learned_path = directory / "ir-learned.json"
    learned = json.loads(learned_path.read_text())
    if args.button in learned["buttons"]:
        raise RuntimeError("Button is already recorded; refusing to overwrite")
    log = directory / f"ir-{args.button}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    events = []
    pending = b""
    with log.open("x") as output:
        process = subprocess.Popen(
            ["stdbuf", "-oL", "-eL", "ir-keytable", "-s", state["rc_device"], "-t"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        print("LISTENING", args.button, "LOG", log, flush=True)
        deadline = time.monotonic() + args.timeout
        try:
            while sum(not event["repeat"] for event in events) < 3:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                ready, _, _ = select.select([process.stdout], [], [], remaining)
                if not ready:
                    break
                data = os.read(process.stdout.fileno(), 4096)
                if not data:
                    break
                pending += data
                while b"\n" in pending:
                    raw, pending = pending.split(b"\n", 1)
                    line = raw.decode("utf8", "replace")
                    output.write(line + "\n")
                    output.flush()
                    print(line, flush=True)
                    if "lirc protocol(" not in line or "scancode" not in line:
                        continue
                    protocol = line.split("protocol(", 1)[1].split(")", 1)[0]
                    token = line.split("scancode", 1)[1].split("=", 1)[1].strip().split()[0]
                    events.append(
                        {"protocol": protocol, "scancode": hex(int(token, 16)), "repeat": "repeat" in line}
                    )
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if pending:
                output.write(pending.decode("utf8", "replace"))

    codes = {(event["protocol"], event["scancode"]) for event in events}
    nonrepeat_frames = sum(not event["repeat"] for event in events)
    if len(codes) != 1 or nonrepeat_frames < 3:
        print("CAPTURE_INCOMPLETE_OR_AMBIGUOUS", nonrepeat_frames, "frames", sorted(codes), flush=True)
        return 2
    protocol, scancode = next(iter(codes))
    record = {
        "protocol": protocol,
        "scancode": scancode,
        "nonrepeat_frames": nonrepeat_frames,
        "repeat_frames": sum(event["repeat"] for event in events),
        "log": str(log),
    }
    learned["buttons"][args.button] = record
    temporary = learned_path.with_suffix(".new")
    with temporary.open("x") as output:
        json.dump(learned, output)
    temporary.replace(learned_path)
    print("LEARNED_BUTTON", args.button, json.dumps(record), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
