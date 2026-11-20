"""Prepare conditional HDMI EDID overrides before any splash/player modeset."""
import fcntl
import logging
import os
import signal
from pathlib import Path
import time

from satip_controller.drm import hd_rates, modes
from satip_controller.state import atomic_json

LOG = logging.getLogger(__name__)
FIRMWARE = "edid/satip-1080p.bin"
PARAMETER = Path("/sys/module/drm/parameters/edid_firmware")


def valid_edid(data):
    if len(data) < 128 or data[:8] != bytes.fromhex("00ffffffffffff00"):
        return False
    size = (data[126] + 1) * 128
    return len(data) == size and all(sum(data[i:i + 128]) % 256 == 0
                                   for i in range(0, size, 128))


class Output:
    def __init__(self, path):
        self.path = path
        self.card, self.name = path.name.split("-", 1)

    def connected(self):
        return (self.path / "status").read_text().strip() == "connected"

    def probe(self):
        # Kernel sysfs 'detect' refreshes modes/ELD using the current EDID policy.
        (self.path / "status").write_text("detect")
        available = modes(f"/dev/dri/{self.card}", self.name)
        return (self.path / "edid").read_bytes(), hd_rates(available)


def configure(outputs, parameter=PARAMETER, firmware=Path("/lib/firmware") / FIRMWARE):
    # Never silently replace an administrator's explicit firmware policy.
    if parameter.read_text().strip():
        LOG.warning("Explicit DRM EDID override preserved; conditional selection skipped")
        return {"policy": "explicit", "outputs": []}
    results, fallback = [], []
    for output in outputs:
        if not output.connected():
            continue
        data, rates = output.probe()
        reason = None if valid_edid(data) and rates else (
            "no-1080p" if valid_edid(data) else "missing-or-invalid-edid")
        result = {"card": output.card, "connector": output.name,
                  "edid": "real" if reason is None else "fallback",
                  "reason": reason, "rates": sorted(rates)}
        results.append(result)
        if reason:
            fallback.append((output, result))
        else:
            LOG.info("%s: real EDID retained (1080p rates: %s)", output.name, sorted(rates))
    if fallback:
        if not valid_edid(firmware.read_bytes()):
            raise ValueError("Fallback EDID missing or invalid")
        policy = ",".join(f"{output.name}:{FIRMWARE}" for output, _ in fallback)
        parameter.write_text(policy)
        try:
            for output, result in fallback:
                data, rates = output.probe()
                if not valid_edid(data) or not rates:
                    raise ValueError("Kernel did not accept fallback EDID")
                result["rates"] = sorted(rates)
                LOG.warning("%s: fallback EDID applied (%s)", output.name, result["reason"])
        except BaseException:
            # charp sysfs parameters retain a newline as part of the value;
            # a NUL write actually resets the firmware string to empty.
            parameter.write_bytes(bytes([0]))
            for output, _ in fallback:
                output.probe()
            raise
    return {"policy": "conditional", "outputs": results}


def prepare():
    # VC4 is built in, but allow a bounded delay for its sysfs connectors.
    deadline = time.monotonic() + 2
    while True:
        outputs = [Output(path) for path in sorted(Path("/sys/class/drm").glob("card*-HDMI-A-*"))
                   if (path.parent / path.name.split("-", 1)[0] / "device/driver").resolve().name == "vc4-drm"]
        if outputs or time.monotonic() >= deadline:
            break
        time.sleep(.1)
    if not outputs:
        raise SystemExit("VC4 HDMI connectors unavailable")
    report = configure(outputs)
    atomic_json(Path("/run/satip-hdmi.json"), report, mode=0o644)


def before_splash(operation, directory=Path("/run/satip-splash")):
    # Share the renderer handoff lock: the supervisor cannot start mpv while
    # preparation is probing/changing EDID. Its finished marker forbids late work.
    directory.mkdir(mode=0o755, exist_ok=True)
    with (directory / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not (directory / "finished").exists():
            operation()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if os.geteuid() != 0:
        raise SystemExit("HDMI preparation requires root")
    # Default SIGALRM action terminates stalled DDC/libdrm work as well.
    signal.alarm(5)
    before_splash(prepare)
    signal.alarm(0)


if __name__ == "__main__":
    main()
