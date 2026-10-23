import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from unittest.mock import patch

import test_scheduling as fixtures
from retrotv.scheduling import ScheduleBuilder
from retrotv.web import make_handler, INDEX_HTML, INTERNET_MEDIA_HTML
from retrotv.runtime import ApplicationRuntime


class IPTVAPITests(unittest.TestCase):
    setUp=fixtures.SchedulingTests.setUp
    tearDown=fixtures.SchedulingTests.tearDown

    def test_register_edit_preview_and_conflict_status(self):
        ScheduleBuilder(self.connection,self.config).ensure_channel('fox')
        self.config.data['storage_roots']['state']=str(self.temporary/'state')
        runtime=ApplicationRuntime(self.config)
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.config,runtime))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def post(path,body):
            with urlopen(Request(f'http://127.0.0.1:{server.server_port}/api/iptv'+path,
                data=json.dumps(body).encode(),headers={'Content-Type':'application/json'}),timeout=15) as r:
                return json.load(r)
        try:
            with patch.object(runtime,'request_maintenance',return_value=False):
                result=post('',{'url':'http://lan/live.ts','direct':True,'assignments':[{'channel':'fox','scope':'morning'}]})
                feed=result['id'];self.assertFalse(result['queued'])
                assignment=self.connection.execute('SELECT channel_slug,scope FROM iptv_assignments WHERE feed_id=?',(feed,)).fetchone()
                self.assertEqual(tuple(assignment),('fox','morning'))
                self.assertIn('next readiness',result['message'])
                post('/'+str(feed),{'name':'My Live Channel'})
                with patch.object(runtime.iptv_preview,'start',return_value={'state':'connecting'}) as preview:
                    result=post('/'+str(feed)+'/test',{})
                    preview.assert_called_once_with(feed)
                    self.assertEqual(result['state'],'connecting')
                self.assertIsNone(runtime.controller)
                self.add_asset(1,media_type='episode',daypart='morning',duration=1200,series='Local')
                ScheduleBuilder(self.connection,self.config).generate_day('fox',__import__('datetime').date(2027,1,1))
                with urlopen(f'http://127.0.0.1:{server.server_port}/api/iptv') as r: overview=json.load(r)
                self.assertEqual(overview['feeds'][0]['name'],'My Live Channel')
                self.assertTrue(overview['conflicts'])
                with self.assertRaises(HTTPError) as error:
                    post('/'+str(feed),{'assignments':[{'channel':'weather','scope':'all-day'}]})
                self.assertEqual(error.exception.code,400)
                runtime.stop_event.set()
                with self.assertRaises(HTTPError):post('/'+str(feed)+'/test',{})
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)

    add_asset=fixtures.SchedulingTests.add_asset

    def test_ui_has_controls_and_does_not_refresh_away_unsaved_assignments(self):
        self.assertNotIn('id="iptv-panel"',INDEX_HTML)
        self.assertIn('IPTV Live Feeds',INTERNET_MEDIA_HTML)
        self.assertIn('Test selected feed',INTERNET_MEDIA_HTML)
        self.assertIn('Remove assignment',INTERNET_MEDIA_HTML)
        self.assertIn('Shared programs are ignored',INTERNET_MEDIA_HTML)
        self.assertIn('/preview-test',INTERNET_MEDIA_HTML)
        self.assertIn('id="iptv-add" disabled',INTERNET_MEDIA_HTML)

    def test_unsaved_preview_and_server_enforced_creation_gate(self):
        from retrotv.iptv import select_feed
        self.connection.commit()
        self.config.data['storage_roots']['state']=str(self.temporary/'state')
        self.config.storage_root('media').mkdir(exist_ok=True)
        runtime=ApplicationRuntime(self.config)
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.config,runtime))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def post(path,body):
            with urlopen(Request(f'http://127.0.0.1:{server.server_port}/api/iptv'+path,
                data=json.dumps(body).encode(),headers={'Content-Type':'application/json'}),timeout=15) as r:
                return json.load(r)
        payload={'url':'http://lan/live.ts','direct':True,'new_channel':'Preview Channel','scope':'night'}
        try:
            for extra in [{},{'test_id':'invented','usable':True}]:
                with self.assertRaises(HTTPError) as error:post('',{**payload,**extra})
                self.assertEqual(error.exception.code,400)
            self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM iptv_feeds').fetchone()[0],0)
            with patch.object(runtime.iptv_preview,'start_selection',return_value={'state':'connecting','test_id':'ticket'}) as start:
                self.assertEqual(post('/preview-test',payload)['test_id'],'ticket')
                start.assert_called_once_with(payload)
            # Emulate the server-owned proof set only after the player decodes video.
            import time
            runtime.iptv_preview.selection=select_feed(payload)
            runtime.iptv_preview.test_started=time.monotonic()
            runtime.iptv_preview.state={'state':'passed','usable':True,'test_id':'ticket'}
            with patch.object(runtime,'request_maintenance',return_value=False),patch('retrotv.media_folders.ensure_library_scaffold') as scaffold:
                with self.assertRaises(HTTPError):post('',{**payload,'test_id':'ticket','url':'http://different/live.ts'})
                scaffold.assert_not_called()
                result=post('',{**payload,'test_id':'ticket'})
                self.assertIn('id',result)
                scaffold.assert_called_once()
            self.assertIsNotNone(self.connection.execute("SELECT id FROM channels WHERE slug='preview-channel'").fetchone())
            assignment=self.connection.execute("SELECT scope FROM iptv_assignments WHERE channel_slug='preview-channel'").fetchone()
            self.assertEqual(assignment['scope'],'night')
            with urlopen(f'http://127.0.0.1:{server.server_port}/internet-media') as r:
                self.assertIn('id="iptv-panel"',r.read().decode())
            with urlopen(f'http://127.0.0.1:{server.server_port}/library') as r:
                self.assertNotIn('id="iptv-panel"',r.read().decode())
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)
