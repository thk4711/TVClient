"""Read-only connector modes via libdrm; never acquires DRM master."""
import ctypes as C
import os
from pathlib import Path


class Mode(C.Structure):
    # libdrm drmModeModeInfo ABI.
    _fields_ = [("clock", C.c_uint32)] + [
        (name, C.c_uint16) for name in (
            "hdisplay", "hsync_start", "hsync_end", "htotal", "hskew",
            "vdisplay", "vsync_start", "vsync_end", "vtotal", "vscan")] + [
        ("vrefresh", C.c_uint32), ("flags", C.c_uint32),
        ("type", C.c_uint32), ("name", C.c_char * 32)]


class Connector(C.Structure):
    _fields_ = [
        ("connector_id", C.c_uint32), ("encoder_id", C.c_uint32),
        ("connector_type", C.c_uint32), ("connector_type_id", C.c_uint32),
        ("connection", C.c_int), ("mmWidth", C.c_uint32),
        ("mmHeight", C.c_uint32), ("subpixel", C.c_int),
        ("count_modes", C.c_int), ("modes", C.POINTER(Mode)),
        ("count_props", C.c_int), ("props", C.POINTER(C.c_uint32)),
        ("prop_values", C.POINTER(C.c_uint64)),
        ("count_encoders", C.c_int), ("encoders", C.POINTER(C.c_uint32))]


def refresh(mode):
    # mpv matches refresh rates to two decimals, not the rounded vrefresh field.
    if not mode.clock or not mode.htotal or not mode.vtotal:
        return float(mode.vrefresh)
    rate = mode.clock * 1000 / (mode.htotal * mode.vtotal)
    if mode.flags & (1 << 4):
        rate *= 2
    if mode.flags & (1 << 5):
        rate /= 2
    if mode.vscan > 1:
        rate /= mode.vscan
    return rate


def modes(device, connector, sysfs=Path("/sys/class/drm")):
    fd = None
    pointer = None
    try:
        card = Path(device).resolve(strict=True).name
        identity = int((sysfs / f"{card}-{connector}" / "connector_id").read_text())
        drm = C.CDLL("libdrm.so.2")
        drm.drmModeGetConnector.argtypes = [C.c_int, C.c_uint32]
        drm.drmModeGetConnector.restype = C.POINTER(Connector)
        drm.drmModeFreeConnector.argtypes = [C.POINTER(Connector)]
        drm.drmModeFreeConnector.restype = None
        fd = os.open(device, os.O_RDONLY | os.O_CLOEXEC)
        pointer = drm.drmModeGetConnector(fd, identity)
        if not pointer or pointer.contents.connection != 1:
            return []
        data = pointer.contents
        if not 0 < data.count_modes <= 4096 or not data.modes:
            return []
        return [(m.hdisplay, m.vdisplay, refresh(m), m.flags)
                for m in data.modes[:data.count_modes]]
    except (OSError, ValueError, AttributeError):
        return []
    finally:
        if pointer:
            drm.drmModeFreeConnector(pointer)
        if fd is not None:
            os.close(fd)


def hd_rates(available):
    return {m[2] for m in available if m[0:2] == (1920, 1080)
            and not m[3] & (1 << 4) and any(abs(m[2] - hz) < .5 for hz in (50, 60))}
