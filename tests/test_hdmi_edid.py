import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BOARD = Path(__file__).resolve().parents[1] / "buildroot/external/board/satip"
spec = importlib.util.spec_from_file_location("hdmi_edid", BOARD / "install-hdmi-edid.py")
hdmi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hdmi)


class HDMIEdidTests(unittest.TestCase):
    def test_valid_reproducible_blocks(self):
        data = hdmi.edid()
        self.assertEqual(data, hdmi.edid())
        self.assertEqual(len(data), 256)
        self.assertEqual(data[:8], bytes.fromhex("00ffffffffffff00"))
        self.assertEqual(data[126], 1)
        for offset in (0, 128):
            self.assertEqual(sum(data[offset:offset + 128]) % 256, 0)

    def test_standard_1080p50_and_60_timings(self):
        data = hdmi.edid()
        for offset, horizontal_blank, front, hz in [(54, 720, 528, 50), (108, 280, 88, 60)]:
            d = data[offset:offset + 18]
            clock = int.from_bytes(d[:2], "little") * 10000
            width, blank = d[2] | (d[4] >> 4) << 8, d[3] | (d[4] & 15) << 8
            height, vblank = d[5] | (d[7] >> 4) << 8, d[6] | (d[7] & 15) << 8
            self.assertEqual((width, height, blank, vblank), (1920, 1080, horizontal_blank, 45))
            self.assertEqual(d[8] | (d[11] >> 6) << 8, front)
            self.assertEqual(d[9], 44)
            self.assertEqual(d[17], 0x1e)  # Positive H/V sync, not interlaced.
            self.assertEqual(clock / ((width + blank) * (height + vblank)), hz)

    def test_cta_stereo_pcm_and_hdmi_identification(self):
        data = hdmi.edid()[128:]
        self.assertEqual(data[:2], bytes([2, 3]))
        self.assertTrue(data[3] & 0x40)  # Basic audio supported.
        blocks = {}
        offset = 4
        while offset < data[2]:
            header = data[offset]
            size = header & 31
            blocks[header >> 5] = data[offset + 1:offset + 1 + size]
            offset += size + 1
        self.assertEqual(blocks[2], bytes([0x9f, 16]))  # VIC31 native, VIC16.
        self.assertEqual(blocks[1], bytes([9, 4, 1]))  # LPCM stereo, 48kHz, 16-bit.
        self.assertEqual(blocks[4], bytes([1, 0, 0]))  # Front L/R.
        self.assertEqual(blocks[3][:3], bytes.fromhex("030c00"))  # HDMI OUI.

    def test_install_and_build_integration_preserve_connector_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            subprocess.run([sys.executable, str(BOARD / "install-hdmi-edid.py"), temp], check=True)
            output = Path(temp) / "lib/firmware/edid/satip-1080p.bin"
            self.assertEqual(output.read_bytes(), hdmi.edid())
            self.assertEqual(output.stat().st_mode & 0o777, 0o644)
        command = (BOARD / "cmdline.txt").read_text()
        self.assertEqual(len(command.splitlines()), 1)
        self.assertFalse(any(token.startswith("drm.edid_firmware=") for token in command.split()))
        self.assertFalse(any(token.startswith("video=") for token in command.split()))
        self.assertIn('python3 "$board/install-hdmi-edid.py" "$target"', (BOARD / "post-build.sh").read_text())
        launch = (BOARD / "rootfs-overlay/usr/sbin/satip-boot-splash").read_text()
        self.assertLess(launch.index("/usr/sbin/satip-hdmi-prepare"), launch.index("exec /usr/bin/satip-splash"))
        self.assertIn(") </dev/null >/dev/null 2>/run/satip-splash.log &", launch)
