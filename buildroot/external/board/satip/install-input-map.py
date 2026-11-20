#!/usr/bin/env python3
"""Install only the GPIO mapping; retain the pinned upstream udev autoload."""

from pathlib import Path
import sys
import tomllib


MAP = "/etc/rc_keymaps/apple-a1294-satip.toml"
RULE = ('ACTION=="add", SUBSYSTEM=="input", SUBSYSTEMS=="rc", KERNEL=="event*", '
        'ENV{.rc_sysdev}="$id", RUN+="/usr/bin/ir-keytable -a /etc/rc_maps.cfg -s $env{.rc_sysdev}"')


def install(target):
    target = Path(target)
    keymap = target / MAP.lstrip("/")
    protocols = tomllib.loads(keymap.read_text())["protocols"]
    expected = {}
    for code, key in ((0x02, "INFO"), (0x06, "VOLUMEUP"), (0x08, "VOLUMEDOWN"),
                      (0x0a, "CHANNELUP"), (0x0c, "CHANNELDOWN"),
                      (0x5c, "MUTE"), (0x5e, "PLAYPAUSE")):
        for suffix in (code, code + 1):
            expected[f"0x{0x87eeaa00 + suffix:08x}"] = "KEY_" + key
    if protocols != [{"name": "apple-a1294-satip", "protocol": "nec",
                      "variant": "nec32", "scancodes": expected}]:
        raise ValueError("Learned IR map differs from reviewed fourteen-entry map")
    rule = target / "usr/lib/udev/rules.d/70-infrared.rules"
    active = [line.strip() for line in rule.read_text().splitlines()
              if line.strip() and not line.lstrip().startswith("#")]
    if active != [RULE] or (target / "etc/udev/rules.d/70-infrared.rules").exists():
        raise ValueError("Unexpected or shadowed upstream IR autoload rule")
    if not (target / "usr/bin/ir-keytable").is_file():
        raise ValueError("ir-keytable is missing")
    config = target / "etc/rc_maps.cfg"
    if config.is_symlink():
        raise ValueError("Linked rc_maps.cfg refused")
    text = config.read_text()
    line = f"gpio_ir_recv * {MAP}"
    # Prepend our exact entry; preserve all other drivers' stock mappings.
    retained = []
    for existing in text.splitlines():
        fields = existing.split()
        if fields and fields[0] == "gpio_ir_recv":
            if existing.strip() != line:
                raise ValueError("Conflicting GPIO IR mapping refused")
            continue
        retained.append(existing)
    config.write_text(line + "\n" + "\n".join(retained) + "\n")
    config.chmod(0o644)
    keymap.chmod(0o644)


if __name__ == "__main__":
    install(sys.argv[1])
