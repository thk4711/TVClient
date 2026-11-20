import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from validation.monitor_playback_on_pi import monitor


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_passive_trace_filters_private_events_and_cache_fields(self):
        reader = asyncio.StreamReader()
        for message in [
            {'event': 'property-change', 'name': 'path', 'data': 'private-authenticated-url'},
            {'event': 'property-change', 'name': 'time-pos', 'data': 3.5},
            {'event': 'property-change', 'name': 'paused-for-cache', 'data': True},
            {'event': 'property-change', 'name': 'demuxer-cache-state',
             'data': {'cache-duration': 0, 'underrun': True, 'private': 'private-authenticated-url'}},
            {'event': 'playback-restart'},
        ]:
            reader.feed_data((json.dumps(message) + chr(10)).encode())
        writer = SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock(), wait_closed=AsyncMock())
        ipc = SimpleNamespace(get=AsyncMock(return_value={'fresh': [
            {'desc': 'drawing osd', 'last': 4000000, 'avg': 3000000, 'peak': 8000000}]}),
            close=AsyncMock())
        with tempfile.TemporaryDirectory() as root:
            config = SimpleNamespace(runtime_dir=Path(root), state_dir=Path(root), management_dir=Path(root))
            with patch('validation.monitor_playback_on_pi.asyncio.open_unix_connection',
                       AsyncMock(return_value=(reader, writer))), \
                    patch('validation.monitor_playback_on_pi.MPVIPC.connect', AsyncMock(return_value=ipc)), \
                    patch('builtins.print') as output:
                await monitor(config, .15)
        result = json.loads(output.call_args.args[0])
        self.assertNotIn('private-authenticated-url', output.call_args.args[0])
        self.assertTrue(any(s.get('paused-for-cache') for s in result['samples']))
        self.assertEqual(result['samples'][-1]['gpu']['fresh'][0]['last_ms'], 4)
        self.assertEqual(result['samples'][-1]['demuxer-cache-state']['cache-duration'], 0)
        for call in writer.write.call_args_list:
            self.assertEqual(json.loads(call.args[0])['command'][0], 'observe_property')
        ipc.get.assert_awaited_with('vo-passes')
        ipc.close.assert_awaited_once()
        writer.close.assert_called_once()
