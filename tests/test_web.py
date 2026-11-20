import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from tests.helpers import Fixture
from fastapi.testclient import TestClient
from satip_controller.web import create_app
from satip_controller.state import library_revision


class WebTests(Fixture, unittest.TestCase):
    def setUp(self):
        self.setup_files()
        self.backend=AsyncMock()
        self.client=TestClient(create_app(self.web_config,self.backend))
        self.addCleanup(self.client.close)
        self.auth=('web','private-web-password')

    def test_login_required(self): self.assertEqual(self.client.get('/api/channels').status_code,401)

    def test_health_endpoint_is_loopback_only_without_weakening_login(self):
        self.assertEqual(self.client.get("/healthz").status_code, 404)
        local = TestClient(create_app(self.web_config, self.backend), client=("127.0.0.1", 50000))
        self.addCleanup(local.close)
        self.assertEqual(local.get("/healthz").status_code, 200)
        self.assertEqual(local.get("/api/channels").status_code, 401)
        remote = TestClient(create_app(self.web_config, self.backend), client=("192.168.1.2", 50000))
        self.addCleanup(remote.close)
        self.assertEqual(remote.get("/healthz", headers={"X-Forwarded-For": "127.0.0.1"}).status_code, 404)

    def test_settings_security_validation_and_persistence(self):
        from satip_controller.settings import load_settings, settings_path
        from stat import S_IMODE
        self.assertEqual(self.client.get('/api/settings').status_code, 401)
        self.assertEqual(self.client.get('/api/settings', auth=self.auth).json(),
                         {'version': 1, 'osd_timeout_ms': 5000, 'osd_language': 'en'})
        self.assertEqual(self.client.put('/api/settings', auth=self.auth,
                         json={'osd_timeout_ms': 8000}).status_code, 403)
        headers = {'x-satip-action': '1'}
        for value in (499, 30001, True, '8000', 8000.0):
            self.assertEqual(self.client.put('/api/settings', auth=self.auth, headers=headers,
                             json={'osd_timeout_ms': value}).status_code, 422)
        self.assertEqual(self.client.put('/api/settings', auth=self.auth, headers=headers,
                         json={'osd_timeout_ms': 8000, 'other': 1}).status_code, 422)
        result = self.client.put('/api/settings', auth=self.auth, headers=headers,
                                 json={'osd_timeout_ms': 8000})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(load_settings(self.config.management_dir), result.json())
        self.assertEqual(S_IMODE(settings_path(self.config.management_dir).stat().st_mode), 0o640)
        self.assertEqual(self.client.get('/api/settings', auth=self.auth).json(), result.json())

    def test_network_status_is_read_only_and_old_setting_is_removed(self):
        self.assertEqual(self.client.get('/api/network').status_code, 401)
        result = self.client.get('/api/network', auth=self.auth)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['mode'], 'dhcp')
        headers = {'x-satip-action': '1'}
        self.assertEqual(self.client.put('/api/network', auth=self.auth, headers=headers,
                                        json={}).status_code, 405)
        self.assertEqual(self.client.put('/api/settings/network', auth=self.auth, headers=headers,
                                        json={'optimistic_reuse': True, 'confirm_risk': True}).status_code, 404)
        self.assertEqual(self.client.get('/api/settings/network', auth=self.auth).status_code, 404)
        self.assertNotIn('Optimistic address reuse', self.client.get('/', auth=self.auth).text)

    def test_youtube_auth_actions_validation_and_private_status(self):
        from satip_controller.youtube import preferences
        self.assertEqual(self.client.get('/api/youtube').status_code, 401)
        self.assertEqual(self.client.post('/api/youtube', auth=self.auth, json={'action': 'pair'}).status_code, 403)
        headers = {'x-satip-action': '1'}
        for data in ({'action': 'play'}, {'action': 'pair', 'url': 'https://evil.test'}, {'action': True}):
            self.assertEqual(self.client.post('/api/youtube', auth=self.auth, headers=headers, json=data).status_code, 422)
        self.assertEqual(self.client.get('/youtube.js').status_code, 401)
        self.assertEqual(self.client.get('/youtube.js', auth=self.auth).status_code, 200)
        self.assertEqual(self.client.post('/api/youtube', auth=self.auth, headers=headers, json={'action': 'pair'}).status_code, 202)
        data = preferences(self.config.management_dir)
        self.assertTrue(data['enabled'])
        self.assertEqual(len(data['pair_request']), 32)
        self.assertEqual(self.client.post('/api/youtube', auth=self.auth, headers=headers, json={'action': 'unlink'}).status_code, 202)
        data = preferences(self.config.management_dir)
        self.assertFalse(data['enabled'])
        self.assertEqual(len(data['unlink_request']), 32)
        self.assertNotIn('screen_id', self.client.get('/api/youtube', auth=self.auth).json())

    def test_qr_encoder_is_local_authenticated_and_no_store(self):
        self.assertEqual(self.client.get('/qrcode.js').status_code, 401)
        response = self.client.get('/qrcode.js', auth=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/javascript', response.headers['content-type'])
        self.assertEqual(response.headers['cache-control'], 'no-store')
        html = self.client.get('/', auth=self.auth).text
        self.assertLess(html.index('/qrcode.js'), html.index('/youtube.js'))
        self.assertIn('id="youtube-qr" hidden', html)
        self.assertEqual(self.client.get('/qrcode-LICENSE.txt', auth=self.auth).status_code, 404)

    def test_preview_security_and_confirmation(self):
        self.assertEqual(self.client.get('/api/preview/'+'a'*32+'/index.m3u8').status_code,401)
        self.assertEqual(self.client.get('/api/preview/'+'a'*32+'/segment-000001.ts').status_code,401)
        self.assertEqual(self.client.post('/api/preview', auth=self.auth, json={}).status_code,403)
        headers={'x-satip-action':'1'}
        self.assertEqual(self.client.post('/api/preview', auth=self.auth,
            json={'service':'a'*32}, headers=headers).status_code,400)
        self.assertEqual(self.client.post('/api/preview/heartbeat', auth=self.auth,
            json={'session':'a'*32}, headers=headers).status_code,410)

    def test_preview_status_and_local_library(self):
        response=self.client.get('/api/status',auth=self.auth)
        self.assertFalse(response.json()['preview']['enabled'])
        self.assertNotIn('session',response.json()['preview'])
        response=self.client.get('/hls.min.js',auth=self.auth)
        self.assertEqual(response.status_code,200)
        self.assertIn('text/javascript',response.headers['content-type'])
        self.assertIn("media-src 'self' blob:",response.headers['content-security-policy'])

    def test_preview_uses_restricted_credentials_and_blocks_scan(self):
        config=dict(self.web_config,preview_credentials=str(self.config.credentials))
        app=create_app(config,self.backend)
        async def quiet():
            import asyncio
            await asyncio.sleep(30)
        app.state.preview.quiet=quiet
        with TestClient(app) as client:
            headers={'x-satip-action':'1'}
            result=client.post('/api/preview',auth=self.auth,headers=headers,
                json={'service':self.data['channels'][0]['service_uuid'],'confirm_pause':True})
            self.assertEqual(result.status_code,202)
            token=result.json()['session']
            self.assertEqual(client.post('/api/scan',auth=self.auth,headers=headers,
                json={'confirm_pause':True}).status_code,409)
            self.assertEqual(client.put('/api/favourites',auth=self.auth,headers=headers,
                json={'services':['a'*32]}).status_code,409)
            self.assertEqual(client.post('/api/preview/stop',auth=self.auth,headers=headers,
                json={'session':'f'*32}).status_code,409)
            self.assertEqual(client.post('/api/preview/stop',auth=self.auth,headers=headers,
                json={'session':token}).status_code,200)
        self.assertFalse(app.state.manager.lock.locked())
    def test_bad_login(self): self.assertEqual(self.client.get('/',auth=('web','wrong')).status_code,401)
    def test_ui(self):
        r=self.client.get('/',auth=self.auth)
        self.assertEqual(r.status_code,200);self.assertIn('Save favourites',r.text)
        self.assertEqual(r.headers['cache-control'],'no-store')
        self.assertIn('scan-progress',r.text)
        self.assertNotIn('JSON.stringify',r.text)

    def test_ui_script(self):
        r=self.client.get('/ui.js',auth=self.auth)
        self.assertEqual(r.status_code,200)
        self.assertIn('text/javascript',r.headers['content-type'])
        self.assertEqual(r.headers['cache-control'],'no-store')

    def test_dashboard_assets_and_telemetry_follow_auth(self):
        self.backend.grid.return_value = []
        for path in ('/app.css', '/app.js', '/api/telemetry'):
            self.assertEqual(self.client.get(path).status_code, 401)
            result = self.client.get(path, auth=self.auth)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.headers['cache-control'], 'no-store')
        data = self.client.get('/api/telemetry', auth=self.auth).json()
        self.assertEqual(data['version'], 1)
        self.assertEqual(data['reception']['activity'], 'idle')
        self.assertNotIn('private-admin-password', str(data))

    def test_static_query_parameters_cannot_override_files_or_media_types(self):
        static_root = Path(__file__).parents[1] / 'satip_controller/static'
        assets = {'/': ('index.html', 'text/html'),
                  **{f'/{name}': (name, 'text/javascript') for name in
                     ('request.js', 'ui.js', 'hls.min.js', 'app.js',
                      'youtube.js', 'qrcode.js', 'device.js')},
                  '/app.css': ('app.css', 'text/css')}
        # Use harmless existing files outside the allowlist. Traversal escapes
        # static_root; an absolute path would also discard that root entirely.
        queries = ({'name': str(Path(__file__).resolve())},
                   {'name': '../../tests/test_web.py'},
                   {'media': 'application/x-static-override'},
                   {'name': '../../tests/test_web.py', 'media': 'application/x-static-override'})
        for client, auth in ((self.client, self.auth), (self.lan_client(), None)):
            for route, (filename, media_type) in assets.items():
                expected = (static_root / filename).read_bytes()
                for params in queries:
                    with self.subTest(authenticated=auth is not None, route=route, params=params):
                        if auth is not None:
                            self.assertEqual(client.get(route, params=params).status_code, 401)
                        response = client.get(route, params=params, auth=auth)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.content, expected)
                        self.assertEqual(response.headers['content-type'].split(';')[0], media_type)
                        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_friendly_channel_name_and_revision(self):
        from satip_controller.state import atomic_json
        atomic_json(self.config.state_dir/'status.json',{'playing':self.data['favourites'][0],'error':None})
        r=self.client.get('/api/status',auth=self.auth)
        self.assertEqual(r.json()['player']['channel_name'],'Das Erste HD')
        self.assertEqual(r.json()['catalogue_revision'],0)
    def test_channels(self):
        r=self.client.get('/api/channels',auth=self.auth)
        self.assertEqual(len(r.json()['favourites']),3)
        self.assertNotIn('private-admin-password',r.text)
    def test_saved_channels_survive_partial_or_stale_catalogue(self):
        from pathlib import Path
        from satip_controller.state import atomic_json
        first = self.data["channels"][0]
        extra = {"service_uuid": "f" * 32, "name": "New station"}
        atomic_json(Path(self.web_config["private_dir"]) / "candidates.json", {"channels": [
            {"service_uuid": first["service_uuid"], "name": "Stale provider name", "encrypted": False}, extra]})
        response = self.client.get("/api/channels", auth=self.auth)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        names = {c["service_uuid"]: c["name"] for c in data["channels"]}
        channels = {c["uuid"]: c for c in self.data["channels"]}
        expected = [channels[c] for c in self.data["favourites"]]
        self.assertEqual(data["favourites"], [c["service_uuid"] for c in expected])
        self.assertEqual([names[c] for c in data["favourites"]], [c["name"] for c in expected])
        self.assertEqual(names[extra["service_uuid"]], extra["name"])
        self.assertFalse(data["channels"][0]["encrypted"])
        self.assertEqual(len(names), len(data["channels"]))

    def test_csrf_header(self):
        r=self.client.post('/api/scan',auth=self.auth,json={'confirm_pause':True})
        self.assertEqual(r.status_code,403)
    def test_cross_origin(self):
        r=self.client.post('/api/scan',auth=self.auth,json={'confirm_pause':True},
            headers={'x-satip-action':'1','origin':'http://evil.example'})
        self.assertEqual(r.status_code,403)
    def test_confirm_required(self):
        r=self.client.post('/api/scan',auth=self.auth,json={},headers={'x-satip-action':'1'})
        self.assertEqual(r.status_code,400)
    def test_save_acknowledgement_is_published_only_after_success(self):
        app=self.client.app
        app.state.manager.save_favourites=AsyncMock(return_value=self.data)
        headers={'x-satip-action':'1'}
        token='c'*32
        result=self.client.put('/api/favourites',auth=self.auth,headers=headers,
            json={'services':[self.data['channels'][0]['service_uuid']], 'save_id':token, 'base_revision':library_revision(self.data)})
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()['save_id'],token)
        status=self.client.get('/api/status',auth=self.auth).json()
        self.assertEqual(status['last_favourites_save_id'],token)
        self.assertEqual(status['catalogue_revision'],1)
        from satip_controller.backend import BackendError
        app.state.manager.save_favourites=AsyncMock(side_effect=BackendError('failed'))
        result=self.client.put('/api/favourites',auth=self.auth,headers=headers,
            json={'services':[self.data['channels'][0]['service_uuid']], 'save_id':'d'*32, 'base_revision':library_revision(self.data)})
        self.assertEqual(result.status_code,503)
        status=self.client.get('/api/status',auth=self.auth).json()
        self.assertEqual(status['last_favourites_save_id'],token)
        self.assertEqual(status['catalogue_revision'],1)

    def test_invalid_save_id_rejected(self):
        result=self.client.put('/api/favourites',auth=self.auth,headers={'x-satip-action':'1'},
            json={'services':['a'*32], 'save_id':'invalid'})
        self.assertEqual(result.status_code,422)
        self.assertIsNone(self.client.get('/api/status',auth=self.auth).json()['last_favourites_save_id'])

    def test_empty_favourites(self):
        r=self.client.put('/api/favourites',auth=self.auth,json={'services':[]},headers={'x-satip-action':'1'})
        self.assertEqual(r.status_code,422)
    def test_invalid_service_uuid(self):
        r=self.client.put('/api/favourites',auth=self.auth,json={'services':['invalid']},headers={'x-satip-action':'1'})
        self.assertEqual(r.status_code,422)
    def test_host_restriction(self):
        r=self.client.get('/',auth=self.auth,headers={'host':'evil.example'})
        self.assertEqual(r.status_code,400)

    def lan_client(self):
        config=dict(self.web_config, listen_host='0.0.0.0', listen_port=8080,
                    require_auth=False, allowed_hosts=['192.0.2.10','satip','satip.local'])
        client=TestClient(create_app(config,self.backend),base_url='http://192.0.2.10:8080')
        self.addCleanup(client.close)
        return client

    def test_lan_without_login(self):
        client=self.lan_client()
        self.assertEqual(client.get('/').status_code,200)
        self.assertEqual(client.get('/api/channels').status_code,200)
        self.assertEqual(client.get('/',headers={'host':'satip.local:8080'}).status_code,200)
        self.assertEqual(client.get('/',headers={'host':'evil.example'}).status_code,400)

    def test_lan_action_protections(self):
        client=self.lan_client()
        self.assertEqual(client.post('/api/scan',json={}).status_code,403)
        headers={'x-satip-action':'1','origin':'http://evil.example'}
        self.assertEqual(client.post('/api/scan',json={},headers=headers).status_code,403)
        headers['origin']='http://192.0.2.10:8080'
        self.assertEqual(client.post('/api/scan',json={},headers=headers).status_code,400)

    def test_no_login_file_needed_when_auth_disabled(self):
        config=dict(self.web_config, require_auth=False, web_credentials='/missing/login.json')
        client=TestClient(create_app(config,self.backend))
        self.addCleanup(client.close)
        self.assertEqual(client.get('/').status_code,200)

    def test_invalid_network_config(self):
        for settings in ({'require_auth':'false'}, {'listen_port':0},
                         {'listen_port':True}, {'listen_host':''}, {'allowed_hosts':[]},
                         {'allowed_hosts':['localhost',123]}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                create_app(dict(self.web_config, **settings),self.backend)
