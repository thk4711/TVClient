import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from satip_controller.osd import (OSD, BODY_ROWS, BODY_WIDTH, BODY_SIZE, advance,
                                  banner, details_panel, escape, fit, font_advance, wrap)


class LayoutTests(unittest.TestCase):
    def test_clock_has_own_space_and_localized_date_format(self):
        from datetime import datetime
        from satip_controller.osd import clock_label, volume_panel
        from unittest.mock import patch
        self.assertEqual(clock_label(datetime(2026, 11, 12, 18, 45)), '2026-11-12  18:45')
        label = clock_label(datetime(2026, 11, 12, 18, 45), language='de')
        self.assertEqual(label, '12.11.2026  18:45')
        with patch('satip_controller.osd.clock_label', return_value=label):
            self.assertIn(label, banner('Channel'))
            self.assertIn(label, volume_panel(40, False))
        self.assertIn('pos(1820,835)', banner('Channel'))
        self.assertIn('pos(1820,927)', volume_panel(40, False))
        for panel in (banner('Channel'), volume_panel(40, False)):
            clock_event = next(event for event in panel.splitlines() if 'pos(1820,' in event)
            self.assertIn('an9', clock_event)
        channel_event = next(event for event in banner('Channel').splitlines() if 'pos(100,830)' in event)
        self.assertIn('an7', channel_event)

    def test_sender_panel_equal_outer_margins(self):
        from satip_controller.osd import favourites_panel
        panel = favourites_panel([(0, 'a', 'Channel', 'Programme')], 0, 1, 1)
        self.assertIn('pos(72,72)', panel)
        self.assertIn('m 0 0 l 1776 0 1776 936 0 936', panel)
        self.assertIn('Channels', panel)
        self.assertNotIn('Favoriten', panel)
        self.assertIn('Channel', panel)
        self.assertIn('Programme', panel)

    def test_sender_full_page_uses_space_without_overlapping_help(self):
        from satip_controller.osd import (FAVOURITES_ROWS, FAVOURITES_TOP,
                                          FAVOURITES_ROW_HEIGHT, FAVOURITES_BOTTOM,
                                          favourites_panel)
        self.assertEqual(FAVOURITES_ROWS, 15)
        rows = [(i, str(i), f"Channel {i + 1}", f"Show {i + 1}") for i in range(FAVOURITES_ROWS)]
        panel = favourites_panel(rows, FAVOURITES_ROWS - 1, 1, 2)
        for i in range(FAVOURITES_ROWS):
            y = FAVOURITES_TOP + i * FAVOURITES_ROW_HEIGHT + 6
            self.assertIn(f"pos(236,{y})", panel)
        last_bottom = FAVOURITES_TOP + (FAVOURITES_ROWS - 1) * FAVOURITES_ROW_HEIGHT + 46
        self.assertLess(last_bottom, FAVOURITES_BOTTOM)
        self.assertLess(FAVOURITES_BOTTOM - last_bottom, FAVOURITES_ROW_HEIGHT)
        self.assertIn("pos(104,890)", panel)
        self.assertIn("m 0 0 l 1712 0 1712 46 0 46", panel)
        self.assertIn("pos(112,966)", panel)

    def test_sender_and_programme_use_fixed_independent_columns(self):
        from satip_controller.osd import favourites_panel, font_advance
        rows = [(0, 'a', 'W' * 200, 'Long programme ' * 100),
                (1, 'b', 'ZDF HD', 'Heute'), (2, 'c', 'VOX', '')]
        panel = favourites_panel(rows, 1, 1, 1)
        for y in (196, 246, 296):
            self.assertIn(f'pos(236,{y})', panel)
            self.assertIn(f'pos(748,{y})', panel)
        self.assertIn('Current programme', panel)
        self.assertIn(escape(fit(rows[0][2], 480, 34, font_advance)), panel)
        self.assertIn(escape(fit(rows[0][3], 1052, 34, font_advance)), panel)
        self.assertNotIn('LÄUFT', panel)
        self.assertNotIn(' — ', panel)

    def test_sender_column_fits_longest_name_and_gives_rest_to_show(self):
        import math
        from satip_controller.osd import favourites_columns, favourites_panel, font_advance
        rows = [(0, 'a', 'ZDF HD', 'Programme'), (1, 'b', 'MDR Sachsen HD', '')]
        name_width, x, width = favourites_columns(rows)
        self.assertEqual(name_width, math.ceil(font_advance('MDR Sachsen HD', 34, True)) + 4)
        self.assertEqual(x - (236 + name_width), 32)
        self.assertEqual(x + width, 1800)
        self.assertGreater(width, 1052)
        for selected in (0, 1):
            panel = favourites_panel(rows, selected, 1, 1)
            for y in (196, 246):
                self.assertIn(f'pos({x},{y})', panel)
        self.assertEqual(favourites_columns([(0, 'a', 'ZDF HD', 'changed')]),
                         favourites_columns([(0, 'a', 'ZDF HD', '')]))

    def test_untrusted_ass_and_properties_stay_literal(self):
        self.assertEqual(escape('Ö ${volume} {\\b1}\\N\nend\x00'),
                         'Ö $\\{volume} \\{\\\u2060b1}\\\u2060N\\Nend')

    def test_body_wrap_fits_and_preserves_paragraphs(self):
        text = 'Eine ausführliche Beschreibung. ' * 100 + '\n\nFINAL WORDS'
        lines = wrap(text)
        self.assertGreater(len(lines), BODY_ROWS)
        self.assertTrue(all(font_advance(line, BODY_SIZE) <= BODY_WIDTH for line in lines))
        self.assertEqual(' '.join(' '.join(lines).split()), ' '.join(text.split()))
        self.assertIn('', lines)

    def test_wide_and_unbroken_fields_fit(self):
        for text in ('W' * 1000, '界' * 1000):
            lines = wrap(text)
            self.assertTrue(all(font_advance(line, BODY_SIZE) <= BODY_WIDTH for line in lines))
            self.assertEqual(''.join(lines), text)
            self.assertLessEqual(advance(fit(text, 400, 42), 42), 400)
            self.assertTrue(fit(text, 400, 42).endswith('…'))

    def test_wrap_uses_measured_width_and_rendering_does_not_shorten_lines(self):
        # Deterministic proportional metrics even on hosts without DejaVu.
        def measured(text, size, bold=False):
            return sum(1 if char == 'W' else .25 for char in text) * size
        with patch('satip_controller.osd.text_width', side_effect=measured):
            self.assertEqual(wrap('i' * 100), ['i' * 100])
            words = ['iii', 'WW', 'iiiii', 'W'] * 100
            lines = wrap(' '.join(words))
            remaining = words[:]
            for line in lines:
                count = len(line.split())
                del remaining[:count]
                self.assertLessEqual(font_advance(line, BODY_SIZE), BODY_WIDTH)
                if remaining:
                    self.assertGreater(font_advance(line + ' ' + remaining[0], BODY_SIZE), BODY_WIDTH)
            panel = details_panel('Channel', ['Title', 'Time'], lines, 0)
            for line in lines[:BODY_ROWS]:
                self.assertIn(escape(line), panel)
            self.assertNotIn('…', panel)

    def test_wrap_without_font_preserves_text_and_fits(self):
        with patch('satip_controller.osd.text_width', return_value=None):
            text = 'Österreich Wörter WWW schmal iii ' * 100
            lines = wrap(text)
            self.assertEqual(' '.join(lines), text.strip())
            self.assertTrue(all(font_advance(line, BODY_SIZE) <= BODY_WIDTH for line in lines))

    def test_details_matches_sender_and_uses_height_without_footer_overlap(self):
        from satip_controller.osd import (BODY_TOP, BODY_LINE_HEIGHT, MENU_BOTTOM,
                                          SCROLLBAR_X, favourites_panel)
        lines = [f'Body line {i}' for i in range(BODY_ROWS * 3)]
        sender = favourites_panel([(0, 'a', 'Channel', 'Show')], 0, 1, 1)
        for offset in (0, len(lines) - BODY_ROWS):
            panel = details_panel('Channel', ['Title', 'Time'], lines, offset)
            self.assertEqual(panel.splitlines()[:2], sender.splitlines()[:2])
            for i, line in enumerate(lines[offset:offset + BODY_ROWS]):
                self.assertIn(f'pos(112,{BODY_TOP + i * BODY_LINE_HEIGHT})', panel)
                self.assertIn(escape(line), panel)
            self.assertLessEqual(BODY_TOP + BODY_ROWS * BODY_LINE_HEIGHT, MENU_BOTTOM)
            self.assertIn('pos(112,966)', panel)
            self.assertIn('pos(1808,966)', panel)
            self.assertIn(f'pos({SCROLLBAR_X},{BODY_TOP})', panel)
            self.assertIn('m 0 0 l 4 0 4 680 0 680', panel)
            self.assertIn(f'{offset + 1}–{offset + BODY_ROWS} / {len(lines)}', panel)

    def test_schedule_and_header_have_separate_positions(self):
        data = banner('Channel\nJetzt: ' + 'Very long title '*30 + ' — bis 20:15 · noch 10 Min.', .5, 1)
        self.assertIn('bis 20:15', data)
        self.assertIn('pos(1160,897)', data)

    def test_more_opaque_panels_and_larger_small_text(self):
        from satip_controller.osd import PANEL_ALPHA, volume_panel
        self.assertEqual(PANEL_ALPHA, '33')
        self.assertEqual(BODY_SIZE, 34)
        self.assertEqual(BODY_ROWS, 17)
        for data in (banner('Channel\nDetails'), volume_panel(40, False)):
            self.assertIn('1a&H33&', data)
        panel = details_panel('Channel', ['Title', '20:00–21:00'], wrap('Body'), 0)
        self.assertIn('fs30', panel)
        self.assertIn('fs34', panel)
        self.assertIn('fs42', panel)  # Large title is unchanged.

    def test_scrolled_details_header_has_fixed_positions(self):
        parts = ['Programme title', '20:00–21:00', 'Description']
        lines = wrap('Description. ' * 300)
        for offset in (0, 10):
            panel = details_panel('Channel', parts, lines, offset)
            self.assertIn('Programme title', panel)
            self.assertIn('20:00–21:00', panel)
            self.assertIn('pos(112,130)', panel)


class OverlayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.player = SimpleNamespace(ipc=SimpleNamespace(command=AsyncMock(), closed=False))
        self.osd = OSD(self.player, 20)

    async def asyncTearDown(self):
        await self.osd.close()

    async def test_transient_expires(self):
        await self.osd.show('Channel')
        await asyncio.sleep(.05)
        self.assertEqual(self.player.ipc.command.await_args.args[0]['format'], 'none')
        self.assertFalse(self.osd.timers)

    async def test_old_expiry_cannot_clear_new_banner(self):
        await self.osd.show('Old', 10)
        await self.osd.show('New', 1000)
        count = self.player.ipc.command.await_count
        await asyncio.sleep(.04)
        self.assertEqual(self.player.ipc.command.await_count, count)
        self.assertIn('New', self.player.ipc.command.await_args.args[0]['data'])

    async def test_menu_survives_banner_and_volume_expiry(self):
        await self.osd.show('Old', 10)
        await self.osd.menu('Persistent')
        panel = self.osd.overlay
        await self.osd.volume(40, False)
        await asyncio.sleep(.05)
        self.assertEqual(self.osd.overlay, panel)
        self.assertNotIn(1, self.osd.timers)
        self.assertEqual(self.player.ipc.command.await_args.args[0]['id'], 2)
        self.assertEqual(self.player.ipc.command.await_args.args[0]['format'], 'none')

    async def test_channel_replaces_volume(self):
        await self.osd.volume(40, False)
        await self.osd.show('New channel')
        self.assertNotIn(2, self.osd.timers)
        self.assertEqual(self.player.ipc.command.await_args_list[-2].args[0]['format'], 'none')

    async def test_disconnected_ipc_not_used_by_old_timer(self):
        old = self.player.ipc
        await self.osd.show('Old', 10)
        count = old.command.await_count
        self.player.ipc = SimpleNamespace(command=AsyncMock(), closed=False)
        await asyncio.sleep(.04)
        self.assertEqual(old.command.await_count, count)
        self.player.ipc.command.assert_not_awaited()

    async def test_reconnect_restores_persistent_panel(self):
        await self.osd.menu('Persistent')
        await self.osd.volume(40, False)
        self.player.ipc = SimpleNamespace(command=AsyncMock(), closed=False)
        await self.osd.reconnect()
        self.assertFalse(self.osd.timers)
        self.assertIn('Persistent', self.player.ipc.command.await_args.args[0]['data'])

    async def test_dismiss_clears_both_layers(self):
        await self.osd.menu('Persistent')
        await self.osd.volume(40, False)
        await self.osd.dismiss()
        self.assertIsNone(self.osd.overlay)
        self.assertFalse(self.osd.timers)
        self.assertEqual([c.args[0]['format'] for c in
                          self.player.ipc.command.await_args_list[-2:]], ['none', 'none'])
