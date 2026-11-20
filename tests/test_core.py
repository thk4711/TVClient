import json
import unittest
from unittest.mock import AsyncMock

from satip_controller.backend import candidate
from satip_controller.channels import step
from satip_controller.config import Config, Channel, credentials, library
from satip_controller.controller import Controller
from satip_controller.input import Policy, selected
from satip_controller.osd import OSD, literal, volume_label
from satip_controller.player import arguments, BWDIF
from satip_controller.state import State, atomic_json, read_json
from tests.helpers import Fixture


class CoreTests(Fixture, unittest.TestCase):
    def setUp(self): self.setup_files()

    def test_wrap(self):
        a,b,c=self.data['favourites']
        self.assertEqual(step([a,b,c],c,1),a)
        self.assertEqual(step([a,b,c],a,-1),c)

    def test_single(self): self.assertEqual(step(['x'],'x',1),'x')
    def test_empty(self):
        with self.assertRaises(ValueError): step([], 'x', 1)

    def test_missing_current(self):
        self.assertEqual(step(['a','b'],'x',1),'a')
        self.assertEqual(step(['a','b'],'x',-1),'b')

    def test_invalid_library(self):
        for change in ({'version':2},{'favourites':[]},{'favourites':['x']},
                       {'favourites':[self.data['favourites'][0]]*2},{'channels':self.data['channels']*2}):
            with self.subTest(change=change), self.assertRaises(ValueError): library({**self.data,**change})

    def test_invalid_channel(self):
        for key,value in [('uuid','bad'),('name','bad\nname'),('audio_pid',True),('video','hevc')]:
            with self.subTest(key=key), self.assertRaises(ValueError): Channel.parse({**self.data['channels'][0],key:value})

    def test_credentials_mode(self):
        self.config.credentials.chmod(0o644)
        with self.assertRaises(ValueError): credentials(self.config.credentials)

    def test_combined_credentials_rejected(self):
        atomic_json(self.config.credentials, {'admin':{'username':'x','password':'y'}})
        with self.assertRaises(ValueError): credentials(self.config.credentials)

    def test_no_secrets_in_arguments(self):
        argv=arguments(self.config,self.state.data)
        self.assertNotIn('sensitive-test-password',' '.join(argv))
        self.assertNotIn('Authorization',' '.join(argv))
        self.assertIn('--hwdec=v4l2m2m-copy',argv)
        self.assertIn('--drm-mode=1920x1080@50',argv)
        self.assertIn('--volume-max=100',argv)

    def test_invalid_config(self):
        data=read_json(self.root/'config.json')
        for key,value in [('repeat_delay',float('nan')),('osd_ms',True),('library','relative'),('version',3)]:
            (self.root/'bad.json').write_text(json.dumps({**data,key:value}))
            with self.subTest(key=key), self.assertRaises(ValueError): Config.load(self.root/'bad.json')

    def test_atomic_storage(self):
        p=self.root/'storage.json'
        atomic_json(p, {'name':'Österreich'})
        self.assertEqual(read_json(p),{'name':'Österreich'})
        self.assertEqual(p.stat().st_mode & 0o777,0o600)
        self.assertFalse(list(self.root.glob('.storage.json*')))

    def test_corrupt_state_defaults(self):
        p=self.root/'state.json';p.write_text('{bad')
        state=State(p,self.data['favourites'])
        self.assertEqual(state.data['volume'],35)

    def test_invalid_state_defaults(self):
        p=self.root/'state.json'
        atomic_json(p,{'version':1,'channel':self.data['favourites'][0],'volume':101,'mute':False})
        self.assertEqual(State(p,self.data['favourites']).data['volume'],35)

    def test_literal_control_characters(self):
        self.assertEqual(literal('Ö \u001b{\\b1} ${volume}'),'Ö {\\b1} ${volume}')

    def test_volume_label(self):
        self.assertIn('Muted',volume_label(35,True))
        self.assertIn('100%',volume_label(100,False))


class InputTests(unittest.TestCase):
    def test_press_release_repeat(self):
        p=Policy();self.assertEqual(p.feed('r','remote',402,1,0),'next')
        self.assertIsNone(p.feed('r','remote',402,2,.7))
        self.assertIsNone(p.feed('r','remote',402,0,.8))

    def test_repeat_cap_and_delay(self):
        p=Policy();self.assertEqual(p.feed('k','keyboard',106,1,0),'volume_up')
        self.assertIsNone(p.feed('k','keyboard',106,2,.3))
        self.assertEqual(p.feed('k','keyboard',106,2,.5),'volume_up')
        self.assertIsNone(p.feed('k','keyboard',106,2,.53))
        self.assertEqual(p.feed('k','keyboard',106,2,.64),'volume_up')

    def test_duplicate_nodes(self):
        p=Policy();self.assertEqual(p.feed('k1','keyboard',115,1,0),'volume_up')
        self.assertIsNone(p.feed('k2','keyboard',115,1,.01))
        self.assertEqual(p.feed('k1','keyboard',115,2,.6),'volume_up')
        self.assertIsNone(p.feed('k2','keyboard',115,2,.61))

    def test_bounce(self):
        p=Policy();p.feed('r','remote',113,1,0);p.feed('r','remote',113,0,.01)
        self.assertIsNone(p.feed('r','remote',113,1,.02))

    def test_hotplug_clears_hold(self):
        p=Policy();p.feed('r','remote',115,1,0);p.remove('r')
        self.assertIsNone(p.feed('r','remote',115,2,1))
        self.assertEqual(p.feed('r','remote',115,1,2),'remote_right')

    def test_independent_remote_keyboard(self):
        p=Policy();self.assertEqual(p.feed('k','keyboard',103,1,0),'next')
        self.assertEqual(p.feed('r','remote',402,1,.01),'next')

    def test_mappings(self):
        p=Policy()
        self.assertEqual(p.feed('keyboard','keyboard',113,1,0),'pause')
        for code,action in [(113,'menu'),(164,'pause'),(50,'pause'),(28,'menu'),(358,'info')]:
            self.assertEqual(p.feed(str(code),str(code),code,1,0),action)


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.service={'uuid':'a'*32,'multiplex_uuid':'b'*32,'svcname':'HD','encrypted':False,'enabled':True,'caid':''}
        self.streams=[{'type':'H264','width':0,'height':0},{'type':'MPEG2AUDIO','pid':42,'language':'ger','audio_type':0}]

    def test_fta(self): self.assertEqual(candidate(self.service,self.streams)['audio_pid'],42)

    def test_clean_effects_fallback(self):
        streams=[self.streams[0], {**self.streams[1], 'audio_type':1}]
        self.assertEqual(candidate(self.service,streams)['audio_pid'],42)

    def test_normal_audio_preferred_over_clean_effects(self):
        streams=[self.streams[0], {**self.streams[1], 'audio_type':1, 'pid':43}, self.streams[1]]
        self.assertEqual(candidate(self.service,streams)['audio_pid'],42)

    def test_accessibility_unknown_and_foreign_audio_rejected(self):
        for audio_type in (2, 3, 4, None):
            with self.subTest(audio_type=audio_type):
                self.assertIsNone(candidate(self.service,[self.streams[0],
                    {**self.streams[1], 'audio_type':audio_type}]))
        self.assertIsNone(candidate(self.service,[self.streams[0],
            {**self.streams[1], 'audio_type':1, 'language':'eng'}]))
    def test_encrypted(self): self.assertIsNone(candidate({**self.service,'encrypted':True},self.streams))
    def test_unknown_encryption(self): self.assertIsNone(candidate({**self.service,'encrypted':None},self.streams))
    def test_ca_component(self): self.assertIsNone(candidate(self.service,self.streams+[{'type':'CA'}]))
    def test_uhd(self): self.assertIsNone(candidate(self.service,[{**self.streams[0],'width':3840}]+self.streams[1:]))
    def test_hevc(self): self.assertIsNone(candidate(self.service,[{**self.streams[0],'type':'HEVC'}]+self.streams[1:]))
    def test_no_description(self): self.assertIsNone(candidate(self.service,[self.streams[0],{**self.streams[1],'audio_type':3}]))
    def test_no_radio(self): self.assertIsNone(candidate(self.service,self.streams[1:]))


class ActionTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self): self.setup_files();self.controller=Controller(self.config,self.player,self.state)
    async def asyncTearDown(self): await self.state.close()

    async def test_volume_clamp(self):
        self.player.ipc.get=AsyncMock(side_effect=[99,100,False])
        await self.controller.action('volume_up')
        self.player.ipc.command.assert_any_await('set_property','volume',100)
        self.assertEqual(self.state.data['volume'],100)

    async def test_mute_preserves_volume(self):
        self.player.ipc.get=AsyncMock(side_effect=[35,True])
        await self.controller.action('mute')
        self.assertEqual(self.state.data['volume'],35)
        self.assertTrue(self.state.data['mute'])

    async def test_menu_select(self):
        await self.controller.action('menu')
        await self.controller.action('previous')
        self.assertEqual(self.controller.requested,self.data['favourites'][0])
        await self.controller.action('menu')
        self.assertEqual(self.controller.requested,self.data['favourites'][1])
        self.assertIsNone(self.controller.menu_index)

    async def test_menu_current_channel_does_not_retune(self):
        c = self.controller
        c.playing = c.requested
        c.show_programme = AsyncMock()
        await c.action('menu')
        await c.action('menu')
        self.assertEqual(c.generation, 0)
        self.assertFalse(c.wake.is_set())
        c.show_programme.assert_awaited_once_with(c.channels[c.playing])

    async def test_single_favourite_navigation_does_not_retune(self):
        c = self.controller
        c.favourites = [c.requested]
        c.playing = c.requested
        c.show_programme = AsyncMock()
        await c.action('next')
        self.assertEqual(c.generation, 0)
        c.show_programme.assert_awaited_once()

    async def test_current_channel_can_be_forced_or_recovered(self):
        c = self.controller
        c.playing = c.requested
        self.assertFalse(c.request(c.requested))
        self.assertTrue(c.request(c.requested, force=True))
        c.error = 'Playback unavailable'
        self.assertTrue(c.request(c.requested))
        c.error = None
        self.player.ipc.closed = True
        self.assertTrue(c.request(c.requested))
        self.player.ipc.closed = False
        c.paused = True
        self.assertTrue(c.request(c.requested))

    async def test_return_to_playing_channel_supersedes_pending_request(self):
        c = self.controller
        current = c.requested
        c.playing = current
        self.assertTrue(c.request(c.favourites[1]))
        self.assertTrue(c.request(current))
        self.assertEqual(c.requested, current)
        self.assertEqual(c.generation, 2)

    async def test_changed_audio_policy_forces_current_channel_reload(self):
        c = self.controller
        c.playing = c.requested
        data = {**self.data, 'channels': [dict(item) for item in self.data['channels']]}
        data['channels'][0]['audio_pid'] += 1
        atomic_json(self.config.library, data)
        await c.sync_management()
        self.assertEqual(c.generation, 1)
        self.assertTrue(c.wake.is_set())

    async def test_menu_up_down_direction_and_wrap(self):
        c = self.controller
        await c.action('menu')
        self.assertEqual(c.menu_index, 0)
        await c.action('previous')  # Physical Down.
        self.assertEqual(c.menu_index, 1)
        await c.action('next')  # Physical Up.
        self.assertEqual(c.menu_index, 0)
        await c.action('next')
        self.assertEqual(c.menu_index, len(c.favourites) - 1)
        await c.action('previous')
        self.assertEqual(c.menu_index, 0)
        self.assertEqual(c.requested, self.data['favourites'][0])

    async def test_menu_pages_cover_saved_favourites_in_order(self):
        from dataclasses import replace
        from unittest.mock import patch
        from satip_controller.osd import FAVOURITES_ROWS
        c = self.controller
        template = c.channels[c.requested]
        for count in (1, FAVOURITES_ROWS, 23, 500):
            c.channels = {f"{i:032x}": replace(template, uuid=f"{i:032x}", name=f"Station {i}")
                          for i in range(count)}
            c.favourites = list(c.channels)
            seen = []
            with patch("satip_controller.controller.favourites_panel", return_value="panel") as panel:
                for start in range(0, count, FAVOURITES_ROWS):
                    c.menu_index = start
                    await c.draw_menu()
                    rows, selected, page, pages, language = panel.call_args.args
                    self.assertEqual(language, "en")
                    self.assertEqual(selected, start)
                    self.assertEqual(page, start // FAVOURITES_ROWS + 1)
                    self.assertEqual(pages, (count + FAVOURITES_ROWS - 1) // FAVOURITES_ROWS)
                    self.assertLessEqual(len(rows), FAVOURITES_ROWS)
                    seen.extend(row[1] for row in rows)
            self.assertEqual(seen, c.favourites)

    async def test_menu_cancel(self):
        await self.controller.action('menu');await self.controller.action('next');await self.controller.action('back')
        self.assertEqual(self.controller.requested,self.data['favourites'][0])

    async def test_rapid_last_request_wins(self):
        for _ in range(20): await self.controller.action('next')
        self.assertEqual(self.controller.requested,self.data['favourites'][2])
        self.player.load.assert_not_awaited()

    async def test_osd_three_seconds_escaped(self):
        from satip_controller.osd import escape
        text = 'Ö ${volume} {\\b1}'
        await self.controller.osd.show(text)
        command = self.player.ipc.command.await_args.args[0]
        self.assertEqual(command['name'], 'osd-overlay')
        self.assertIn(escape(text), command['data'])
        self.assertEqual(self.controller.osd.durations[1], 3000)

    async def test_menu_not_overwritten(self):
        await self.controller.action('menu')
        await self.controller.osd.show('old channel')
        self.assertIn('Channels',self.player.ipc.command.call_args.args[0]['data'])

    async def test_management_reload(self):
        data={**self.data,'favourites':self.data['favourites'][1:]}
        atomic_json(self.config.library,data)
        await self.controller.sync_management()
        self.assertEqual(self.controller.requested,self.data['favourites'][1])

    async def test_invalid_library_retained(self):
        atomic_json(self.config.library,{})
        await self.controller.sync_management()
        self.assertEqual(self.controller.favourites,self.data['favourites'])

    async def test_state_shutdown_restore(self):
        self.state.update(volume=45,mute=True)
        await self.state.close()
        restored=State(self.state.path,self.data['favourites'])
        self.assertEqual(restored.data['volume'],45)
        self.assertTrue(restored.data['mute'])
