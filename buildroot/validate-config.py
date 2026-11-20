#!/usr/bin/env python3
"""Reject ignored defconfig selections and unsafe foundation configurations."""

from pathlib import Path
import re
import sys


def settings(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        if line.startswith("BR2_") and "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
        else:
            match = re.fullmatch(r"# (BR2_\w+) is not set", line)
            if match:
                result[match[1]] = "n"
    return result


def validate(resolved, requested):
    errors = []
    for key, value in requested.items():
        if resolved.get(key, "n") != value:
            errors.append(f"Selection not retained: {key}")
    for key in ("BR2_INIT_SYSTEMD", "BR2_PACKAGE_SYSTEMD", "BR2_TARGET_UBOOT",
                "BR2_TARGET_ENABLE_ROOT_LOGIN", "BR2_PACKAGE_FFMPEG_NONFREE"):
        if resolved.get(key) == "y":
            errors.append(f"Not permitted in foundation: {key}")
    if resolved.get("BR2_TOOLCHAIN_USES_GLIBC") != "y":
        errors.append("Expected glibc toolchain")
    return errors


if __name__ == "__main__":
    errors = validate(settings(sys.argv[1]), settings(sys.argv[2]))
    if errors:
        print("\n".join(errors), file=sys.stderr)
        sys.exit(1)
    print("SAT-IP foundation Kconfig selections verified")
