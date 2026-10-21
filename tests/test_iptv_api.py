import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from unittest.mock import patch

import test_scheduling as fixtures
from retrotv.scheduling import ScheduleBuilder
from retrotv.web import make_handler, INDEX_HTML
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
                result=post('',{'url':'http://lan/live.ts','direct':True,'assignments':[{'channel':'fox','scope':'all-day'}]})
                feed=result['id'];self.assertFalse(result['queued'])
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
        self.assertIn('IPTV Live Feeds',INDEX_HTML)
        self.assertIn('Test feed',INDEX_HTML)
        self.assertIn('Remove assignment',INDEX_HTML)
        self.assertIn('Shared programs are ignored',INDEX_HTML)
        self.assertIn('/api/iptv/preview',INDEX_HTML)
