import functools
import subprocess
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import test_scheduling as fixtures
from bundled_media import bundled_tools
from retrotv.db import apply_migrations, connect_database
from retrotv.iptv import IPTVService, presentation_filter, select_feed
from retrotv.playback import MpvIpcPlayer, PlaybackTarget
from retrotv.scheduling import ScheduleBuilder


class PresentationTests(unittest.TestCase):
    setUp=fixtures.SchedulingTests.setUp
    tearDown=fixtures.SchedulingTests.tearDown

    def test_settings_validation_and_schedule_independence(self):
        builder=ScheduleBuilder(self.connection,self.config);builder.ensure_channel('fox')
        service=IPTVService(self.connection,self.config)
        feed=service.register({'url':'http://lan/live.ts','direct':True,
            'assignments':[{'channel':'fox','scope':'all-day'}]})['id']
        self.assertEqual(service._feed(feed)['presentation'],'preserve')
        before=builder._configuration_fingerprint('fox')
        for mode in ['fill_4_3','stretch_4_3','preserve']:
            service.update(feed,{'presentation':mode,'assignments':[{'channel':'fox','scope':'all-day'}]})
            self.assertEqual(service.overview()['feeds'][0]['presentation'],mode)
            self.assertEqual(builder._configuration_fingerprint('fox'),before)
        with self.assertRaises(ValueError):service.update(feed,{'presentation':'arbitrary filter'})
        self.assertEqual(service._feed(feed)['presentation'],'preserve')
        with self.assertRaises(ValueError):service.register({'url':'http://lan/new.ts','direct':True,'presentation':'bad'})
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM iptv_feeds').fetchone()[0],1)

    def test_old_feed_migrates_without_losing_identity_or_status(self):
        with connect_database(self.temporary/'legacy.sqlite3') as db:
            for migration in sorted((self.project/'migrations').glob('*.sql')):
                if migration.name>='038':break
                db.executescript(migration.read_text(encoding='utf-8'))
            db.execute("INSERT INTO iptv_sources(id,url,kind) VALUES(4,'http://lan/list.m3u','playlist')")
            db.execute("INSERT INTO iptv_feeds(id,source_id,selection_key,name,stream_url,availability) VALUES(7,4,'id:test','Test','http://lan/live.ts','available')")
            db.commit()
            db.executescript((self.project/'migrations/038_iptv_presentation.sql').read_text())
            row=dict(db.execute('SELECT * FROM iptv_feeds WHERE id=7').fetchone())
            self.assertEqual(row['presentation'],'preserve')
            self.assertEqual((row['source_id'],row['selection_key'],row['availability']),(4,'id:test','available'))

    def test_preview_choice_is_part_of_successful_test_identity(self):
        from retrotv.iptv_preview import IPTVPreview
        preview=IPTVPreview(self.config)
        payload={'url':'http://lan/live.ts','direct':True,'presentation':'fill_4_3'}
        preview.selection=select_feed(payload);preview.test_started=time.monotonic()
        preview.state={'test_id':'proof','usable':True}
        self.assertEqual(preview.validated_selection('proof',payload)['presentation'],'fill_4_3')
        with self.assertRaisesRegex(ValueError,'selected feed changed'):
            preview.validated_selection('proof',{**payload,'presentation':'preserve'})

    def test_filter_updates_during_live_playback_without_reconnection(self):
        from test_iptv_playback import LiveControllerTests
        fixture=LiveControllerTests(methodName='test_live_load_never_seeks_and_is_nonblocking')
        fixture.setUp()
        try:
            controller=fixture.controller()
            fixture.player.set_live_filter=lambda value:setattr(fixture.player,'filter',value)
            controller.tune_once('fox');loads=len(fixture.player.loads)
            service=IPTVService(fixture.connection,fixture.config)
            service.update(fixture.target.feed_id,{'presentation':'fill_4_3'})
            controller._poll_live(fixture.target)
            self.assertEqual(fixture.player.filter,presentation_filter('fill_4_3'))
            self.assertEqual(len(fixture.player.loads),loads)
            service.update(fixture.target.feed_id,{'presentation':'preserve'})
            controller._poll_live(fixture.target)
            self.assertIsNone(fixture.player.filter)
        finally:fixture.tearDown()

    def test_real_player_handles_wide_native_and_anamorphic_streams(self):
        tools=bundled_tools(self.project)
        ffmpeg=tools/'ffmpeg/ffmpeg.exe';mpv=tools/'mpv/mpv.exe'
        if not ffmpeg.is_file() or not mpv.is_file():self.skipTest('Bundled media tools unavailable')
        variants=[('wide',320,180,'1/1',240),('native',240,180,'1/1',240),
                  ('anamorphic',720,480,'32/27',540),('anamorphic-native',720,480,'8/9',720)]
        for name,w,h,sar,expected in variants:
            subprocess.run([str(ffmpeg),'-hide_banner','-loglevel','error','-f','lavfi','-i',
                f'testsrc2=s={w}x{h}:r=10','-t','8','-vf',f'setsar={sar}',
                '-c:v','libx264','-preset','ultrafast','-f','mpegts',str(self.temporary/(name+'.ts'))],
                check=True,capture_output=True,timeout=20)
        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Quiet,directory=str(self.temporary)))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        config=replace(self.config,data={**self.config.data,'platform':{'name':'windows'},
            'storage_roots':{**self.config.data['storage_roots'],'state':str(self.temporary/'state')},
            'player':{'mpv':str(mpv),'foreground_on_start':False,'extra_args':[
                '--no-config','--load-scripts=no','--vo=null','--ao=null','--force-window=no']}})
        player=MpvIpcPlayer(config)
        try:
            for name,w,h,sar,expected in variants:
                now=datetime.now().astimezone()
                target=PlaybackTarget(1,None,name,'iptv_live',now,now+timedelta(seconds=30),None,'live',0,30,
                    stream_url=f'http://127.0.0.1:{server.server_port}/{name}.ts',video_filter=presentation_filter('fill_4_3'))
                player.load(target)
                deadline=time.monotonic()+5;params={}
                while time.monotonic()<deadline:
                    if player.live_ready():
                        params=player._command(['get_property','video-out-params']) or {}
                        if params.get('w')==expected:break
                    time.sleep(.05)
                self.assertEqual(params.get('w'),expected,(name,params))
                self.assertAlmostEqual(params['dw']/params['dh'],4/3,delta=.01)
            player.set_live_filter(presentation_filter('stretch_4_3'))
            self.assertIsNone(presentation_filter('preserve'))
            player.set_live_filter(None)
            self.assertFalse(player.live_broken())
            player.load(replace(target,stream_url=f'http://127.0.0.1:{server.server_port}/wide.ts',
                video_filter=presentation_filter('stretch_4_3')))
            for mode,expected_width,expected_ratio in [('stretch_4_3',320,4/3),('preserve',320,16/9),('fill_4_3',240,4/3)]:
                player.set_live_filter(presentation_filter(mode))
                deadline=time.monotonic()+3;params={}
                while time.monotonic()<deadline:
                    if player.live_ready():
                        params=player._command(['get_property','video-out-params']) or {}
                        if params.get('w')==expected_width and abs(params.get('dw',0)/max(params.get('dh',1),1)-expected_ratio)<.01:break
                    time.sleep(.05)
                self.assertEqual(params.get('w'),expected_width,(mode,params))
                self.assertAlmostEqual(params['dw']/params['dh'],expected_ratio,delta=.01)
            # A provider/ad transition can change resolution inside one HLS feed.
            (self.temporary/'changing.m3u8').write_text('#EXTM3U\n#EXT-X-TARGETDURATION:8\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:8,\nwide.ts\n#EXT-X-DISCONTINUITY\n#EXTINF:8,\nnative.ts\n#EXT-X-ENDLIST\n')
            player.load(replace(target,stream_url=f'http://127.0.0.1:{server.server_port}/changing.m3u8'))
            seen=set();deadline=time.monotonic()+13
            while time.monotonic()<deadline and len(seen)<2:
                if player.live_ready():
                    source=player._command(['get_property','video-params']) or {}
                    output=player._command(['get_property','video-out-params']) or {}
                    if source.get('w') in {320,240} and output.get('w')==240:
                        seen.add(source['w'])
                        self.assertAlmostEqual(output['dw']/output['dh'],4/3,delta=.01)
                time.sleep(.05)
            self.assertEqual(seen,{320,240},'Crop must reconfigure when one live feed changes format')
        finally:
            player.stop();server.shutdown();server.server_close();thread.join(timeout=2)
