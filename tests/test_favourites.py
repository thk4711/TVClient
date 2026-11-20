import unittest
from unittest.mock import AsyncMock

from satip_controller.backend import BackendError
from satip_controller.scan import Manager
from satip_controller.state import read_json
from tests.helpers import Fixture


class FavouriteTests(Fixture,unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.setup_files()
        self.backend=AsyncMock()
        self.manager=Manager(self.backend,self.web_config)
        self.items=[{k:v for k,v in c.items() if k!='uuid'} for c in self.data['channels']]
        self.new={'service_uuid':'f'*32,'mux_uuid':'c'*32,'name':'New FTA', 'video':'h264','audio_pid':42}
        self.items.append(self.new)
        self.manager.candidates=AsyncMock(return_value=self.items)
        self.backend_channels=[{'uuid':c['uuid'],'name':c['name'],'number':(i+1)*1000000,
            'enabled':True,'epgauto':True,'services':[c['service_uuid']]} for i,c in enumerate(self.data['channels'])]
        self.acl={'uuid':'b'*32,'admin':False,'webui':False,'conn_limit':1,
                  'channel_min':1000000,'channel_max':3000000}
        async def grid(endpoint):
            if endpoint=='channel/grid': return self.backend_channels.copy()
            if endpoint=='access/entry/grid': return [self.acl.copy()]
            if endpoint=='mpegts/mux/grid':
                return [{'uuid':c['mux_uuid'],'enabled':1,'epg':1} for c in self.data['channels']]+[
                    {'uuid':'c'*32,'enabled':0,'epg':0}]
            return []
        self.backend.grid.side_effect=grid
        self.backend.request.return_value={'uuid':'e'*32}

    async def asyncTearDown(self): await self.state.close()

    async def test_reorder_only_revalidates_selected_services(self):
        selected=[self.items[0]['service_uuid']]
        await self.manager.save_favourites(selected)
        self.manager.candidates.assert_awaited_once_with(service_ids=set(selected))
        self.backend.save.assert_not_awaited()

    async def test_expansion_revalidates_all_potentially_authorized_mappings(self):
        await self.manager.save_favourites([self.new['service_uuid']])
        expected={self.new['service_uuid']} | {c['service_uuid'] for c in self.data['channels']}
        self.manager.candidates.assert_awaited_once_with(service_ids=expected)

    async def test_reorder_preserves_uuids(self):
        services=[c['service_uuid'] for c in reversed(self.data['channels'])]
        result=await self.manager.save_favourites(services)
        self.assertEqual(result['favourites'],list(reversed(self.data['favourites'])))
        self.backend.request.assert_not_awaited()
        self.backend.save.assert_not_awaited()

    async def test_new_fta_maps_and_widens_only_range(self):
        result=await self.manager.save_favourites([self.new['service_uuid'],self.items[0]['service_uuid']])
        self.assertEqual(result['favourites'][0],'e'*32)
        self.backend.save.assert_any_await(uuid='c'*32,enabled=1,epg=1)
        self.backend.save.assert_any_await(uuid='b'*32,channel_max=4000000)
        self.assertTrue(list(self.manager.directory.glob('favourites-backup-*.json')))

    async def test_failed_backend_keeps_old_library(self):
        self.backend.request.side_effect=BackendError('redacted')
        with self.assertRaises(BackendError): await self.manager.save_favourites([self.new['service_uuid']])
        self.assertEqual(read_json(self.config.library),self.data)

    async def test_unknown_mapping_prevents_acl_expansion(self):
        self.backend_channels.append({'uuid':'d'*32,'number':4000000,'enabled':True,'services':['0'*32]})
        with self.assertRaises(BackendError): await self.manager.save_favourites([self.new['service_uuid']])
        self.backend.request.assert_not_awaited()

    async def test_invalid_name_rejected_before_mapping(self):
        self.new['name']='bad\nchannel'
        with self.assertRaises(ValueError): await self.manager.save_favourites([self.new['service_uuid']])
        self.backend.request.assert_not_awaited()

    async def test_empty_favourites_rejected(self):
        with self.assertRaises(ValueError): await self.manager.save_favourites([])

    async def test_existing_channel_epg_is_enabled_without_acl_change(self):
        self.backend_channels[0]['epgauto']=False
        original=self.backend.grid.side_effect
        async def grid(endpoint):
            rows=await original(endpoint)
            if endpoint=='mpegts/mux/grid':
                rows[0]['epg']=0
            return rows
        self.backend.grid.side_effect=grid
        await self.manager.save_favourites([self.items[0]['service_uuid']])
        self.backend.save.assert_any_await(uuid=self.data['channels'][0]['uuid'],epgauto=True)
        self.backend.save.assert_any_await(uuid=self.data['channels'][0]['mux_uuid'],enabled=1,epg=1)
        self.backend.request.assert_not_awaited()
