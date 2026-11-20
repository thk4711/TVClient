#!/usr/bin/env python3
"""Read-only application identity and reviewed firmware input inventory."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import runpy
import stat
import sys
import tomllib


SCHEMA = 3
STATIC_FILES = ("hls-LICENSE.txt", "hls.min.js", "index.html", "ui.js", "app.css", "app.js", "setup.html", "setup.js", "sources.js", "request.js", "youtube.js", "device.js", "qrcode.js", "qrcode-LICENSE.txt")

# Shared by the staging wrapper and image metadata, including Git-free builds.
# New firmware inputs require an explicit inventory change. Never derive this
# list from Git: ignored files and local device exports must not become inputs.
BUILD_EXTERNAL_FILES = """
Config.in
external.desc
external.mk
configs/satip_rpi4_64_defconfig
board/satip/busybox.fragment
board/satip/cmdline.txt
board/satip/config.txt
board/satip/genimage.cfg
board/satip/install-documentation.py
board/satip/install-hdmi-edid.py
board/satip/install-input-map.py
board/satip/install-splash.py
board/satip/linux-devices.fragment
board/satip/post-build.sh
board/satip/post-image.sh
board/satip/satip-config.txt
board/satip/users.txt
board/satip/validate-app-build.py
board/satip/validate-media-build.py
board/satip/rootfs-overlay/etc/fstab
board/satip/rootfs-overlay/etc/init.d/S01satip-splash
board/satip/rootfs-overlay/etc/init.d/S25satip-data
board/satip/rootfs-overlay/etc/init.d/S30satip-dev-provision
board/satip/rootfs-overlay/etc/init.d/S39satip-device
board/satip/rootfs-overlay/etc/init.d/S40network
board/satip/rootfs-overlay/etc/init.d/S50dropbear
board/satip/rootfs-overlay/etc/init.d/S89satip-watchdog
board/satip/rootfs-overlay/etc/init.d/S90satip-services
board/satip/rootfs-overlay/etc/rc_keymaps/apple-a1294-satip.toml
board/satip/rootfs-overlay/etc/udev/rules.d/60-satip-drm.rules
board/satip/rootfs-overlay/etc/udev/rules.d/99-satip-input.rules
board/satip/rootfs-overlay/usr/sbin/satip-boot-splash
board/satip/rootfs-overlay/usr/sbin/satip-dev-provision
board/satip/rootfs-overlay/usr/sbin/satip-device-helper
board/satip/rootfs-overlay/usr/sbin/satip-dhcp-hook
board/satip/rootfs-overlay/usr/sbin/satip-hdmi-prepare
board/satip/rootfs-overlay/usr/share/udhcpc/default.script.d/50-satip-cache
package/python-satip-yt-dlp/Config.in
package/python-satip-yt-dlp/python-satip-yt-dlp.hash
package/python-satip-yt-dlp/python-satip-yt-dlp.mk
package/satip-app/Config.in
package/satip-app/satip-app.mk
package/satip-splash/Config.in
package/satip-splash/satip-splash.mk
package/satip-splash/src/LICENSE
package/satip-splash/src/preview.c
package/satip-splash/src/render.c
package/satip-splash/src/render.h
package/satip-splash/src/qrcodegen.c
package/satip-splash/src/qrcodegen.h
package/satip-splash/src/splash.c
package/satip-splash/src/translations.h
patches/ffmpeg/0001-rpi-v4l2-m2m.patch
patches/ffmpeg/rpi-v4l2-m2m.json
patches/tvheadend/0001-idle-mux-grace.patch
patches/tvheadend/idle-mux-grace.json
""".split()
BUILD_MODULES = {
    "satip_controller": """
        __init__ __main__ astra audio backend channels config controller coordination
        diagnostics drm enigma2 epg font_metrics http i18n input lifecycle mpv_ipc osd
        performance player preview recovery relay_worker rolling scan settings splash state switching telemetry timeshift
        tuning web youtube manual
    """.split(),
    "satip_appliance": """
        __init__ backup channel_backup config_restore device_api device_client device_helper
        device_settings discovery epg_persistence guardian hdmi health mdns network onboarding operations
        persistent provision registration satip_tuner setup setup_device setup_rehearsal source_api source_browser splash starter startup supervisor
        timeshift_storage tuning tv_source watchdog web wifi_scan wifi_startup
    """.split(),
}
BUILD_GUIDES = """
    README api architecture backup-validation boot-process buildroot-first-boot-setup
    buildroot-services-persistence configuration development device-settings enigma2
    channel-switching enigma2-internals timeshift-internals youtube-internals
    hdmi-startup-screen installation operations provisioning recovery
    reference-setup releasing security timeshift troubleshooting tvheadend usage validation youtube
    first-boot-validation timeshift-validation user-manual infrared
""".split()
BUILD_FILES = frozenset({
    "README.md", "LICENSE", "pyproject.toml", "buildroot/README.md",
    "validation/README.md", "examples/README.md", "scripts/source_manifest.py",
    "scripts/render_manual.py", "scripts/draw_manual_diagrams.py",
    "doc/images/startbild.png", "doc/images/manual-icon.png",
    "doc/user-manual.html", "doc/user-manual.css",
    *("doc/images/manual-" + name + ".png" for name in (
        "welcome", "setup", "tuner", "selection", "order", "status", "live",
        "channels", "search", "settings-mobile", "sources", "flash", "connections",
        "wifi", "enigma", "bouquets", "ir-wiring")),
    *("buildroot/" + name for name in ("build.sh", "validate-checkout.sh",
      "validate-config.py", "build-metadata.py", "validate-artifacts.sh")),
    *("buildroot/external/" + name for name in BUILD_EXTERNAL_FILES),
    *(f"{package}/{name}.py" for package, names in BUILD_MODULES.items() for name in names),
    *("satip_controller/static/" + name for name in STATIC_FILES),
    *(f"doc/{name}.md" for name in BUILD_GUIDES),
})


def build_inputs(root):
    """Validate the complete source inventory before reading or staging data.

    Only reviewed files are required/hashed. Reject extra entries in source
    trees, including ignored files. Empty directories outside the overlay carry
    no staged payload and are left alone. Only regular CPython cache
    files are excluded; overlay caches must match reviewed sibling sources. Image
    metadata also rejects overlay caches copied into a local Buildroot target. Finder
    metadata outside the overlay is excluded.
    Error messages contain paths/categories only, never file contents.
    """
    root = Path(root).absolute()
    for parent in (root, *root.parents):
        if parent.is_symlink():
            raise ValueError("Linked build source root refused")
    directories = {parent.as_posix() for name in BUILD_FILES
                   for parent in Path(name).parents if parent != Path(".")}
    overlay = Path("buildroot/external/board/satip/rootfs-overlay")

    def kind(relative):
        mode = (root / relative).lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError(f"Linked build input refused: {relative.as_posix()!r}")
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError(f"Nonregular build input refused: {relative.as_posix()!r}")
        return mode

    def inspect(relative):
        mode = kind(relative)
        if relative.name == "__pycache__" and stat.S_ISDIR(mode):
            # SourceFileLoader uses a different cache stem for extensionless
            # scripts. Outside the overlay, also exclude stale module caches
            # left by renames; never follow links or hide other cache contents.
            stems = [path.stem + ("." if path.suffix == ".py" else "")
                     for name in BUILD_FILES if (path := Path(name)).parent == relative.parent
                     and path.suffix in ("", ".py")]
            for entry in sorted((root / relative).iterdir()):
                suffix = r"cpython-[0-9]{2,3}(?:\.opt-[0-9]+)?\.pyc"
                recognized = any(re.fullmatch(re.escape(stem) + suffix, entry.name) for stem in stems)
                if not relative.is_relative_to(overlay):
                    recognized = re.fullmatch(r"[A-Za-z0-9_.-]+\." + suffix, entry.name)
                if not stat.S_ISREG(kind(entry.relative_to(root))) or not recognized:
                    raise ValueError(f"Unexpected Python cache entry: {entry.relative_to(root).as_posix()!r}")
            return
        if not relative.is_relative_to(overlay):
            if relative.name == ".DS_Store" and stat.S_ISREG(mode):
                return
        if stat.S_ISDIR(mode):
            if relative.as_posix() not in directories:
                if not relative.is_relative_to(overlay) and not any((root / relative).iterdir()):
                    return
                raise ValueError(f"Unexpected build directory: {relative.as_posix()!r}")
            for child in sorted((root / relative).iterdir()):
                inspect(child.relative_to(root))
        elif relative.as_posix() not in BUILD_FILES:
            raise ValueError(f"Unexpected build input: {relative.as_posix()!r}")

    try:
        # Check ancestors first: never traverse linked source directories.
        for directory in sorted(directories, key=lambda name: (len(Path(name).parts), name)):
            if not stat.S_ISDIR(kind(Path(directory))):
                raise ValueError(f"Missing build directory: {directory!r}")
        for directory in ("buildroot/external", "satip_controller", "satip_appliance", "doc"):
            inspect(Path(directory))
        for name in sorted(BUILD_FILES):
            if not stat.S_ISREG(kind(Path(name))):
                raise ValueError(f"Missing build file: {name!r}")
        files = {}
        for name in sorted(BUILD_FILES):
            path = root / name
            before = path.lstat()
            data = path.read_bytes()
            after = path.lstat()
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode) != (
                    after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode):
                raise ValueError(f"Build source changed during collection: {name!r}")
            files[name] = hashlib.sha256(data).hexdigest()
        board = root / "buildroot/external/board/satip"
        provision = runpy.run_path(str(board / "rootfs-overlay/usr/sbin/satip-dev-provision"))
        try:
            settings = provision["parse_boot_config"]((board / "satip-config.txt").read_text())
        except (ValueError, UnicodeError):
            raise ValueError("Invalid generic provisioning template") from None
        if settings != {"hostname": "satip-dev"}:
            raise ValueError("Customized provisioning template must never be staged")
        return files
    except OSError as error:
        raise ValueError("Missing or unreadable build input") from error


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def source_manifest(root):
    """Hash flat Python modules, reviewed application assets and package metadata.

    This is an application identity, not a build-staging/privacy validator or a
    complete firmware identity. Expand the schema when new package inputs exist.
    Refuse links so this allowlist cannot traverse into private/external trees.
    """
    root = Path(root).resolve(strict=True)
    package = root / "satip_controller"
    for directory in (package, package / "static"):
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Missing or linked application directory")
    paths = [Path("pyproject.toml")]
    paths.extend(path.relative_to(root) for path in package.glob("*.py"))
    paths.extend(Path("satip_controller/static") / name for name in STATIC_FILES)
    if not (package / "__init__.py").is_file():
        raise ValueError("Missing application package")
    files = {}
    version = None
    for relative in sorted(paths):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing or linked source file: {relative.as_posix()}")
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError(f"Source changed during collection: {relative.as_posix()}")
        files[relative.as_posix()] = {
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        if relative == Path("pyproject.toml"):
            version = tomllib.loads(data.decode("utf-8"))["project"]["version"]
    identity = {"schema": SCHEMA, "scope": "satip-application",
                "version": version, "files": files}
    return {**identity, "source_sha256": hashlib.sha256(canonical_bytes(identity)).hexdigest()}


def compare_manifests(current, reference):
    left, right = current["files"], reference["files"]
    return {
        "only_current": sorted(left.keys() - right.keys()),
        "only_reference": sorted(right.keys() - left.keys()),
        "changed": sorted(key for key in left.keys() & right.keys()
                          if left[key] != right[key]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Application source root")
    parser.add_argument("--compare", type=Path, help="Compare against saved manifest (exit 1 on differences)")
    args = parser.parse_args()
    try:
        manifest = source_manifest(args.root)
        if args.compare:
            reference = json.loads(args.compare.read_text(encoding="utf-8"))
            identity = {key: reference[key] for key in ("schema", "scope", "version", "files")}
            if (identity["schema"] != SCHEMA or identity["scope"] != "satip-application"
                    or hashlib.sha256(canonical_bytes(identity)).hexdigest() != reference["source_sha256"]):
                raise ValueError("Unsupported or invalid reference manifest")
            differences = compare_manifests(manifest, reference)
            print(json.dumps({"source_sha256": manifest["source_sha256"],
                              "differences": differences}, indent=2, sort_keys=True))
            return int(manifest["source_sha256"] != reference["source_sha256"])
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Manifest failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
