"""Read horizontal DejaVu glyph advances without an extra target dependency."""
import struct
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=2)
def load_font(bold):
    filename = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    path = next((directory / filename for directory in (
        Path("/usr/share/fonts/dejavu"), Path("/usr/share/fonts/truetype/dejavu"))
        if (directory / filename).is_file()), None)
    if path is None:
        return None
    try:
        data = path.read_bytes()
        def u16(offset):
            return struct.unpack_from(">H", data, offset)[0]
        tables = {}
        for i in range(u16(4)):
            tag, _, offset, length = struct.unpack_from(">4sIII", data, 12 + i * 16)
            tables[tag] = data[offset:offset + length]
        units = struct.unpack_from(">H", tables[b"head"], 18)[0]
        count = struct.unpack_from(">H", tables[b"hhea"], 34)[0]
        widths = [struct.unpack_from(">H", tables[b"hmtx"], i * 4)[0] for i in range(count)]
        cmap = tables[b"cmap"]
        maps = {}
        for i in range(struct.unpack_from(">H", cmap, 2)[0]):
            platform, encoding, offset = struct.unpack_from(">HHI", cmap, 4 + i * 8)
            if platform == 0 or (platform == 3 and encoding in (1, 10)):
                format = struct.unpack_from(">H", cmap, offset)[0]
                if format in (4, 12):
                    maps[format] = cmap[offset:]
        if not units or not widths or not maps:
            return None
        return units, widths, maps
    except (OSError, KeyError, struct.error):
        return None


def glyph(cmaps, code):
    if 12 in cmaps:
        data = cmaps[12]
        count = struct.unpack_from(">I", data, 12)[0]
        low, high = 0, count
        while low < high:
            mid = (low + high) // 2
            start, end, first = struct.unpack_from(">III", data, 16 + mid * 12)
            if code < start:
                high = mid
            elif code > end:
                low = mid + 1
            else:
                return first + code - start
        return 0
    data = cmaps[4]
    count = struct.unpack_from(">H", data, 6)[0] // 2
    for i in range(count):
        end = struct.unpack_from(">H", data, 14 + i * 2)[0]
        start = struct.unpack_from(">H", data, 16 + count * 2 + i * 2)[0]
        if start <= code <= end:
            delta = struct.unpack_from(">h", data, 16 + count * 4 + i * 2)[0]
            address = 16 + count * 6 + i * 2
            offset = struct.unpack_from(">H", data, address)[0]
            if not offset:
                return (code + delta) & 65535
            index = struct.unpack_from(">H", data, address + offset + (code - start) * 2)[0]
            return (index + delta) & 65535 if index else 0
    return 0


def text_width(text, size, bold=False):
    font = load_font(bold)
    if font is None:
        return None
    units, widths, cmaps = font
    try:
        indices = [glyph(cmaps, ord(char)) for char in text]
        if any(index == 0 for index in indices):
            return None  # Unknown glyph: let the caller use a conservative estimate.
        return sum(widths[min(index, len(widths) - 1)] for index in indices) * size / units
    except struct.error:
        return None
