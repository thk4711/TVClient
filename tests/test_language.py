"""Language persistence, live switching and localized HDMI output."""
from datetime import datetime
from pathlib import Path
from string import Formatter
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from satip_appliance.splash import display_language
from satip_controller.controller import Controller
from satip_controller.epg import programme_banner, programme_parts, remaining
from satip_controller.i18n import GERMAN, tr
from satip_controller.osd import clock_label, details_panel, favourites_panel, volume_panel
from satip_controller.settings import load_settings, settings_path, validate_settings
from satip_controller.state import atomic_json
from satip_controller.web import create_app
from tests.helpers import Fixture


class LanguageTests(unittest.TestCase):
    def test_settings_defaults_legacy_and_invalid_languages(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_settings(directory)['osd_language'], 'en')
            legacy = {'version': 1, 'osd_timeout_ms': 8000}
            self.assertEqual(validate_settings(legacy), {**legacy, 'osd_language': 'en'})
            self.assertNotIn('osd_language', legacy)
            for language in (None, '', 'EN', 'fr', True, 1, [], {}, 'de\nSTOP'):
                with self.subTest(language=language), self.assertRaises(ValueError):
                    validate_settings({**legacy, 'osd_language': language})
            root = Path(directory)
            self.assertEqual(display_language(root), 'en')
            path = root / 'data/store/management/settings.json'
            path.parent.mkdir(parents=True)
            atomic_json(path, {**legacy, 'osd_language': 'de'})
            self.assertEqual(display_language(root), 'de')
            path.write_text('{broken')
            self.assertEqual(display_language(root), 'en')

    def test_translation_placeholders_and_literal_values(self):
        def fields(text):
            return {field for _, field, _, _ in Formatter().parse(text) if field is not None}
        for english, german in GERMAN.items():
            self.assertEqual(fields(english), fields(german), english)
        self.assertEqual(tr('Page {page} / {pages}', 'de', page=2, pages=3), 'Seite 2 / 3')
        self.assertEqual(tr('Unknown'), 'Unknown')

    def test_panels_dates_and_durations_in_both_languages(self):
        for language, heading, current, volume, muted, info, minutes in (
                ('en', 'Channels', 'Current programme', 'Volume', 'Muted', 'PROGRAMME INFORMATION', '2 min'),
                ('de', 'Sender', 'Aktuelle Sendung', 'Lautstärke', 'Stumm', 'PROGRAMMINFORMATIONEN', '2 Min.')):
            with self.subTest(language=language):
                panel = favourites_panel([(0, 'id', 'Das Erste', 'Tagesschau')], 0, 2, 3, language)
                for label in (heading, current, 'Das Erste', 'Tagesschau'):
                    self.assertIn(label, panel)
                self.assertIn(volume, volume_panel(35, False, language))
                self.assertIn(muted, volume_panel(35, True, language))
                self.assertIn(info, details_panel('Das Erste', ['Tagesschau', '20:00'], ['Text'], 0, language))
                self.assertEqual(remaining(61, 0, language), minutes)
        now = datetime(2026, 11, 13, 12, 0)
        self.assertEqual(clock_label(now), '2026-11-13  12:00')
        self.assertEqual(clock_label(now, 'de'), '13.11.2026  12:00')


class LanguageAPITests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup_files()
        self.client = TestClient(create_app(self.web_config, AsyncMock()))
        self.addCleanup(self.client.close)
        self.auth = ('web', 'private-web-password')

    def test_language_save_and_timeout_only_compatibility(self):
        path = settings_path(self.config.management_dir)
        atomic_json(path, {'version': 1, 'osd_timeout_ms': 8000})
        def put(data):
            return self.client.put('/api/settings', auth=self.auth, headers={'x-satip-action': '1'}, json=data)
        for language in ('de', 'en', 'de'):
            response = put({'osd_timeout_ms': 8000, 'osd_language': language})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['osd_language'], language)
            self.assertEqual(load_settings(self.config.management_dir), response.json())
        self.assertEqual(put({'osd_timeout_ms': 8500}).json()['osd_language'], 'de')
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        for language in (None, 'fr', True, 1, [], 'de\nSTOP'):
            self.assertEqual(put({'osd_timeout_ms': 8500, 'osd_language': language}).status_code, 422)
        self.assertEqual(load_settings(self.config.management_dir)['osd_language'], 'de')
        self.assertEqual(self.client.get('/api/settings', auth=self.auth).json()['osd_timeout_ms'], 8500)


class LiveLanguageTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_files()
        self.controller = Controller(self.config, self.player, self.state)
        self.channel = self.controller.channels[self.controller.requested]
        self.controller.playing = self.channel.uuid
        self.now = time.time()
        self.title = 'Tagesschau {title}'
        atomic_json(self.config.management_dir / 'epg.json', {'version': 1, 'channels': {
            self.channel.uuid: {'fetched': self.now, 'events': [{
                'title': self.title, 'start': self.now-60, 'stop': self.now+600,
                'description': 'Deutscher Beschreibungstext. ' * 300}]}}})

    async def asyncTearDown(self):
        await self.controller.osd.close()
        await self.state.close()

    async def language(self, value):
        atomic_json(settings_path(self.config.management_dir),
                    {'version': 1, 'osd_timeout_ms': 8000, 'osd_language': value})
        await self.controller.sync_management()

    async def test_broadcast_text_stays_literal_and_cache_is_language_neutral(self):
        for language, prefix in (('en', 'Now:'), ('de', 'Jetzt:')):
            text, _ = programme_banner(self.channel, self.config.management_dir, self.now, language)
            self.assertIn(prefix, text)
            self.assertIn(self.title, text)
            _, parts = programme_parts(self.channel, self.config.management_dir, self.now, language)
            self.assertEqual(parts[0], self.title)
            self.assertIn('Deutscher Beschreibungstext.', parts[2])

    async def test_open_panels_switch_without_retuning_or_resetting_scroll(self):
        c = self.controller
        await c.action('menu')
        c.menu_index = 1
        generation = c.generation
        await self.language('de')
        self.assertIn('Aktuelle Sendung', c.osd.overlay)
        self.assertEqual(c.menu_index, 1)
        await self.language('en')
        self.assertIn('Current programme', c.osd.overlay)
        await c.action('info')
        c.details['offset'] = 3
        await c.draw_details(force=True)
        await self.language('de')
        self.assertIn('PROGRAMMINFORMATIONEN', c.osd.overlay)
        self.assertEqual(c.details['offset'], 3)
        await self.language('en')
        self.assertIn('PROGRAMME INFORMATION', c.osd.overlay)
        self.assertEqual(c.details['offset'], 3)
        self.assertEqual(c.generation, generation)
        self.player.load.assert_not_awaited()
        self.player.stop_stream.assert_not_awaited()

    async def test_banner_switch_preserves_deadline_and_invalid_settings_keep_last_value(self):
        c = self.controller
        await c.show_programme(self.channel)
        end = c.epg_window[2]
        await self.language('de')
        self.assertEqual(c.epg_window[2], end)
        data = self.player.ipc.command.await_args.args[0]['data']
        self.assertIn('Jetzt:', data)
        await self.language('invalid')
        self.assertEqual(c.language, 'de')
        self.assertEqual(c.osd.duration, 8000)
        await self.language('en')
        self.assertEqual(c.epg_window[2], end)
        self.assertIn('Now:', self.player.ipc.command.await_args.args[0]['data'])
        self.player.load.assert_not_awaited()

    async def test_time_shift_and_youtube_messages_use_current_language(self):
        c = self.controller
        self.player.timeshift_action = AsyncMock(return_value='Time shift started – TV paused')
        for language, label in (('de', 'Zeitspeicher gestartet'), ('en', 'Time shift started')):
            await self.language(language)
            await c.action('pause')
            self.assertIn(label, self.player.ipc.command.await_args.args[0]['data'])
            self.assertIn('Video wird geladen' if language == 'de' else 'Loading video',
                          c.text('YouTube\nLoading video…'))
