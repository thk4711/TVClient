#!/usr/bin/env python3
"""Check compiled features without pretending to execute Pi hardware paths."""

import hashlib
import json
from pathlib import Path
import re
import sys


FFMPEG_FEATURES = ("H264_DECODER", "H264_V4L2M2M_DECODER", "MPEG2VIDEO_DECODER",
                   "MP2_DECODER", "AAC_ENCODER", "LIBX264_ENCODER",
                   "MPEGTS_DEMUXER", "MPEGTS_MUXER", "HLS_MUXER", "SEGMENT_MUXER",
                   "HTTP_PROTOCOL", "PIPE_PROTOCOL", "FILE_PROTOCOL",
                   "H264_PARSER", "MPEGVIDEO_PARSER",
                   "BWDIF_FILTER", "SCALE_FILTER", "SETSAR_FILTER",
                   "ARESAMPLE_FILTER")
MPV_FEATURES = ("DRM", "EGL_DRM", "EGL", "GBM", "GL", "ALSA", "LIBASS", "POSIX")


def require_defines(text, prefix, names):
    for name in names:
        if not re.search(r"^#define " + prefix + name + r"\s+1\s*$", text, re.MULTILINE):
            raise ValueError(f"Missing compiled feature: {prefix}{name}")


def validate_backport(ffmpeg, manifest_path):
    """Reject upstream/stale M2M sources even when the decoder is enabled."""
    manifest = json.loads(manifest_path.read_text())
    patch = manifest_path.with_name("0001-rpi-v4l2-m2m.patch")
    if hashlib.sha256(patch.read_bytes()).hexdigest() != manifest["patch_sha256"]:
        raise ValueError("FFmpeg backport patch differs from verified manifest")
    if ffmpeg.name != "ffmpeg-" + manifest["ffmpeg_version"]:
        raise ValueError("FFmpeg backport version must be requalified")
    for name, expected in manifest["files"].items():
        source = ffmpeg / name
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise ValueError(f"FFmpeg Raspberry Pi backport source missing/stale: {name}; use a fresh output")
        if source.suffix == ".c":
            obj = source.with_suffix(".o")
            if not obj.is_file() or obj.read_bytes()[:4] != b"\x7fELF":
                raise ValueError(f"FFmpeg Raspberry Pi backport object missing: {name}")
    return {"rpi_ffmpeg_revision": manifest["rpi_ffmpeg_revision"],
            "patch_sha256": manifest["patch_sha256"],
            "source_sha256": manifest["files"]}


def validate(target, output, backport_manifest=None):
    target, output = Path(target), Path(output)
    build = output / "build"
    def package(name):
        matches = [path for path in build.glob(name + "-*") if (path / ".stamp_built").exists()]
        if len(matches) != 1:
            raise ValueError(f"Expected one built {name} package")
        return matches[0]
    ffmpeg, mpv, mesa, libv4l = map(package, ("ffmpeg", "mpv", "mesa3d", "libv4l"))
    ffconfig = ffmpeg / "config_components.h"
    require_defines(ffconfig.read_text(), "CONFIG_", FFMPEG_FEATURES)
    if backport_manifest is None:
        backport_manifest = Path(__file__).resolve().parents[2] / "patches/ffmpeg/rpi-v4l2-m2m.json"
    backport = validate_backport(ffmpeg, backport_manifest)
    mpconfig = mpv / "buildroot-build/config.h"
    require_defines(mpconfig.read_text(), "HAVE_", MPV_FEATURES)
    # Unix JSON IPC is unconditional in mpv's POSIX source selection.
    if not list((mpv / "buildroot-build").rglob("*ipc-unix*.o")):
        raise ValueError("mpv Unix IPC object missing")
    if not (libv4l / "buildroot-build/utils/keytable/ir-keytable.p/toml.c.o").is_file():
        raise ValueError("ir-keytable TOML parser object missing")
    options_path = mesa / "buildroot-build/meson-info/intro-buildoptions.json"
    options = {item["name"]: item["value"] for item in json.loads(options_path.read_text())}
    if not {"v3d", "vc4"}.issubset(options["gallium-drivers"]):
        raise ValueError("Mesa V3D/VC4 drivers missing")
    if options["egl"] != "enabled" or options["gles2"] != "enabled":
        raise ValueError("Mesa EGL/GLES support missing")
    files = ("usr/bin/mpv", "usr/bin/ffmpeg", "usr/bin/ffprobe", "usr/bin/ir-keytable",
             "usr/share/fonts/dejavu/DejaVuSans.ttf")
    installed = {}
    for name in files:
        path = target / name
        if not path.is_file():
            raise ValueError(f"Missing media file: {name}")
        installed[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    for library in ("libEGL", "libGLESv2", "libgbm", "libass", "libfontconfig",
                    "libfreetype", "libharfbuzz", "libfribidi", "libx264"):
        if not any(path.is_file() for path in (target / "usr/lib").glob(library + ".so*")):
            raise ValueError(f"Missing media library: {library}")
    codec = target / "usr/lib/libavcodec.so.60.31.102"
    if not codec.is_file() or codec.read_bytes()[:4] != b"\x7fELF":
        raise ValueError("Missing FFmpeg backport codec library")
    installed[str(codec.relative_to(target))] = hashlib.sha256(codec.read_bytes()).hexdigest()
    return {"schema": 1, "qualification": "compiled-features-only", "hardware_tested": False,
            "ffmpeg_rpi_backport": backport,
            "versions": {name: package(name).name[len(name) + 1:]
                         for name in ("mpv", "ffmpeg", "mesa3d", "libv4l", "libass", "x264")},
            "ffmpeg_features": list(FFMPEG_FEATURES), "mpv_features": list(MPV_FEATURES),
            "mesa_gallium_drivers": options["gallium-drivers"],
            "installed_files_sha256": installed,
            "configuration_sha256": {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
                                     for path in (ffconfig, mpconfig, options_path)}}


if __name__ == "__main__":
    target, output = map(Path, sys.argv[1:])
    evidence = validate(target, output)
    text = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    (output / "satip-media-build.json").write_text(text)
    (target / "etc/satip-media-build.json").write_text(text)
    print("SAT-IP compiled media features verified; hardware qualification deferred")
