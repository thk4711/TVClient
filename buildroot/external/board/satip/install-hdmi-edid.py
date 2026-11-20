#!/usr/bin/env python3
"""Install a reproducible 1080p HDMI EDID for receivers that hide display EDID."""
from pathlib import Path
import sys


def timing(horizontal_blank, horizontal_front):
    # CTA VIC 31 (50 Hz) / VIC 16 (60 Hz): 148.5 MHz, positive sync, progressive.
    width, height, vertical_blank = 1920, 1080, 45
    return bytes([
        0x02, 0x3a, width & 255, horizontal_blank & 255,
        (width >> 8) << 4 | horizontal_blank >> 8,
        height & 255, vertical_blank, (height >> 8) << 4,
        horizontal_front & 255, 44, 0x45, (horizontal_front >> 8) << 6,
        520 & 255, 290 & 255, (520 >> 8) << 4 | (290 >> 8),
        0, 0, 0x1e])


def block(data):
    result = bytearray(data)
    result.extend(bytes(127 - len(result)))
    result.append((-sum(result)) & 255)
    return bytes(result)


def edid():
    base = bytearray(54)
    base[:8] = bytes.fromhex("00ffffffffffff00")
    base[8:10] = bytes.fromhex("4c34")  # SAT manufacturer identifier.
    base[10:12] = (1080).to_bytes(2, "little")
    base[16:25] = bytes([1, 36, 1, 3, 0x80, 52, 29, 120, 0x0a])
    # sRGB chromaticity (EDID fixed-point representation).
    base[25:35] = bytes.fromhex("ee91a3544c99260f5054")
    base[38:54] = bytes([1, 1]) * 8  # Unused standard timing slots.
    base.extend(timing(720, 528))  # Preferred 1080p50.
    base.extend(bytes.fromhex("000000fc00") + b"SAT-IP 1080p\n")
    base.extend(bytes.fromhex("000000fd00") + bytes([50, 60, 30, 85, 15, 0, 0x0a]) + b" " * 6)
    base.extend(timing(280, 88))  # Standard 1080p60.
    base.append(1)  # One CTA extension.
    # CTA video, 2-channel 48 kHz/16-bit LPCM, front L/R speakers, HDMI VSDB.
    data = bytes([0x42, 0x9f, 0x10, 0x23, 0x09, 0x04, 0x01,
                  0x83, 0x01, 0, 0, 0x65, 0x03, 0x0c, 0, 0x10, 0])
    extension = bytes([2, 3, 4 + len(data), 0x40]) + data
    return block(base) + block(extension)


def main():
    destination = Path(sys.argv[1]) / "lib/firmware/edid/satip-1080p.bin"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(edid())
    destination.chmod(0o644)


if __name__ == "__main__":
    main()
