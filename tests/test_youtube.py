import asyncio
import json
import time
import unittest
from urllib.parse import urlsplit
import httpx
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from tests.helpers import Fixture
from satip_controller.config import Channel
from satip_controller.controller import Controller
from satip_controller.player import LIVE_BUFFER_OPTIONS, Player
from satip_controller.state import atomic_json, boot_id, runtime_path
from satip_controller.youtube import Frames, Lounge, MediaProxy, media_url, number, preferences, public_status
from satip_controller.mpv_ipc import IPCError
from satip_controller.youtube import resolve
from satip_controller.input import Inputs


def frame(messages):
    data = json.dumps(messages, ensure_ascii=False)
    return str(len(data.encode('utf-16-le')) // 2) + '\n' + data


class ProtocolTests(unittest.TestCase):
    def test_split_frames_unicode_and_multiple_messages(self):
        messages = [[0, ['c', 'session', '', 8]], [1, ['S', 'generation']],
                    [2, ['setPlaylist', {'videoId': 'aqz-KE-bpKQ', 'name': 'TV 📺'}]]]
        text = frame(messages[:2]) + frame(messages[2:])
        parser, received = Frames(), []
        for char in text:
            received.extend(parser.feed(char))
        self.assertEqual(received, messages)
        self.assertEqual(parser.buffer, '')

    def test_invalid_frames_fail_closed(self):
        for text in ('not-a-number\n[]', '999999999\n', '0\n', '2\n{}', '4\n[42]'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                Frames().feed(text)

    def test_only_googlevideo_https_without_credentials(self):
        allowed = 'https://rr1.example.googlevideo.com/videoplayback?id=test'
        self.assertEqual(media_url(allowed), allowed)
        for url in ('http://rr1.googlevideo.com/a', 'https://googlevideo.com.evil.test/a',
                    'https://127.0.0.1/a', 'file:///etc/shadow', 'https://user:secret@x.googlevideo.com/a',
                    'https://x.googlevideo.com:9981/a', allowed + '\n'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                media_url(url)

    def test_numeric_controls_are_bounded(self):
        for value in ('nan', 'inf', -1, None, 100001):
            self.assertEqual(number(value), 0)
        self.assertEqual(number('12.5'), 12.5)


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    def process(self, output, running=False):
        reader = asyncio.StreamReader()
        reader.feed_data(output)
        reader.feed_eof()
        process = Mock(stdout=reader, returncode=None if running else 0)
        async def wait():
            process.returncode = 0
        process.wait = AsyncMock(side_effect=wait)
        return process

    async def test_only_selected_stream_metadata_is_requested(self):
        video = 'https://test.googlevideo.com/video'
        audio = 'https://test.googlevideo.com/audio'
        output = json.dumps({'requested_formats': [
            {'url': video, 'vcodec': 'avc1.4D401F', 'acodec': 'none'},
            {'url': audio, 'vcodec': 'none', 'acodec': 'mp4a.40.2'}]}).encode()
        process = self.process(output)
        with patch('satip_controller.youtube.asyncio.create_subprocess_exec',
                   new_callable=AsyncMock, return_value=process) as spawn:
            self.assertEqual(await resolve('DJYupQGdJVY'), (video, audio))
        args = spawn.await_args.args
        self.assertNotIn('--dump-single-json', args)
        self.assertEqual(args[args.index('--print') + 1],
                         '%(.{url,vcodec,acodec,requested_formats})j')
        process.wait.assert_awaited()

    async def test_compact_single_stream_is_supported(self):
        video = 'https://test.googlevideo.com/video'
        process = self.process(json.dumps({'url': video, 'vcodec': 'avc1.4D401F',
                                           'acodec': 'mp4a.40.2'}).encode())
        with patch('satip_controller.youtube.asyncio.create_subprocess_exec',
                   new_callable=AsyncMock, return_value=process):
            self.assertEqual(await resolve('DJYupQGdJVY'), (video, None))

    async def test_output_bound_still_kills_and_joins_resolver(self):
        process = self.process(b'x' * 257, running=True)
        with (patch('satip_controller.youtube.asyncio.create_subprocess_exec',
                    new_callable=AsyncMock, return_value=process),
              patch('satip_controller.youtube.MAX_FRAME', 32)):
            with self.assertRaisesRegex(ValueError, 'Resolver output too large'):
                await resolve('DJYupQGdJVY')
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()


class ReceiverTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_files()
        self.controller = Controller(self.config, self.player, self.state)
        self.youtube = self.controller.youtube

    async def asyncTearDown(self):
        await self.youtube.stop()
        await self.state.close()

    def test_disabled_default_and_strict_preferences(self):
        self.assertFalse(preferences(self.config.management_dir)['enabled'])
        atomic_json(self.config.management_dir / 'youtube.json', {'enabled': True})
        with self.assertRaises(ValueError):
            preferences(self.config.management_dir)

    def test_public_status_redacts_identity_and_expires_code(self):
        data = {'boot_id': boot_id(), 'updated': time.monotonic(), 'state': 'ready',
                'active': False, 'pairing_code': '123456789012', 'code_until': time.monotonic() + 30,
                'screen_id': 'private', 'loungeToken': 'secret'}
        path = runtime_path(self.config.state_dir, 'youtube-status.json')
        atomic_json(path, data)
        result = public_status(self.config.state_dir)
        self.assertEqual(result['pairing_code'], '123456789012')
        self.assertNotIn('screen_id', result)
        self.assertNotIn('loungeToken', result)
        data['code_until'] = 0
        atomic_json(path, data)
        self.assertIsNone(public_status(self.config.state_dir)['pairing_code'])
        data['boot_id'] = 'old-boot'
        atomic_json(path, data)
        self.assertEqual(public_status(self.config.state_dir)['state'], 'unavailable')

    async def test_external_pause_rejects_remote_play(self):
        self.youtube.enabled = True
        await self.youtube.incoming.put(('setPlaylist', {'videoIds': 'aqz-KE-bpKQ'}))
        with patch('satip_controller.youtube.resolve', new_callable=AsyncMock) as resolve:
            await self.youtube.tick(True)
            resolve.assert_not_called()
        self.assertFalse(self.youtube.active)

    async def test_local_audio_buttons_preserve_youtube_and_report_volume(self):
        for loaded in (False, True):
            for action, readings in (('volume_up', [35, 40, False]),
                                     ('volume_down', [40, 35, False])):
                with self.subTest(loaded=loaded, action=action):
                    self.youtube.active, self.youtube.loaded = True, loaded
                    self.youtube.video_id = 'DJYupQGdJVY'
                    self.player.stop_stream.reset_mock()
                    self.player.ipc.get = AsyncMock(side_effect=readings)
                    self.youtube.notify = Mock()
                    generation = self.controller.generation
                    self.assertTrue(await self.controller.local_press(action))
                    await self.controller.action(action)
                    self.assertTrue(self.youtube.active)
                    self.assertEqual(self.youtube.video_id, 'DJYupQGdJVY')
                    self.assertEqual(self.controller.generation, generation)
                    self.player.stop_stream.assert_not_awaited()
                    self.youtube.notify.assert_called_once_with('onVolumeChanged', {
                        'volume': self.state.data['volume'], 'muted': self.state.data['mute'],
                    })
        self.youtube.active = False

    async def test_local_mute_toggles_youtube_pause_without_changing_audio(self):
        self.youtube.active = self.youtube.loaded = True
        self.youtube.video_id = 'DJYupQGdJVY'
        self.player.ipc.closed = False
        paused = False
        async def command(*args):
            nonlocal paused
            self.assertEqual(args, ('cycle', 'pause'))
            paused = not paused
        self.player.ipc.command.side_effect = command
        self.youtube.notify = Mock()
        self.controller.osd.volume = AsyncMock()
        generation = self.controller.generation
        audio = dict(self.state.data)
        for expected in (True, False):
            self.youtube.reported = time.monotonic()
            self.assertTrue(await self.controller.local_press('mute'))
            await self.controller.action('mute')
            self.assertEqual(paused, expected)
            self.assertEqual(self.youtube.reported, 0)
            self.assertTrue(self.youtube.active)
            self.assertEqual(self.youtube.video_id, 'DJYupQGdJVY')
            self.assertEqual(self.controller.generation, generation)
        self.assertEqual(self.state.data, audio)
        self.player.stop_stream.assert_not_awaited()
        self.player.ipc.get.assert_not_awaited()
        self.controller.osd.volume.assert_not_awaited()
        self.youtube.notify.assert_not_called()
        self.player.ipc.command.side_effect = None

    async def test_youtube_mute_while_loading_or_disconnected_does_not_mute_or_pause_tv(self):
        for loaded, connected in ((False, True), (True, False)):
            with self.subTest(loaded=loaded, connected=connected):
                self.youtube.active, self.youtube.loaded = True, loaded
                self.player.ipc.closed = not connected
                self.player.ipc.command.reset_mock()
                await self.controller.action('mute')
                self.player.ipc.command.assert_not_awaited()
        self.youtube.active = False

    async def test_local_mute_still_mutes_tv_when_youtube_is_inactive(self):
        self.youtube.active = False
        self.player.ipc.closed = False
        self.player.ipc.get.side_effect = [35, True]
        await self.controller.action('mute')
        self.player.ipc.command.assert_any_await('cycle', 'mute')
        self.assertFalse(any(call.args == ('cycle', 'pause')
                             for call in self.player.ipc.command.await_args_list))
        self.assertEqual(self.state.data['volume'], 35)
        self.assertTrue(self.state.data['mute'])

    async def test_local_navigation_and_unmapped_keys_still_stop_youtube(self):
        for action in ('next', 'previous', 'menu', 'back', 'info', None):
            with self.subTest(action=action):
                self.youtube.active = True
                self.player.stop_stream.reset_mock()
                self.assertTrue(await self.controller.local_press(action))
                self.assertFalse(self.youtube.active)
                self.player.stop_stream.assert_awaited_once()

    async def test_loading_properties_unavailable_are_not_ipc_disconnect(self):
        self.player.ipc.closed = False
        self.player.ipc.get.side_effect = IPCError('Property unavailable')
        self.assertIsNone(await self.youtube.sample_property('time-pos'))
        self.player.ipc.closed = True
        with self.assertRaises(IPCError):
            await self.youtube.sample_property('time-pos')

    async def test_bridge_cleanup_even_if_player_stop_fails(self):
        self.youtube.active = True
        media = self.youtube.media = Mock(close=AsyncMock())
        self.player.stop_stream.side_effect = IPCError('Disconnected')
        with self.assertRaises(IPCError):
            await self.youtube.stop()
        media.close.assert_awaited_once()
        self.assertFalse(self.youtube.active)
        self.assertIsNone(self.youtube.media)

    async def test_local_takeover_cancels_and_joins_extraction(self):
        self.youtube.enabled = True
        finished = asyncio.Event()
        async def resolve(_):
            try:
                await asyncio.sleep(100)
            finally:
                finished.set()
        with patch('satip_controller.youtube.resolve', resolve):
            await self.youtube.begin('aqz-KE-bpKQ')
            await asyncio.sleep(0)
            await self.youtube.incoming.put(('setPlaylist', {'videoIds': 'aqz-KE-bpKQ'}))
            await self.youtube.local_stop()
        self.assertTrue(finished.is_set())
        self.assertFalse(self.youtube.active)
        self.assertIsNone(self.youtube.resolver)
        self.assertTrue(self.youtube.incoming.empty())

    async def test_session_dispatch_and_bounded_outgoing(self):
        incoming = asyncio.Queue()
        lounge = Lounge({'screen_id': 'private', 'device_id': 'device'}, incoming)
        await lounge.dispatch([[0, ['c', 'session']], [1, ['S', 'generation']],
                               [2, ['pause', {}]]])
        self.assertEqual(lounge.sid, 'session')
        self.assertEqual(lounge.gsession, 'generation')
        self.assertEqual(incoming.get_nowait(), ('pause', {}))
        for i in range(100):
            lounge.notify('onStateChange', {'currentTime': i})
        self.assertEqual(lounge.outgoing.qsize(), 32)

    async def test_external_load_removes_tv_secret_before_load_and_restores_tv(self):
        player = Player(self.config, self.state)
        player.ipc = self.player.ipc
        player.ipc.begin_load = Mock()
        player.stop_stream = AsyncMock()
        player.tv_header = 'Authorization: Basic private'
        video = 'http://127.0.0.1:12345/' + 'a' * 48 + '/video'
        audio = 'http://127.0.0.1:12345/' + 'a' * 48 + '/audio'
        await player.load_youtube(video, audio, 12)
        commands = player.ipc.command.await_args_list
        clear = next(i for i, call in enumerate(commands) if call.args == ('set_property', 'http-header-fields', []))
        load = next(i for i, call in enumerate(commands) if call.args[0] == 'loadfile')
        self.assertLess(clear, load)
        self.assertTrue(player.external_stream)
        player.ipc.command.assert_any_await('set_property', 'tls-verify', True)
        external_load = commands[load].args
        self.assertEqual(external_load[:4], ('loadfile', video, 'replace', -1))
        self.assertEqual(external_load[4], {
            'cache': 'yes', 'cache-pause': 'yes', 'cache-pause-initial': 'yes',
            'cache-pause-wait': '3', 'demuxer-readahead-secs': '20', 'cache-secs': '20',
        })
        await player.load(Channel.parse(self.data['channels'][0]))
        player.ipc.command.assert_any_await('set_property', 'http-header-fields', [player.tv_header])
        player.ipc.command.assert_any_await('set_property', 'audio-files', [])
        self.assertFalse(player.external_stream)
        tv_load = [call.args for call in player.ipc.command.await_args_list
                   if call.args[0] == 'loadfile'][-1]
        self.assertEqual(tv_load[3], -1)
        self.assertEqual(tv_load[4], {**player.timeshift.options(), **LIVE_BUFFER_OPTIONS})
        self.assertEqual(tv_load[4]['cache-pause-wait'], '1')  # Not YouTube's three-second cushion.
        self.assertFalse(any(call.args[:2] == ('set_property', option)
                             for call in player.ipc.command.await_args_list
                             for option in external_load[4]))


class InputHandoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_keydown_passes_group_specific_action_before_dispatch(self):
        for group, code, action in (('keyboard', 113, 'pause'), ('remote', 113, 'menu'),
                                    ('remote', 115, 'remote_right'), ('keyboard', 114, 'volume_down'),
                                    ('keyboard', 999, None)):
            with self.subTest(group=group, code=code):
                callback, handover = AsyncMock(), AsyncMock(return_value=True)
                inputs = Inputs(SimpleNamespace(repeat_delay=0, repeat_interval=0, debounce=0),
                                callback, handover)
                async def events():
                    for value in (1, 2, 0):
                        yield SimpleNamespace(type=1, code=code, value=value)
                device = SimpleNamespace(path='test', async_read_loop=events)
                await inputs._read(device, group)
                handover.assert_awaited_once_with(action)
                if action:
                    self.assertEqual(callback.await_args_list[0].args, (action,))
                    self.assertEqual(callback.await_count, 2 if action.startswith(('volume_', 'remote_')) else 1)
                else:
                    callback.assert_not_awaited()


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.proxy = MediaProxy()
        self.video, _ = await self.proxy.start('https://test.googlevideo.com/video', None)
        await self.proxy.client.aclose()
        self.requests = []
        def upstream(request):
            self.requests.append(request)
            return httpx.Response(206, headers={'Content-Range': 'bytes 0-3/10',
                                               'Content-Length': '4'}, stream=httpx.ByteStream(b'test'))
        self.proxy.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), trust_env=False)

    async def asyncTearDown(self):
        await self.proxy.close()

    async def request(self, extra='', path=None):
        url = urlsplit(self.video)
        reader, writer = await asyncio.open_connection(url.hostname, url.port)
        writer.write(('GET ' + (path or url.path) + ' HTTP/1.0\r\n' + extra + '\r\n').encode())
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        return response

    async def test_ranges_and_no_forwarded_credentials(self):
        self.proxy.client.cookies.set('private', 'secret', domain='test.googlevideo.com')
        response = await self.request('Range: bytes=0-3\r\nAuthorization: Basic private\r\nCookie: secret\r\n')
        self.assertTrue(response.startswith(b'HTTP/1.0 206'))
        self.assertTrue(response.endswith(b'test'))
        request = self.requests[0]
        self.assertEqual(request.headers['Range'], 'bytes=0-3')
        self.assertNotIn('authorization', request.headers)
        self.assertNotIn('cookie', request.headers)

    async def test_allowed_redirect_preserves_range_without_cookies(self):
        await self.proxy.client.aclose()
        requests = []
        def upstream(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(302, headers={'Location': 'https://test.googlevideo.com/next',
                                                   'Set-Cookie': 'private=secret'})
            return httpx.Response(206, stream=httpx.ByteStream(b'test'))
        self.proxy.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.assertTrue((await self.request('Range: bytes=0-3\r\n')).endswith(b'test'))
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1].headers['range'], 'bytes=0-3')
        self.assertNotIn('cookie', requests[1].headers)

    async def test_unknown_path_and_invalid_range_do_not_contact_upstream(self):
        self.assertIn(b'502', await self.request(path='/arbitrary'))
        self.assertIn(b'502', await self.request('Range: bytes=0-1,3-4\r\n'))
        self.assertEqual(self.requests, [])

    async def test_redirect_outside_googlevideo_is_rejected(self):
        await self.proxy.client.aclose()
        self.proxy.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(302, headers={'Location': 'http://127.0.0.1:9981/private'})))
        self.assertIn(b'502', await self.request())

    async def test_close_joins_waiting_connections(self):
        url = urlsplit(self.video)
        reader, writer = await asyncio.open_connection(url.hostname, url.port)
        await asyncio.sleep(0)
        await self.proxy.close()
        self.assertEqual(await reader.read(), b'')
        self.assertFalse(self.proxy.tasks)
        writer.close()
        await writer.wait_closed()
