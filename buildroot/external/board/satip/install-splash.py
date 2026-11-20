#!/usr/bin/env python3
"""Frühen Start reproduzierbar einfügen und die gebauten Voraussetzungen prüfen."""
from pathlib import Path
import sys


def install(target):
    inittab = target / "etc/inittab"
    text = inittab.read_text()
    entry = "::sysinit:/usr/sbin/satip-boot-splash\n"
    text = text.replace(entry, "")
    anchor = "::sysinit:/bin/mount -a\n"
    if text.count(anchor) != 1:
        raise ValueError("Früher Einfügepunkt in inittab fehlt oder ist mehrdeutig")
    text = text.replace(anchor, anchor + entry)
    inittab.write_text(text)
    for name in ("usr/sbin/satip-boot-splash", "usr/sbin/satip-hdmi-prepare", "etc/init.d/S01satip-splash"):
        (target / name).chmod(0o755)


def validate(target, output):
    binary = target / "usr/bin/satip-splash"
    header = binary.read_bytes()[:20]
    if header[:4] != b"\x7fELF" or header[18:20] != b"\xb7\x00":
        raise ValueError("AArch64-Startbildschirm fehlt")
    for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
        if not (target / "usr/share/fonts/dejavu" / name).is_file():
            raise ValueError("Schrift für den Startbildschirm fehlt")
    configs = list((output / "build").glob("linux-*/.config"))
    if len(configs) != 1:
        raise ValueError("Kernel-Konfiguration nicht eindeutig")
    selections = set(configs[0].read_text().splitlines())
    for key in ("DRM", "DRM_VC4", "SND", "SND_SOC", "RASPBERRYPI_FIRMWARE", "DEVTMPFS_MOUNT"):
        if f"CONFIG_{key}=y" not in selections:
            raise ValueError(f"Frühe Kernel-Unterstützung fehlt: {key}")


if __name__ == "__main__":
    target, output = map(Path, sys.argv[1:])
    validate(target, output)
    install(target)
