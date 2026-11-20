import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "buildroot/external/board/satip"


def load(path):
    module = types.ModuleType(path.stem)
    exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
    return module


input_map = load(BOARD / "install-input-map.py")
media = load(BOARD / "validate-media-build.py")


class InputMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ("etc/rc_keymaps", "etc/udev/rules.d", "usr/lib/udev/rules.d", "usr/bin"):
            (self.root / directory).mkdir(parents=True)
        shutil.copy(BOARD / "rootfs-overlay/etc/rc_keymaps/apple-a1294-satip.toml",
                    self.root / input_map.MAP.lstrip("/"))
        (self.root / "usr/bin/ir-keytable").touch()
        (self.root / "usr/lib/udev/rules.d/70-infrared.rules").write_text(input_map.RULE + "\n")
        self.config = self.root / "etc/rc_maps.cfg"
        self.stock = "# stock mapping\n* rc-rc6-mce rc6_mce.toml\nother * other.toml\n"
        self.config.write_text(self.stock)

    def test_preserves_stock_maps_and_repeat_is_identical(self):
        input_map.install(self.root)
        first = self.config.read_bytes()
        self.assertTrue(first.decode().endswith(self.stock))
        self.assertEqual(first.decode().count("gpio_ir_recv"), 1)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o644)
        input_map.install(self.root)
        self.assertEqual(first, self.config.read_bytes())

    def test_conflicting_map_refused_without_rewrite(self):
        self.config.write_text("gpio_ir_recv * unrelated.toml\n" + self.stock)
        original = self.config.read_bytes()
        with self.assertRaises(ValueError):
            input_map.install(self.root)
        self.assertEqual(original, self.config.read_bytes())

    def test_unreviewed_remote_id_refused(self):
        path = self.root / input_map.MAP.lstrip("/")
        path.write_text(path.read_text().replace("87eeaa", "87eebb"))
        with self.assertRaises(ValueError):
            input_map.install(self.root)
        self.assertEqual(self.config.read_text(), self.stock)

    def test_shadowed_rule_and_linked_map_config_refused(self):
        shadow = self.root / "etc/udev/rules.d/70-infrared.rules"
        shadow.touch()
        with self.assertRaises(ValueError):
            input_map.install(self.root)
        shadow.unlink()
        self.config.unlink()
        outside = self.root / "outside"
        outside.write_text(self.stock)
        self.config.symlink_to(outside)
        with self.assertRaises(ValueError):
            input_map.install(self.root)
        self.assertEqual(outside.read_text(), self.stock)


class MediaBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "output"
        self.target = self.output / "target"
        self.build = self.output / "build"
        for name in ("ffmpeg", "mpv", "mesa3d", "libv4l", "libass", "x264"):
            path = self.build / (name + ("-6.1.5" if name == "ffmpeg" else "-test"))
            path.mkdir(parents=True)
            (path / ".stamp_built").touch()
        self.ffmpeg = self.build / "ffmpeg-6.1.5"
        self.ffconfig = self.ffmpeg / "config_components.h"
        self.ffconfig.write_text("".join(f"#define CONFIG_{name} 1\n" for name in media.FFMPEG_FEATURES))
        self.manifest = self.output / "rpi-v4l2-m2m.json"
        patch = self.manifest.with_name("0001-rpi-v4l2-m2m.patch")
        patch.write_text("fixture patch")
        sources = {}
        for name in ("v4l2_m2m_dec.c", "v4l2_req_dmabufs.c", "weak_link.c", "v4l2_req_dma_heap.h", "Makefile"):
            path = self.ffmpeg / "libavcodec" / name
            path.parent.mkdir(exist_ok=True)
            path.write_text("patched fixture " + name)
            sources[str(path.relative_to(self.ffmpeg))] = hashlib.sha256(path.read_bytes()).hexdigest()
            if path.suffix == ".c":
                path.with_suffix(".o").write_bytes(b"\x7fELFfixture")
        self.manifest.write_text(json.dumps({"ffmpeg_version": "6.1.5", "rpi_ffmpeg_revision": "fixture",
            "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(), "files": sources}))
        mpv = self.build / "mpv-test/buildroot-build"
        mpv.mkdir()
        (mpv / "config.h").write_text("".join(f"#define HAVE_{name} 1\n" for name in media.MPV_FEATURES))
        (mpv / "input_ipc-unix.c.o").touch()
        libv4l = self.build / "libv4l-test/buildroot-build/utils/keytable/ir-keytable.p"
        libv4l.mkdir(parents=True)
        (libv4l / "toml.c.o").touch()
        options = self.build / "mesa3d-test/buildroot-build/meson-info"
        options.mkdir(parents=True)
        self.options = options / "intro-buildoptions.json"
        self.options.write_text(json.dumps([{"name": name, "value": value} for name, value in
                              {"gallium-drivers": ["v3d", "vc4"], "egl": "enabled",
                               "gles2": "enabled"}.items()]))
        for name in ("usr/bin/mpv", "usr/bin/ffmpeg", "usr/bin/ffprobe", "usr/bin/ir-keytable",
                     "usr/share/fonts/dejavu/DejaVuSans.ttf"):
            path = self.target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fake")
        for library in ("libEGL", "libGLESv2", "libgbm", "libass", "libfontconfig", "libfreetype",
                        "libharfbuzz", "libfribidi", "libx264"):
            path = self.target / "usr/lib" / (library + ".so.1")
            path.parent.mkdir(exist_ok=True)
            path.touch()
        (self.target / "usr/lib/libavcodec.so.60.31.102").write_bytes(b"\x7fELFfixture")

    def validate(self):
        return media.validate(self.target, self.output, self.manifest)

    def test_records_compiled_not_hardware_qualification(self):
        evidence = self.validate()
        self.assertFalse(evidence["hardware_tested"])
        self.assertEqual(evidence["qualification"], "compiled-features-only")
        self.assertEqual(evidence["versions"]["mpv"], "test")
        self.assertIn("H264_V4L2M2M_DECODER", evidence["ffmpeg_features"])
        self.assertEqual(evidence["ffmpeg_rpi_backport"]["rpi_ffmpeg_revision"], "fixture")
        self.assertIn("usr/lib/libavcodec.so.60.31.102", evidence["installed_files_sha256"])

    def test_missing_or_stale_backport_sources_are_rejected(self):
        source = self.ffmpeg / "libavcodec/v4l2_m2m_dec.c"
        for content in (None, "upstream implementation"):
            if content is None:
                source.unlink()
            else:
                source.write_text(content)
            with self.assertRaisesRegex(ValueError, "source missing/stale"):
                self.validate()

    def test_backport_objects_patch_and_version_are_checked(self):
        obj = self.ffmpeg / "libavcodec/weak_link.o"
        obj.unlink()
        with self.assertRaisesRegex(ValueError, "object missing"):
            self.validate()
        obj.write_bytes(b"\x7fELFfixture")
        patch = self.manifest.with_name("0001-rpi-v4l2-m2m.patch")
        patch.write_text("unqualified patch")
        with self.assertRaisesRegex(ValueError, "patch differs"):
            self.validate()
        patch.write_text("fixture patch")
        self.ffmpeg.rename(self.build / "ffmpeg-other")
        with self.assertRaisesRegex(ValueError, "version must be requalified"):
            self.validate()

    def test_missing_installed_codec_is_rejected(self):
        (self.target / "usr/lib/libavcodec.so.60.31.102").unlink()
        with self.assertRaisesRegex(ValueError, "codec library"):
            self.validate()

    def test_disabled_decoder_refused(self):
        self.ffconfig.write_text(self.ffconfig.read_text().replace("CONFIG_H264_V4L2M2M_DECODER 1",
                                                                  "CONFIG_H264_V4L2M2M_DECODER 0"))
        with self.assertRaisesRegex(ValueError, "H264_V4L2M2M"):
            self.validate()

    def test_ipc_and_toml_objects_required(self):
        # Another tool's TOML parser must not qualify ir-keytable.
        unrelated = self.build / "libv4l-test/buildroot-build/utils/ir-ctl/ir-ctl.p/toml.c.o"
        unrelated.parent.mkdir(parents=True)
        unrelated.touch()
        for path in (self.build / "mpv-test/buildroot-build/input_ipc-unix.c.o",
                     self.build / "libv4l-test/buildroot-build/utils/keytable/ir-keytable.p/toml.c.o"):
            path.unlink()
            with self.assertRaises(ValueError):
                self.validate()
            path.touch()

    def test_disabled_gles_refused(self):
        self.options.write_text(self.options.read_text().replace('"gles2", "value": "enabled"',
                                                                '"gles2", "value": "disabled"'))
        with self.assertRaisesRegex(ValueError, "EGL/GLES"):
            self.validate()


if __name__ == "__main__":
    unittest.main()
