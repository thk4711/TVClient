from satip_controller.state import lease_data, lease_active
import asyncio
import dataclasses
import time
import unittest
from unittest.mock import AsyncMock, Mock

from satip_controller.controller import Controller
from satip_controller.mpv_ipc import IPCError
from satip_controller.scan import Manager
from satip_controller.state import atomic_json, read_json
from tests.helpers import Fixture


class LifecycleTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self): self.setup_files()
    async def asyncTearDown(self): await self.state.close()

    async def test_load_coalescing_and_cleanup(self):
        position=0
        async def sample(channel):
            nonlocal position
            position+=.2
            return position
        self.player.sample=sample
        self.player.select_audio=AsyncMock(return_value=True)
        self.player.ipc.get=AsyncMock(side_effect=lambda key: 35 if key=='volume' else False)
        c=Controller(self.config,self.player,self.state)
        task=asyncio.create_task(c.run())
        for id in self.data['favourites']*3: c.request(id)
        try:
            async with asyncio.timeout(3):
                while not c.playing: await asyncio.sleep(.05)
            self.assertEqual(c.playing,self.data['favourites'][-1])
            self.assertEqual(self.player.load.await_count,1)
            self.assertEqual(self.state.data['channel'],c.playing)
            self.assertEqual(c.osd.durations[1], 5000)
            self.assertTrue(any(isinstance(call.args[0], dict)
                                and 'Now:' in call.args[0].get('data', '')
                                for call in self.player.ipc.command.await_args_list))
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.player.close.assert_awaited()

    async def test_live_audio_fallback_confirms_playback_without_retry(self):
        from satip_controller.player import Player
        channel = self.data["channels"][0]
        position = 0
        async def sample(_channel):
            nonlocal position
            position += .2
            return position
        tracks = [{"type": "audio", "id": 1, "src-id": channel["audio_pid"] + 1,
                   "codec": "mp2", "lang": "deu", "visual-impaired": False,
                   "hearing-impaired": False, "external": False, "selected": True}]
        self.player.sample = sample
        real = Player(self.config, self.state)
        real.ipc = self.player.ipc
        self.player.select_audio = real.select_audio
        self.player.ipc.get = AsyncMock(side_effect=lambda key: tracks if key == "track-list"
                                       else 35 if key == "volume" else False)
        c = Controller(dataclasses.replace(self.config, load_timeout=1), self.player, self.state)
        with self.assertLogs("satip_controller.player", level="WARNING"):
            task = asyncio.create_task(c.run())
            try:
                async with asyncio.timeout(3):
                    while not c.playing:
                        await asyncio.sleep(.05)
                await asyncio.sleep(1.1)  # Past the load deadline, still no retry.
                self.assertEqual(c.playing, channel["uuid"])
                self.assertEqual(c.retry_count, 0)
                self.player.load.assert_awaited_once()
                self.player.stop_stream.assert_not_awaited()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_no_false_ready_without_advance(self):
        self.player.sample=AsyncMock(return_value=0)
        self.player.select_audio=AsyncMock(return_value=True)
        c=Controller(dataclasses.replace(self.config,load_timeout=.25),self.player,self.state)
        task=asyncio.create_task(c.run())
        try:
            async with asyncio.timeout(3):
                while not c.error: await asyncio.sleep(.05)
            self.assertIsNone(c.playing)
            self.player.stop_stream.assert_awaited()
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_inflight_tune_finishes_before_latest_request_loads(self):
        c = Controller(self.config, self.player, self.state)
        first, skipped, last = self.data['favourites']
        started, ready = asyncio.Event(), asyncio.Event()
        position = 0
        async def load(channel):
            if channel.uuid == first:
                started.set()
            else:
                self.assertTrue(ready.is_set(), 'An in-flight tune was interrupted')
        async def sample(channel):
            nonlocal position
            if not ready.is_set():
                return None
            position += .2
            return position
        self.player.load = AsyncMock(side_effect=load)
        self.player.sample = AsyncMock(side_effect=sample)
        self.player.select_audio = AsyncMock(return_value=True)
        self.player.ipc.get = AsyncMock(side_effect=lambda key: 35 if key == 'volume' else False)
        task = asyncio.create_task(c.run())
        try:
            await asyncio.wait_for(started.wait(), 3)
            # These run through the same action/OSD path as physical buttons.
            await asyncio.wait_for(c.action('next'), .5)
            self.assertEqual(c.requested, skipped)
            await asyncio.sleep(.15)
            await asyncio.wait_for(c.action('next'), .5)
            await asyncio.sleep(.3)
            self.assertEqual(c.requested, last)
            self.assertEqual(c.loading, first)
            self.assertIsNone(c.playing)
            self.assertEqual(self.player.load.await_count, 1)
            self.assertEqual(self.state.data['channel'], first)
            ready.set()
            async with asyncio.timeout(3):
                while c.playing != last:
                    await asyncio.sleep(.05)
                    self.assertNotEqual(c.playing, first)
                    self.assertNotEqual(self.state.data['channel'], skipped)
            self.assertEqual([call.args[0].uuid for call in self.player.load.await_args_list],
                             [first, last])
            self.assertEqual(self.state.data['channel'], last)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_superseded_unplayable_load_still_has_a_deadline(self):
        self.player.sample = AsyncMock(return_value=None)
        c = Controller(dataclasses.replace(self.config, load_timeout=.25),
                       self.player, self.state)
        task = asyncio.create_task(c.run())
        try:
            async with asyncio.timeout(3):
                while not self.player.load.await_count:
                    await asyncio.sleep(.01)
                c.request(self.data['favourites'][-1])
                while self.player.load.await_count < 2:
                    await asyncio.sleep(.05)
            self.assertEqual(c.reason, 'load_timeout')
            self.assertEqual(self.player.load.await_args.args[0].uuid, c.requested)
            self.assertIsNone(c.playing)
            self.assertEqual(self.state.data['channel'], self.data['favourites'][0])
            self.player.stop_stream.assert_awaited()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_management_pause_interrupts_superseded_load(self):
        self.player.sample = AsyncMock(return_value=None)
        c = Controller(self.config, self.player, self.state)
        task = asyncio.create_task(c.run())
        try:
            async with asyncio.timeout(3):
                while not self.player.load.await_count:
                    await asyncio.sleep(.01)
                c.request(self.data['favourites'][-1])
                atomic_json(self.config.management_dir / 'scan-lease.json', lease_data(30))
                while not c.paused:
                    await asyncio.sleep(.05)
            await asyncio.sleep(.2)
            self.player.stop_stream.assert_awaited_once()
            self.assertEqual(self.player.load.await_count, 1)
            self.assertIsNone(c.playing)
            self.assertIsNone(c.loading)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_request_changed_during_osd_does_not_start_obsolete_load(self):
        self.player.sample = AsyncMock(return_value=None)
        c = Controller(self.config, self.player, self.state)
        last = self.data['favourites'][-1]
        async def switching(channel):
            if channel.uuid != last:
                c.request(last)
            return 0
        c.show_switching = switching
        task = asyncio.create_task(c.run())
        try:
            async with asyncio.timeout(3):
                while not self.player.load.await_count:
                    await asyncio.sleep(.01)
            self.assertEqual(self.player.load.await_count, 1)
            self.assertEqual(self.player.load.await_args.args[0].uuid, last)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_stale_generation_not_saved(self):
        c=Controller(self.config,self.player,self.state)
        called=False
        async def sample(channel):
            nonlocal called
            if not called:
                called=True;c.request(self.data['favourites'][1])
            return .1
        self.player.sample=sample
        self.player.select_audio=AsyncMock(return_value=True)
        task=asyncio.create_task(c.run())
        try:
            await asyncio.sleep(.6)
            self.assertIsNone(c.playing)
            self.assertEqual(self.state.data['channel'],self.data['favourites'][0])
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_expired_lease_with_pending_recovery_still_pauses(self):
        c=Controller(self.config,self.player,self.state)
        atomic_json(self.config.management_dir/'scan-lease.json', {'until':0, 'recovery_pending':True})
        await c.sync_management()
        self.assertTrue(c.paused)

    async def test_scan_pause_and_resume(self):
        c=Controller(self.config,self.player,self.state)
        atomic_json(self.config.management_dir/'scan-lease.json',lease_data(30))
        await c.sync_management()
        self.assertTrue(c.paused);self.player.stop_stream.assert_awaited_once()
        atomic_json(self.config.management_dir/'scan-lease.json',{'until':0})
        await c.sync_management();self.assertFalse(c.paused)

    async def test_unresponsive_player_recovers_before_restart(self):
        self.player.recover = Mock()
        async def load(channel):
            self.player.ipc.closed = True
            raise IPCError("Player command timed out", reason="ipc_timeout")
        self.player.load = AsyncMock(side_effect=load)
        c = Controller(self.config, self.player, self.state)
        task = asyncio.create_task(c.run())
        try:
            async with asyncio.timeout(3):
                while not c.error:
                    await asyncio.sleep(.05)
            self.player.recover.assert_called_once_with("ipc_timeout")
            self.player.close.assert_awaited()
            self.assertIsNone(c.playing)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_disconnected_player_recreated(self):
        self.player.ipc.closed=True
        async def start(): self.player.ipc.closed=False
        self.player.start=AsyncMock(side_effect=start)
        self.player.sample=AsyncMock(return_value=None)
        c=Controller(self.config,self.player,self.state)
        task=asyncio.create_task(c.run())
        try:
            await asyncio.sleep(.3);self.player.start.assert_awaited_once()
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)


class FakeBackend:
    def __init__(self):
        self.mux={'uuid':'c'*32,'network_uuid':'a'*32,'enabled':0,'epg':0,
                  'name':'test','scan_state':0,'scan_last':0,'scan_result':1}
        self.saves=[]
        self.event=asyncio.Event()
    async def save(self,**node):
        self.saves.append(node.copy());self.mux.update(node)
        if node.get('scan_state')==1:
            self.event.set();self.mux.update(scan_state=0,scan_last=int(time.time())+1)
    async def grid(self,name):
        if name=='mpegts/mux/grid': return [self.mux.copy()]
        if name=='access/entry/grid':
            return [{'uuid':'b'*32,'admin':False,'webui':False,'conn_limit':1,
                     'channel_min':1000000,'channel_max':3000000}]
        if name=='mpegts/service/grid':
            return [{'uuid':'d'*32,'multiplex_uuid':'c'*32,'svcname':'FTA','enabled':True,'encrypted':False}]
        return []
    async def request(self,*args,**kwargs):
        return {'streams':[{'type':'H264'},{'type':'MPEG2AUDIO','pid':42,'language':'ger','audio_type':0}]}


class ScanTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self): self.setup_files();self.backend=FakeBackend();self.manager=Manager(self.backend,self.web_config)
    async def asyncTearDown(self): await self.state.close();await self.manager.close()

    async def test_candidate_subset_skips_unselected_stream_requests(self):
        original=self.backend.grid
        async def grid(endpoint):
            found=await original(endpoint)
            if endpoint=='mpegts/service/grid':
                return found + [{**found[0], 'uuid':'e'*32}]
            return found
        self.backend.grid=grid
        self.backend.request=AsyncMock(return_value={'streams':[
            {'type':'H264'}, {'type':'MPEG2AUDIO','pid':42,'language':'ger','audio_type':0}]})
        found=await self.manager.candidates(service_ids={'d'*32})
        self.assertEqual(len(found),1)
        self.backend.request.assert_awaited_once_with('service/streams',uuid='d'*32)

    async def test_scan_restores_disabled_mux(self):
        await self.manager.scan()
        self.assertEqual(self.manager.status['state'],'complete')
        self.assertEqual(self.manager.status['found'],1)
        self.assertEqual(self.backend.mux['enabled'],0)
        self.assertFalse(self.manager.journal.exists())
        self.assertFalse(lease_active(read_json(self.config.management_dir/'scan-lease.json')))
        restarted=Manager(self.backend,self.web_config)
        self.assertEqual(restarted.status['state'],'complete')
        self.assertEqual(restarted.status['found'],1)

    async def test_partial_scan_publishes_channels_and_restores_mux(self):
        self.backend.mux['scan_result']=3
        await self.manager.scan()
        self.assertEqual(self.manager.status['found'],1)
        self.assertEqual(self.manager.status['partial_muxes'],1)
        self.assertEqual(self.manager.status['failed_muxes'],0)
        self.assertEqual(self.backend.mux['enabled'],0)
        self.assertFalse(self.manager.journal.exists())
        channels=read_json(self.manager.directory/'candidates.json')['channels']
        self.assertIn('d'*32,[c['service_uuid'] for c in channels])
        self.assertEqual(Manager(self.backend,self.web_config).status['partial_muxes'],1)

    async def test_failed_scan_does_not_publish_services(self):
        self.backend.mux['scan_result']=2
        await self.manager.scan()
        self.assertEqual(self.manager.status['found'],0)
        self.assertEqual(self.manager.status['failed_muxes'],1)
        self.assertEqual(self.manager.status['partial_muxes'],0)
        self.assertEqual(self.manager.catalogue_revision,0)
        self.assertEqual(self.backend.mux['enabled'],0)

    async def test_cancel_restores(self):
        self.manager.task=asyncio.create_task(self.manager.scan())
        await self.backend.event.wait();await self.manager.close()
        self.assertEqual(self.backend.mux['enabled'],0)
        self.assertEqual(self.manager.status['state'],'cancelled')
        self.assertFalse(self.manager.journal.exists())

    async def test_incremental_discoveries_survive_cancel(self):
        second={**self.backend.mux, 'uuid':'e'*32, 'name':'second'}
        original_grid=self.backend.grid
        second_started=asyncio.Event()
        original_save=self.backend.save
        async def grid(endpoint):
            if endpoint=='mpegts/mux/grid': return [self.backend.mux.copy(), second.copy()]
            return await original_grid(endpoint)
        async def save(**node):
            if node['uuid']==second['uuid']:
                second.update(node)
                if node.get('scan_state')==1: second_started.set()
            else: await original_save(**node)
        self.backend.grid=grid;self.backend.save=save
        self.manager.task=asyncio.create_task(self.manager.scan())
        await asyncio.wait_for(second_started.wait(),3)
        self.assertEqual(self.manager.status['state'],'running')
        self.assertEqual(self.manager.status['completed'],1)
        self.assertEqual(self.manager.status['found'],1)
        self.assertEqual(self.manager.catalogue_revision,1)
        channels=read_json(self.manager.directory/'candidates.json')['channels']
        self.assertIn('d'*32,[c['service_uuid'] for c in channels])
        self.assertEqual(len(channels),4)
        await self.manager.close()
        self.assertEqual(self.manager.status['state'],'cancelled')
        self.assertEqual(read_json(self.manager.directory/'candidates.json')['channels'],channels)
        self.assertEqual(read_json(self.config.library),self.data)
        self.assertEqual(second['enabled'],0)
        self.assertFalse(self.manager.journal.exists())

    async def test_publication_deduplicates_and_preserves_known_channels(self):
        item={'service_uuid':'d'*32,'name':'Discovered'}
        self.manager.publish_candidates([item,item])
        self.manager.publish_candidates([{**item,'name':'Updated name'}])
        channels=read_json(self.manager.directory/'candidates.json')['channels']
        self.assertEqual(len(channels),4)
        self.assertEqual(next(c for c in channels if c['service_uuid']=='d'*32)['name'],'Updated name')
        self.assertEqual(self.manager.catalogue_revision,2)

    async def test_crash_journal_recovery(self):
        atomic_json(self.manager.journal,{'uuid':'c'*32,'enabled':0,'epg':0,'scan_state':0})
        self.backend.mux['enabled']=1
        await self.manager.recover()
        self.assertEqual(self.backend.mux['enabled'],0)
        self.assertFalse(self.manager.journal.exists())

    async def test_failed_restore_keeps_playback_paused(self):
        from satip_controller.backend import BackendError
        original=self.backend.save
        async def save(**node):
            if node['enabled']==0: raise BackendError('private detail')
            await original(**node)
        self.backend.save=save
        await self.manager.scan()
        self.assertTrue(self.manager.journal.exists())
        self.assertTrue(self.manager.status['recovery_pending'])
        self.assertTrue(lease_active(read_json(self.config.management_dir/'scan-lease.json')))

    async def test_encrypted_not_saved(self):
        with self.assertRaises(ValueError): await self.manager.save_favourites(['e'*32])
        self.assertEqual(read_json(self.config.library),self.data)
        self.assertEqual(self.backend.saves,[])
