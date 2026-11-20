import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from satip_controller.mpv_ipc import MPVIPC, IPCError


class Writer:
    def __init__(self): self.messages=[]
    def write(self,data): self.messages.append(json.loads(data))
    async def drain(self): pass
    def close(self): pass
    async def wait_closed(self): pass


class IPCTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.reader=asyncio.StreamReader();self.writer=Writer();self.ipc=MPVIPC(self.reader,self.writer,.1)
    async def asyncTearDown(self): await self.ipc.close()

    async def test_fragmented_event_and_reply(self):
        task=asyncio.create_task(self.ipc.get('volume'));await asyncio.sleep(0)
        self.reader.feed_data(b'{"event":"file-loaded"}\n{"request_id":1,"err')
        await asyncio.sleep(0)
        self.reader.feed_data(b'or":"success","data":35}\n')
        self.assertEqual(await task,35)

    async def test_out_of_order(self):
        a=asyncio.create_task(self.ipc.get('a'));b=asyncio.create_task(self.ipc.get('b'));await asyncio.sleep(0)
        self.reader.feed_data(b'{"request_id":2,"error":"success","data":"B"}\n{"event":"end-file"}\n{"request_id":1,"error":"success","data":"A"}\n')
        self.assertEqual(await a,'A');self.assertEqual(await b,'B')

    async def test_named_overlay_command(self):
        command = {'name': 'osd-overlay', 'id': 1, 'format': 'ass-events',
                   'data': 'literal ${volume}', 'res_x': 1920, 'res_y': 1080}
        task = asyncio.create_task(self.ipc.command(command))
        await asyncio.sleep(0)
        self.assertEqual(self.writer.messages[0]['command'], command)
        self.reader.feed_data(b'{"request_id":1,"error":"success"}\n')
        await task

    async def test_timeout_cleanup(self):
        with self.assertRaises(IPCError) as raised: await self.ipc.get('volume')
        self.assertEqual(raised.exception.reason, 'ipc_timeout')
        self.assertFalse(self.ipc.pending)
        self.assertTrue(self.ipc.closed)
        with self.assertRaises(IPCError):
            await self.ipc.get("volume")
        self.assertEqual(len(self.writer.messages), 1)

    async def test_disconnect(self):
        task=asyncio.create_task(self.ipc.get('volume'));await asyncio.sleep(0);self.reader.feed_eof()
        with self.assertRaises(IPCError): await task
        self.assertTrue(self.ipc.closed)

    async def test_load_error_is_correlated_and_reset_for_next_load(self):
        self.ipc.begin_load()
        self.ipc.load_event({"event": "start-file", "playlist_entry_id": 8})
        self.ipc.load_event({"event": "end-file", "playlist_entry_id": 7, "reason": "error"})
        self.assertFalse(self.ipc.load_failure)
        self.ipc.load_event({"event": "end-file", "playlist_entry_id": 8, "reason": "stop"})
        self.assertFalse(self.ipc.load_failure)
        self.reader.feed_data(b'{"event":"end-file","playlist_entry_id":8,"reason":"error"}\n')
        await asyncio.sleep(0)
        self.assertTrue(self.ipc.load_failure)
        self.ipc.begin_load()
        self.ipc.load_event({"event": "end-file", "playlist_entry_id": 8, "reason": "error"})
        self.assertFalse(self.ipc.load_failure)

    async def test_invalid_json(self):
        self.reader.feed_data(b'bad\n');await asyncio.sleep(0)
        self.assertTrue(self.ipc.closed)

    async def test_redacted_error(self):
        task=asyncio.create_task(self.ipc.command('loadfile','secret'));await asyncio.sleep(0)
        self.reader.feed_data(b'{"request_id":1,"error":"sensitive-test-password"}\n')
        with self.assertRaises(IPCError) as raised: await task
        self.assertNotIn('sensitive',str(raised.exception))
        self.assertFalse(self.ipc.closed)

    async def test_cancel_cleanup(self):
        task=asyncio.create_task(self.ipc.get('volume'));await asyncio.sleep(0);task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        self.assertFalse(self.ipc.pending)
