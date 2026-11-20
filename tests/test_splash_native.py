"""Exercise the compiled renderer: SATIP_SPLASH_RENDERER=/path/to/satip-splash.

Run on Linux with the target/native executable and installed DejaVu fonts.
No DRM device or receiver is opened by --frame/--bar.
"""
import os
from pathlib import Path
import struct
import subprocess
import unittest

from satip_controller.splash import PLAYER_PROGRESS, bar_bounds

RENDERER = os.environ.get("SATIP_SPLASH_RENDERER", "")


@unittest.skipUnless(RENDERER and Path(RENDERER).is_file(), "set SATIP_SPLASH_RENDERER to the compiled Linux renderer")
class NativeSplashTests(unittest.TestCase):
    def test_setup_qr_decodes_to_the_displayed_url(self):
        try:
            from PIL import Image
            import zxingcpp
        except ImportError:
            self.skipTest("install Pillow and zxing-cpp for independent QR decoding")
        url = "http://192.0.2.10:8080/"
        page = "Welcome\nOpen this address on your home network\n" + url + "\n" * 7 + url
        for width, height in ((1920, 1080), (1280, 720), (640, 360)):
            for state, detail in ((5, url), (38, page)):
                raw = subprocess.check_output([RENDERER, "--frame", str(width), str(height), str(state), detail])
                image = Image.frombytes("RGB", (width, height), raw, "raw", "BGRX")
                codes = zxingcpp.read_barcodes(image)
                self.assertEqual([code.text for code in codes], [url])

    def render(self, mode, width, height, state, *extra, language="en"):
        return subprocess.run([RENDERER, mode, str(width), str(height), str(state), "", language,
                               *map(str, extra)], capture_output=True, check=True).stdout

    def test_strip_matches_full_frame_at_all_supported_sizes(self):
        for width, height in ((1920, 1080), (1280, 720), (1024, 768)):
            with self.subTest(size=(width, height)):
                top, bottom = bar_bounds(width, height)
                full = self.render("--frame", width, height, 36, 91, 33)
                strip = self.render("--bar", width, height, 36, 91, 33)
                self.assertEqual(len(full), width * height * 4)
                self.assertEqual(len(strip), width * (bottom-top) * 4)
                reference = full[top*width*4:bottom*width*4]
                opaque = 0
                for offset in range(0, len(strip), 4):
                    if strip[offset+3]:
                        opaque += 1
                        self.assertEqual(strip[offset:offset+4], reference[offset:offset+4])
                    else:
                        self.assertEqual(strip[offset:offset+4], b"\0" * 4)
                self.assertGreater(opaque, 0)

    def test_player_milestones_match_native_defaults_and_completion_is_full_blue(self):
        for state, percent in PLAYER_PROGRESS.items():
            with self.subTest(state=state):
                default = self.render("--bar", 1920, 1080, state)
                # Early tuner connection is 64%; the player's later tuner-retry
                # artwork deliberately retains its already reached 86% milestone.
                explicit = self.render("--bar", 1920, 1080, state, 64 if state == 27 else percent, 24)
                self.assertEqual(default, explicit)
        completed = self.render("--bar", 1920, 1080, 37, 100, 24)
        row = struct.unpack("<1920I", completed[:1920*4])
        self.assertEqual(row[760:1160], (0xff67b6ff,) * 400)
        for language in ("en", "de"):
            frame = self.render("--frame", 640, 360, 37, language=language)
            self.assertEqual(len(frame), 640*360*4)

    def test_activity_moves_only_while_waiting_and_bad_progress_is_rejected(self):
        self.assertNotEqual(self.render("--bar", 1920, 1080, 3, 86, 24),
                            self.render("--bar", 1920, 1080, 3, 86, 40))
        for state in (11, 37):
            self.assertEqual(self.render("--bar", 1920, 1080, state, 94, 24),
                             self.render("--bar", 1920, 1080, state, 94, 40))
        for percent in ("101", "-1", "bad", ""):
            result = subprocess.run([RENDERER, "--bar", "1920", "1080", "3", "", "en", percent, "24"],
                                    capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")


if __name__ == "__main__":
    unittest.main()
