import unittest
from unittest.mock import AsyncMock

from validation.measure_switching_on_pi import MuxGrace
from validation.observe_satip_sessions_on_pi import rtsp_event


class GraceComparisonTests(unittest.IsolatedAsyncioTestCase):
    def backend(self, value=0, enabled=True):
        backend = AsyncMock()
        backend.request.return_value = {'entries': [{'uuid': 'a' * 32,
            'params': [{'id': 'idle_grace', 'value': value}, {'id': 'enabled', 'value': enabled}]}]}
        return backend

    async def test_verified_native_change_and_original_restoration(self):
        backend = self.backend(1000)
        grace = MuxGrace(backend, 'a' * 32, 1000)
        grace.original = await grace.read()
        await grace.set(1000)
        await grace.restore()
        self.assertEqual(backend.save.await_count, 2)
        backend.save.assert_awaited_with(uuid='a' * 32, idle_grace=1000)

    async def test_unapplied_change_is_rejected(self):
        grace = MuxGrace(self.backend(0), 'a' * 32, 1000)
        with self.assertRaisesRegex(ValueError, 'did not apply'):
            await grace.set(1000)

    async def test_disabled_missing_or_malformed_frontend_is_rejected(self):
        for value, enabled in [(None, True), (False, True), (0, False)]:
            with self.assertRaises(ValueError):
                await MuxGrace(self.backend(value, enabled), 'a' * 32, 1000).read()
        backend = self.backend()
        backend.request.return_value = {'entries': []}
        with self.assertRaises(ValueError):
            await MuxGrace(backend, 'a' * 32, 1000).read()

    def test_frontend_uuid_is_not_an_arbitrary_backend_node(self):
        with self.assertRaises(ValueError):
            MuxGrace(self.backend(), '../config', 1000)


class PassiveRTSPTests(unittest.TestCase):
    def packet(self, body):
        ethernet = bytes(12) + bytes.fromhex('0800')
        ip = bytearray(20)
        ip[0] = 0x45
        ip[9] = 6
        ip[12:16] = bytes([192, 0, 2, 10])
        ip[16:20] = bytes([192, 0, 2, 50])
        tcp = bytearray(20)
        tcp[:2] = (12345).to_bytes(2, 'big')
        tcp[2:4] = (554).to_bytes(2, 'big')
        tcp[12] = 0x50
        return ethernet + ip + tcp + body

    def test_only_sanitized_method_and_hashed_session_are_returned(self):
        event = rtsp_event(self.packet(b'PLAY rtsp://private/stream RTSP/1.0\r\nSession: secret;timeout=30\r\n'), '192.0.2.50')
        self.assertEqual(event['method'], 'PLAY')
        self.assertEqual(event['client_port'], 12345)
        self.assertEqual(len(event['session']), 12)
        self.assertNotIn('secret', str(event))
        self.assertNotIn('private', str(event))

    def test_truncated_unrelated_or_non_rtsp_data_is_ignored(self):
        for packet in (b'', bytes(54), self.packet(b'other data')):
            self.assertIsNone(rtsp_event(packet, '192.0.2.50'))
        self.assertIsNone(rtsp_event(self.packet(b'PLAY url RTSP/1.0'), '10.0.0.1'))


class ReleaseProbeTests(unittest.IsolatedAsyncioTestCase):
    def test_idle_frontend_is_not_an_active_mux(self):
        from validation.exercise_mux_grace_release_on_pi import active_inputs
        idle = {'stream': None, 'subs': 0}
        active = {'stream': '11362H', 'subs': 0}
        self.assertEqual(active_inputs([idle, active, {'stream': ''}]), [active])

    async def test_release_probe_requires_explicit_interruption(self):
        from types import SimpleNamespace
        from validation.exercise_mux_grace_release_on_pi import exercise
        with self.assertRaisesRegex(ValueError, 'Root and --confirm-interrupt'):
            await exercise(SimpleNamespace(confirm_interrupt=False))
