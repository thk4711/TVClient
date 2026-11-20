import struct
import unittest
from unittest.mock import patch

from satip_controller.font_metrics import glyph, text_width
from satip_controller.osd import font_advance, advance, fit


class FontMetricTests(unittest.TestCase):
    def test_unicode_cmap_and_horizontal_advances(self):
        cmap = struct.pack('>HHIII', 12, 0, 28, 0, 1) + struct.pack('>III', 65, 66, 1)
        self.assertEqual(glyph({12: cmap}, 65), 1)
        self.assertEqual(glyph({12: cmap}, 66), 2)
        self.assertEqual(glyph({12: cmap}, 67), 0)
        with patch('satip_controller.font_metrics.load_font', return_value=(1000, [0, 600, 700], {12: cmap})):
            self.assertAlmostEqual(text_width('AB', 30), 39)
            self.assertIsNone(text_width('C', 30))
            self.assertEqual(text_width('', 30), 0)

    def test_bmp_cmap_delta_and_glyph_array(self):
        header = struct.pack('>7H', 4, 32, 0, 2, 2, 0, 0)
        cmap = header + struct.pack('>HHHhH', 65, 0, 65, -64, 0)
        self.assertEqual(glyph({4: cmap}, 65), 1)
        self.assertEqual(glyph({4: cmap}, 66), 0)
        array_map = header + struct.pack('>HHHhHH', 65, 0, 65, 1, 2, 5)
        self.assertEqual(glyph({4: array_map}, 65), 6)

    def test_missing_font_and_glyph_use_conservative_fallback(self):
        with patch('satip_controller.font_metrics.load_font', return_value=None):
            self.assertIsNone(text_width('Sender', 34))
            self.assertEqual(font_advance('Sender', 34), advance('Sender', 34))
            self.assertEqual(font_advance('Sender', 34, True), advance('Sender', 34) * 1.08)

    def test_fit_uses_supplied_metrics(self):
        self.assertEqual(fit('ABCDE', 4, 34, lambda value, size: len(value)), 'ABC…')
        self.assertEqual(fit('ABC', 4, 34, lambda value, size: len(value)), 'ABC')
