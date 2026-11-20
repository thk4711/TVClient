from satip_controller.state import lease_data, lease_active
import asyncio
import re
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from satip_controller.backend import BackendError
from satip_controller.config import Channel
from satip_controller.controller import Controller
from satip_controller.epg import EPGCache, channel_events, epg_text, events, programme_details, programme_text, remaining
from satip_controller.state import atomic_json, read_json
from tests.helpers import Fixture


class EPGTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_files()
        self.channel = Channel.parse(self.data['channels'][0])
        self.now = time.time()
        self.rows = [
            {'title': 'Aktuelle Sendung', 'start': self.now-600, 'stop': self.now+600},
            {'title': 'Nächste Sendung', 'start': self.now+600, 'stop': self.now+4200}]

    async def asyncTearDown(self):
        await self.state.close()

    def cache(self, rows=None, fetched=None):
        atomic_json(self.config.management_dir/'epg.json', {'version': 1, 'channels': {
            self.channel.uuid: {'fetched': self.now if fetched is None else fetched,
                                'events': self.rows if rows is None else rows}}})

    async def test_current_next_and_remaining_until_next_finishes(self):
        self.cache()
        text = programme_text(self.channel, self.config.management_dir, self.now)
        self.assertIn('Now: Aktuelle Sendung', text)
        self.assertIn('10 min left', text)
        self.assertIn('Next: Nächste Sendung', text)
        self.assertIn('ends in 1 hr 10 min', text)

    async def test_boundary_uses_new_current_and_excludes_ended_event(self):
        self.cache(fetched=self.now+600)
        text = programme_text(self.channel, self.config.management_dir, self.now+600)
        self.assertIn('Now: Nächste Sendung', text)
        self.assertNotIn('Aktuelle Sendung', text)

    async def test_missing_stale_and_corrupt_cache(self):
        self.assertIn('not yet available', programme_text(self.channel, self.config.management_dir))
        self.cache(fetched=self.now-301)
        self.assertNotIn('Aktuelle Sendung', programme_text(self.channel, self.config.management_dir, self.now))
        (self.config.management_dir/'epg.json').write_text('{bad')
        self.assertIn('not yet available', programme_text(self.channel, self.config.management_dir))

    async def test_next_without_current_and_schedule_gap(self):
        self.cache(rows=self.rows[1:])
        text = programme_text(self.channel, self.config.management_dir, self.now)
        self.assertIn('Now: Programme information', text)
        self.assertIn('Next: Nächste Sendung', text)

    async def test_invalid_events_and_title_normalization(self):
        bad = [{'title':'bad', 'start':0, 'stop':1},
               {'title':'bad', 'start':float('nan'), 'stop':42},
               {'title':'bad', 'start':1, 'stop':float('inf')},
               {'title':'bad', 'start':True, 'stop':42},
               {'title':'bad', 'start':1e14, 'stop':1e14+60}, None]
        self.assertEqual(events(bad), [])
        self.assertEqual(events([{**self.rows[0], 'title':'Österreich\n  live'}])[0]['title'],
                         'Österreich live')

    async def test_long_titles_wrap_and_minutes_round_up(self):
        self.cache(rows=[{**self.rows[0], 'title':'Lange Sendung '*30}])
        text = programme_text(self.channel, self.config.management_dir, self.now)
        self.assertTrue(all(len(line) <= 90 for line in text.splitlines()))
        self.assertEqual(remaining(self.now+61, self.now), '2 min')

    async def test_enigma2_fallback_is_throttled_and_never_opens_a_stream(self):
        from pathlib import Path
        backend = AsyncMock()
        backend.request.return_value = {'entries': []}
        atomic_json(Path(self.web_config['private_dir']) / 'tv-source-enigma2.json', {
            'network_uuid': self.web_config['network_uuid'],
            'enigma2': {'address': '192.168.1.2', 'web_port': 80, 'stream_port': 8001,
                       'services': {self.channel.mux_uuid: '1:0:19:283D:3FB:1:C00000:0:0:0:'}}})
        cache = EPGCache(backend, self.web_config)
        with patch('satip_controller.enigma2.Enigma2.events', return_value=self.rows) as source:
            await cache.refresh()
            await cache.refresh()
        source.assert_called_once()
        self.assertEqual(len(read_json(cache.path)['channels'][self.channel.uuid]['events']), 2)
        self.assertTrue(all(call.args == ('epg/events/grid',) for call in backend.request.await_args_list))
        backend.grid.assert_not_awaited()

    async def test_publisher_is_read_only_and_cache_is_secret_free(self):
        backend = AsyncMock()
        backend.request.return_value = {'entries': [{**self.rows[0], 'description':'Full synopsis',
                                                     'unrelated':'not published'}]}
        cache = EPGCache(backend, self.web_config)
        await cache.refresh()
        call = backend.request.await_args_list[0]
        self.assertEqual(call.args, ('epg/events/grid',))
        self.assertNotIn('write', call.kwargs)
        self.assertEqual(call.kwargs['channel'], self.channel.uuid)
        data = read_json(cache.path)
        self.assertIn('Full synopsis', str(data))
        self.assertNotIn('unrelated', str(data))
        self.assertEqual(cache.path.stat().st_mode & 0o777, 0o640)

    async def test_publisher_prioritizes_requested_channel(self):
        requested = self.data['favourites'][2]
        atomic_json(self.config.state_dir/'status.json', {'requested': requested})
        backend = AsyncMock()
        backend.request.return_value = {'entries': []}
        cache = EPGCache(backend, self.web_config)
        await cache.refresh()
        self.assertIn(requested, read_json(cache.path)['channels'])
        backend.save.assert_not_awaited()

    def many_favourites(self, count=23):
        self.data["channels"] = [{**self.data["channels"][0], "uuid": f"{i:032x}"}
                                 for i in range(1, count+1)]
        self.data["favourites"] = [channel["uuid"] for channel in self.data["channels"]]
        atomic_json(self.config.library, self.data)

    async def test_startup_warms_all_favourites_in_bounded_batches(self):
        self.many_favourites()
        backend = AsyncMock()
        backend.request.return_value = {"entries": self.rows}
        cache = EPGCache(backend, self.web_config)
        for batch in range(6):
            before = backend.request.await_count
            await cache.refresh()
            self.assertLessEqual(backend.request.await_count-before, 4)
            self.assertEqual(len(read_json(cache.path)["channels"]), min(4*(batch+1), 23))
        self.assertFalse(cache.warming)
        before = backend.request.await_count
        await cache.refresh()
        self.assertEqual(backend.request.await_count-before, 1)
        backend.save.assert_not_awaited()

    async def test_warmup_prioritizes_requested_without_duplicate_queries(self):
        self.many_favourites()
        requested = self.data["favourites"][-1]
        atomic_json(self.config.state_dir/"status.json", {"requested": requested})
        backend = AsyncMock()
        backend.request.return_value = {"entries": self.rows}
        cache = EPGCache(backend, self.web_config)
        await cache.refresh()
        calls = [call.kwargs["channel"] for call in backend.request.await_args_list]
        self.assertEqual(calls[0], requested)
        self.assertEqual(len(calls), 5)
        self.assertEqual(len(set(calls)), 5)
        backend.request.reset_mock()
        cache.cursor = 20
        await cache.refresh()
        calls = [call.kwargs["channel"] for call in backend.request.await_args_list]
        self.assertEqual(calls.count(requested), 1)

    async def test_500_favourites_remain_fresh_in_published_cache_over_multiple_sweeps(self):
        self.many_favourites(500)
        requested = self.data['favourites'][0]
        atomic_json(self.config.state_dir/'status.json', {'requested': requested})
        now = self.now
        rows = [{'title': 'Long programme', 'start': now-60, 'stop': now+3600}]
        backend = AsyncMock()

        async def reply(endpoint, **values):
            nonlocal now
            # Requests in a batch overlap. Charge one second of backend latency
            # per batch, independently of the run loop's polling delay.
            if values['channel'] == requested:
                now += 1
            return {'entries': rows}

        backend.request.side_effect = reply
        clock = SimpleNamespace(time=lambda: now, monotonic=lambda: now)
        checked = 0

        async def sleep(delay):
            nonlocal now, checked
            now += delay
            calls = [call.kwargs['channel'] for call in backend.request.await_args_list]
            self.assertEqual(calls[0], requested)
            self.assertEqual(len(calls), len(set(calls)))
            self.assertLessEqual(len(calls), 11)
            backend.request.reset_mock()
            if not cache.warming:
                self.assertEqual(delay, 2)
                # Check the published snapshot, including time spent between
                # polls and timestamp-only refreshes of unchanged programmes.
                published = read_json(cache.path)['channels']
                self.assertEqual(set(published), set(self.data['favourites']))
                self.assertTrue(all(0 <= now-item['fetched'] <= 300 for item in published.values()))
                checked += 1
            if now-self.now >= 1200:
                raise asyncio.CancelledError

        with (patch('satip_controller.epg.time', clock),
              patch('satip_controller.epg.asyncio.sleep', side_effect=sleep)):
            cache = EPGCache(backend, self.web_config)
            with self.assertRaises(asyncio.CancelledError):
                await cache.run()
        self.assertGreater(checked, 300)
        backend.save.assert_not_awaited()

    async def test_steady_batches_preserve_failures_without_starving_other_channels(self):
        self.many_favourites(500)
        requested = self.data['favourites'][0]
        atomic_json(self.config.state_dir/'status.json', {'requested': requested})
        now = self.now
        rows = [{'title': 'Long programme', 'start': now-60, 'stop': now+3600}]
        failures = {}
        backend = AsyncMock()

        async def reply(endpoint, **values):
            failure = failures.get(values['channel'])
            if isinstance(failure, Exception):
                raise failure
            return failure if failure is not None else {'entries': rows}

        backend.request.side_effect = reply
        clock = SimpleNamespace(time=lambda: now, monotonic=lambda: now)
        with patch('satip_controller.epg.time', clock):
            cache = EPGCache(backend, self.web_config)
            while cache.warming:
                await cache.refresh()
            before = read_json(cache.path)['channels']
            failed_ids = self.data['favourites'][:3]
            failures.update(zip(failed_ids, (BackendError('unavailable'), TimeoutError(), {'entries': None})))
            # Simulate an outage longer than the TTL, then let healthy channels
            # recover while these three continue to fail on every sweep.
            now += 301
            cache.clock = (now, now)
            for _ in range(70):
                now += 3
                backend.request.reset_mock()
                self.assertTrue(await cache.refresh())
                calls = [call.kwargs['channel'] for call in backend.request.await_args_list]
                self.assertEqual(calls[0], requested)
                self.assertEqual(len(calls), len(set(calls)))
                self.assertLessEqual(len(calls), 11)
            published = read_json(cache.path)['channels']
            for identifier, item in published.items():
                if identifier in failures:
                    self.assertEqual(item, before[identifier])
                    channel = Channel.parse(next(c for c in self.data['channels'] if c['uuid'] == identifier))
                    self.assertEqual(channel_events(channel, self.config.management_dir, now), [])
                else:
                    self.assertLessEqual(now-item['fetched'], 300)
                    self.assertEqual(item['events'], events(rows))
            failures.clear()
            for _ in range(70):
                now += 3
                await cache.refresh()
            published = read_json(cache.path)['channels']
            self.assertTrue(all(0 <= now-item['fetched'] <= 300 for item in published.values()))
            for identifier in failed_ids:
                channel = Channel.parse(next(c for c in self.data['channels'] if c['uuid'] == identifier))
                self.assertEqual(channel_events(channel, self.config.management_dir, now), events(rows))
        backend.save.assert_not_awaited()

    async def test_clock_correction_restarts_warmup_but_elapsed_time_does_not(self):
        self.many_favourites()
        backend = AsyncMock()
        backend.request.return_value = {"entries": self.rows}
        for correction in (self.now, -3600):
            cache = EPGCache(backend, self.web_config)
            cache.cursor = 23
            cache.warming = False
            cache.clock = (100, 10)
            with patch("satip_controller.epg.time", SimpleNamespace(time=lambda: 160, monotonic=lambda: 70)):
                await cache.refresh()
            self.assertFalse(cache.warming)
            self.assertEqual(cache.cursor, 24)
            backend.request.reset_mock()
            with patch("satip_controller.epg.time",
                       SimpleNamespace(time=lambda: 162+correction, monotonic=lambda: 72)):
                await cache.refresh()
            self.assertTrue(cache.warming)
            self.assertEqual(cache.cursor, 4)
            self.assertEqual(backend.request.await_count, 4)
            self.assertEqual(backend.request.await_args_list[0].kwargs["filter"][0]["value"],
                             int(162+correction))
            with patch("satip_controller.epg.time",
                       SimpleNamespace(time=lambda: 162+correction, monotonic=lambda: 72)):
                while cache.warming:
                    await cache.refresh()
            self.assertEqual({item["fetched"] for item in read_json(cache.path)["channels"].values()},
                             {162+correction})

    async def test_failed_channel_does_not_block_successful_cache_publication(self):
        backend = AsyncMock()
        backend.request.side_effect = [BackendError("private"), {"entries": self.rows}, {"entries": []}]
        cache = EPGCache(backend, self.web_config)
        await cache.refresh()
        data = read_json(cache.path)["channels"]
        self.assertNotIn(self.data["favourites"][0], data)
        self.assertEqual(data[self.data["favourites"][1]]["events"][0]["title"], self.rows[0]["title"])
        self.assertIn(self.data["favourites"][2], data)
        backend.request.side_effect = None
        backend.request.return_value = {"entries": self.rows}
        await cache.refresh()
        self.assertIn(self.data["favourites"][0], read_json(cache.path)["channels"])

    async def test_malformed_channel_reply_preserves_previous_events(self):
        backend = AsyncMock()
        backend.request.return_value = {"entries": self.rows}
        cache = EPGCache(backend, self.web_config)
        await cache.refresh()
        previous = read_json(cache.path)
        backend.request.return_value = {"entries": None}
        self.assertFalse(await cache.refresh())
        self.assertEqual(read_json(cache.path), previous)

    async def test_startup_loop_yields_without_poll_delay_and_outages_back_off(self):
        self.many_favourites()
        backend = AsyncMock()
        backend.request.return_value = {"entries": self.rows}
        cache = EPGCache(backend, self.web_config)
        with patch("satip_controller.epg.asyncio.sleep", new_callable=AsyncMock) as sleep:
            sleep.side_effect = [None]*5 + [asyncio.CancelledError()]
            with self.assertRaises(asyncio.CancelledError):
                await cache.run()
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [0]*5+[2])
        cache = EPGCache(backend, self.web_config)
        backend.request.side_effect = BackendError("private")
        with patch("satip_controller.epg.asyncio.sleep", new_callable=AsyncMock) as sleep:
            sleep.side_effect = asyncio.CancelledError()
            with self.assertRaises(asyncio.CancelledError):
                await cache.run()
        sleep.assert_awaited_once_with(2)

    async def test_publisher_outage_does_not_stop_loop(self):
        cache = EPGCache(AsyncMock(), self.web_config)
        cache.refresh = AsyncMock(side_effect=BackendError('private detail'))
        task = asyncio.create_task(cache.run())
        await asyncio.sleep(.02)
        self.assertFalse(task.done())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_osd_five_seconds_and_late_data_keeps_original_deadline(self):
        c = Controller(self.config, self.player, self.state)
        c.playing = self.channel.uuid
        with patch('satip_controller.controller.time.monotonic', return_value=100):
            await c.show_programme(self.channel)
        self.assertEqual(c.osd.durations[1], 5000)
        self.cache()
        with patch('satip_controller.controller.time.monotonic', return_value=102):
            await c.refresh_programme()
        self.assertEqual(c.osd.durations[1], 3000)
        self.assertEqual(c.epg_window[2], 105)
        count = self.player.ipc.command.await_count
        with patch('satip_controller.controller.time.monotonic', return_value=105):
            await c.refresh_programme()
        self.assertIsNone(c.epg_window)
        self.assertEqual(self.player.ipc.command.await_count, count)

    async def test_settings_reload_without_retune_and_invalid_retains_previous(self):
        from satip_controller.settings import settings_path
        c = Controller(self.config, self.player, self.state)
        c.playing = self.channel.uuid
        atomic_json(settings_path(self.config.management_dir),
                    {'version': 1, 'osd_timeout_ms': 8500})
        generation = c.generation
        await c.sync_management()
        await c.show_programme(self.channel)
        self.assertEqual(c.osd.durations[1], 8500)
        self.assertEqual(c.osd.duration, 8500)
        self.assertEqual(c.generation, generation)
        self.player.load.assert_not_awaited()
        atomic_json(settings_path(self.config.management_dir), {'version': 1, 'osd_timeout_ms': True})
        await c.sync_management()
        self.assertEqual(c.osd.duration, 8500)
        await c.osd.close()

    async def test_sender_menu_cached_current_only_and_live_refresh(self):
        c = Controller(self.config, self.player, self.state)
        c.menu_index = 0
        from satip_controller.osd import favourites_columns, FAVOURITES_ROWS, FAVOURITES_TOP
        rows = [(i, uuid, c.channels[uuid].name, '') for i, uuid in enumerate(c.favourites[:FAVOURITES_ROWS])]
        programme_pos = f'pos({favourites_columns(rows)[1]},{FAVOURITES_TOP + 6})'
        await c.draw_menu()
        self.assertNotIn('Aktuelle Sendung', c.osd.overlay.split(programme_pos)[1].splitlines()[0])
        self.cache()
        await c.sync_management()
        self.assertIn(self.channel.name, c.osd.overlay)
        self.assertIn(programme_pos, c.osd.overlay)
        self.assertIn('Aktuelle Sendung', c.osd.overlay.split(programme_pos)[1].splitlines()[0])
        self.assertNotIn('Nächste Sendung', c.osd.overlay)
        self.cache(fetched=self.now-301)
        await c.draw_menu()
        self.assertNotIn('Aktuelle Sendung', c.osd.overlay.split(programme_pos)[1].splitlines()[0])
        self.assertIn(self.channel.name, c.osd.overlay)
        await c.osd.close()

    async def test_new_osd_channel_menu_and_pause_suppress_late_epg(self):
        for action in ('volume', 'switch', 'menu', 'pause'):
            c = Controller(self.config, self.player, self.state)
            c.playing = self.channel.uuid
            await c.show_programme(self.channel)
            if action == 'volume': await c.osd.show('Volume')
            if action == 'switch': c.request(self.data['favourites'][1])
            if action == 'menu': await c.osd.menu('Menu')
            if action == 'pause': c.paused = True
            self.cache()
            count = self.player.ipc.command.await_count
            await c.refresh_programme()
            self.assertEqual(self.player.ipc.command.await_count, count)

    async def test_osd_sequence_checked_inside_lock(self):
        c = Controller(self.config, self.player, self.state)
        ticket = await c.osd.show('Channel')
        await c.osd.show('Volume')
        count = self.player.ipc.command.await_count
        self.assertIsNone(await c.osd.show('Stale EPG', expected=ticket))
        self.assertEqual(self.player.ipc.command.await_count, count)

    async def test_info_toggles_persistent_programme_details(self):
        c = Controller(self.config, self.player, self.state)
        c.playing = self.channel.uuid
        self.cache()
        await c.action('info')
        self.assertIn('Aktuelle Sendung', self.player.ipc.command.await_args.args[0]['data'])
        self.assertNotIn(1, c.osd.timers)
        await c.action('info')
        self.assertIsNone(c.details)
        self.assertIsNone(c.osd.overlay)

    async def test_details_retain_full_description_and_paragraphs(self):
        description = 'First paragraph.\n\n' + 'A detailed sentence. '*100 + 'LAST SENTENCE.'
        self.cache(rows=[{**self.rows[0], 'subtitle':'Episode subtitle', 'description':description}])
        identity, lines = programme_details(self.channel, self.config.management_dir, self.now)
        text = '\n'.join(lines)
        self.assertEqual(identity[0], self.rows[0]['start'])
        self.assertIn('Episode subtitle', text)
        self.assertIn('LAST SENTENCE.', text)
        self.assertTrue(all(len(line) <= 76 for line in lines))
        self.assertIn('', lines)
        self.assertGreater(len(lines), 10)

    async def test_details_deduplicate_synopsis_and_missing_fields(self):
        self.cache(rows=[{**self.rows[0], 'subtitle':'Same text', 'summary':'Same text',
                         'description':'Same text with further details.'}])
        _, lines = programme_details(self.channel, self.config.management_dir, self.now)
        self.assertEqual('\n'.join(lines).count('Same text'), 1)
        self.cache()
        self.assertIn('No detailed description', '\n'.join(
            programme_details(self.channel, self.config.management_dir, self.now)[1]))

    async def test_details_sanitize_and_bound_untrusted_text(self):
        self.assertEqual(epg_text('Österreich\r\n\r\nLive\x00\x1b ${volume} {\\b1}'),
                         'Österreich\n\nLive ${volume} {\\b1}')
        self.assertEqual(epg_text(None), '')
        self.assertIn('…', epg_text('x'*70000))

    async def test_scroll_boundaries_and_restore_channel_keys(self):
        self.cache(rows=[{**self.rows[0], 'description':'Long description. '*200 + 'FINAL WORDS'}])
        c = Controller(self.config, self.player, self.state)
        c.playing = self.channel.uuid
        await c.action('info')
        await c.action('next')
        self.assertEqual(c.details['offset'], 0)
        for _ in range(100): await c.action('previous')
        bottom = c.details['offset']
        self.assertGreater(bottom, 0)
        visible = re.sub(r'\{[^}]*\}', '', c.details['text'])
        self.assertIn('FINAL WORDS', ' '.join(visible.split()))
        await c.action('previous')
        self.assertEqual(c.details['offset'], bottom)
        self.assertEqual(c.requested, self.channel.uuid)
        self.assertEqual(c.generation, 0)
        await c.action('next')
        self.assertEqual(c.details['offset'], bottom-1)
        await c.action('info')
        await c.action('next')
        self.assertEqual(c.requested, self.data['favourites'][1])
        await c.action('previous')
        self.assertEqual(c.requested, self.channel.uuid)
        self.player.load.assert_not_awaited()

    async def test_details_and_favourites_are_mutually_exclusive(self):
        c = Controller(self.config, self.player, self.state)
        c.playing = self.channel.uuid
        await c.action('menu')
        await c.action('info')
        self.assertIsNone(c.menu_index)
        self.assertIsNotNone(c.details)
        await c.action('menu')
        self.assertIsNone(c.details)
        self.assertIsNotNone(c.menu_index)
        self.assertIn('Channels', c.osd.overlay)

    async def test_missing_epg_and_late_description_fill_details(self):
        c = Controller(self.config, self.player, self.state)
        c.playing = self.channel.uuid
        await c.action('info')
        self.assertIn('not yet available', c.osd.overlay)
        self.cache(rows=[{**self.rows[0], 'description':'Late description'}])
        await c.draw_details(force=True)
        self.assertIn('Late description', c.osd.overlay)
        self.assertIsNotNone(c.details)

    async def test_details_hold_priority_over_volume_and_banner(self):
        c = Controller(self.config, self.player, self.state)
        c.playing = self.channel.uuid
        self.cache()
        await c.action('info')
        overlay = c.osd.overlay
        await c.osd.volume(40, False)
        self.assertEqual(self.player.ipc.command.await_args.args[0]['id'], 2)
        self.assertIn('40%', self.player.ipc.command.await_args.args[0]['data'])
        await c.show_programme(self.channel)
        self.assertEqual(c.osd.overlay, overlay)
        self.assertEqual(self.player.ipc.command.await_args.args[0]['data'], overlay)

    async def test_escape_and_external_channel_change_close_details(self):
        c = Controller(self.config, self.player, self.state)
        c.playing = self.channel.uuid
        await c.action('info')
        await c.action('back')
        self.assertIsNone(c.details)
        self.assertIsNone(c.osd.overlay)
        await c.action('info')
        c.request(self.data['favourites'][1])
        self.assertIsNone(c.details)
        self.assertIsNone(c.osd.overlay)

    async def test_scan_pause_closes_details(self):
        c = Controller(self.config, self.player, self.state)
        c.playing = self.channel.uuid
        await c.action('info')
        atomic_json(self.config.management_dir/'scan-lease.json', lease_data(30))
        await c.sync_management()
        self.assertIsNone(c.details)
        self.assertIsNone(c.osd.overlay)
        self.assertTrue(c.paused)

    async def test_details_refresh_keeps_scroll_and_renews_persistence(self):
        self.cache(rows=[{**self.rows[0], 'description':'Detailed text. '*200}])
        c = Controller(self.config, self.player, self.state)
        c.playing = self.channel.uuid
        with patch('satip_controller.controller.time.monotonic', return_value=100):
            await c.action('info')
            await c.action('previous')
        count = self.player.ipc.command.await_count
        with patch('satip_controller.controller.time.monotonic', return_value=160):
            await c.draw_details()
        self.assertEqual(c.details['offset'], 1)
        self.assertGreater(self.player.ipc.command.await_count, count)
        self.assertNotIn(1, c.osd.timers)

    async def test_loading_details_fill_after_readiness_without_switching(self):
        c = Controller(self.config, self.player, self.state)
        await c.action('info')
        self.assertIn('Loading', c.osd.overlay)
        self.cache()
        c.playing = self.channel.uuid
        await c.draw_details(force=True)
        self.assertIn('Aktuelle Sendung', c.osd.overlay)
        self.assertEqual(c.requested, self.channel.uuid)

    async def test_unusually_long_title_remains_in_scrollable_body(self):
        title = 'A very long programme title ' * 30 + 'FINAL TITLE WORDS'
        self.cache(rows=[{**self.rows[0], 'title':title, 'description':'Synopsis'}])
        c = Controller(self.config, self.player, self.state)
        c.playing = self.channel.uuid
        await c.action('info')
        self.assertIn('FINAL TITLE WORDS', ' '.join(c.details['text'].split()))
        for _ in range(30):
            await c.action('previous')
        self.assertIn('Synopsis', c.details['text'])

    async def test_scan_pause_dismisses_favourites(self):
        c = Controller(self.config, self.player, self.state)
        await c.action('menu')
        atomic_json(self.config.management_dir/'scan-lease.json', lease_data(30))
        await c.sync_management()
        self.assertIsNone(c.menu_index)
        self.assertIsNone(c.osd.overlay)
        self.assertIn('paused', self.player.ipc.command.await_args.args[0]['data'])

    async def test_new_programme_resets_scroll(self):
        self.cache(rows=[{**self.rows[0], 'description':'Detailed text. '*200}])
        c = Controller(self.config, self.player, self.state)
        c.playing = self.channel.uuid
        await c.action('info')
        await c.action('previous')
        self.cache(rows=[{**self.rows[0], 'title':'New programme', 'start':self.now-30}])
        await c.draw_details(force=True)
        self.assertEqual(c.details['offset'], 0)
        self.assertIn('New programme', c.osd.overlay)
