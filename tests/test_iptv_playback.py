import functools
import json
import subprocess
import threading
import time
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from retrotv.config import AppConfig
from retrotv.iptv import IPTVService, inspect_url
from retrotv.playback import PlaybackTarget, PlaybackController, MpvIpcPlayer, PlaybackError
import test_scheduling as fixtures


class FakePlayer:
    def __init__(self):self.loads=[];self.position=None;self.broken=False;self.ready=False
    def load(self,target):self.loads.append(target)
    def is_running(self):return True
    def playback_position(self):return self.position
    def live_broken(self):return self.broken
    def live_ready(self):return self.ready


class LiveControllerTests(unittest.TestCase):
    setUp=fixtures.SchedulingTests.setUp
    tearDown=fixtures.SchedulingTests.tearDown

    def controller(self):
        from retrotv.scheduling import ScheduleBuilder
        ScheduleBuilder(self.connection,self.config).ensure_channel('fox')
        feed=IPTVService(self.connection,self.config).register({'direct':True,'url':'http://lan/live.ts',
            'assignments':[{'channel':'fox','scope':'all-day'}]})['id']
        now=datetime.now().astimezone()
        self.target=PlaybackTarget(1,None,'Live','iptv_live',now,now+timedelta(hours=2),None,'iptv_live',0,7200,'fox',stream_url='http://lan/live.ts',feed_id=feed)
        self.player=FakePlayer()
        self.resolver=SimpleNamespace(connection=self.connection,config=self.config,
            scheduler=SimpleNamespace(timezone=now.tzinfo),resolve=lambda *args:self.target)
        self.c=PlaybackController(self.resolver,self.player)
        self.c._refresh_live=lambda *args,**kwargs:None
        return self.c

    def test_connect_stall_continuity_retry_and_recovery(self):
        c=self.controller()
        slate=self.temporary/'slate.mp4';slate.write_bytes(b'fixture')
        clock=[100.]
        with patch('retrotv.playback.time_module.monotonic',side_effect=lambda:clock[0]),patch('retrotv.playback.ensure_continuity_slate',return_value=slate):
            c.tune_once('fox')
            self.assertEqual(c._live_phase,'connecting')
            clock[0]=116
            c._poll_live(self.target)
            self.assertEqual(self.player.loads[-1].entry_type,'channel_fallback')
            self.assertEqual(c._live_retry_at,121)
            clock[0]=121;c._poll_live(self.target)
            self.assertEqual(self.player.loads[-1].stream_url,'http://lan/live.ts')
            self.player.ready=True;self.player.position=3
            c._poll_live(self.target)
            self.assertEqual(c._live_phase,'playing')
            self.assertEqual(IPTVService(self.connection,self.config)._feed(self.target.feed_id)['availability'],'available')
            clock[0]=137;c._poll_live(self.target)
            self.assertEqual(c._live_retry_at,152)

    def test_same_feed_continues_but_changed_channel_and_local_load(self):
        c=self.controller();c.tune_once('fox')
        initial=len(self.player.loads)
        self.target=replace(self.target,entry_id=2,starts_at=self.target.ends_at,ends_at=self.target.ends_at+timedelta(hours=2))
        c.tune_once('fox',continue_synthetic=True)
        self.assertEqual(len(self.player.loads),initial)
        self.target=replace(self.target,channel_slug='pbs')
        c.tune_once('pbs')
        self.assertEqual(len(self.player.loads),initial+1)
        self.target=replace(self.target,path=self.temporary/'program.mp4',entry_type='episode',stream_url=None,feed_id=None)
        c.tune_once('fox')
        self.assertEqual(self.player.loads[-1].entry_type,'episode')

    def test_removed_selection_and_repeated_failures_do_not_stop_tv(self):
        c=self.controller();slate=self.temporary/'slate.mp4';slate.write_bytes(b'fixture')
        c.tune_once('fox')
        IPTVService(self.connection,self.config).status(self.target.feed_id,'missing','Stream not found')
        with patch('retrotv.playback.ensure_continuity_slate',return_value=slate):
            c._poll_live(self.target)
            self.assertEqual(c._live_phase,'continuity')
            for _ in range(6):c._begin_live(self.target)
        self.assertFalse(c.stop_event.is_set())
        self.assertEqual(IPTVService(self.connection,self.config)._feed(self.target.feed_id)['detail'],'Stream not found')

    def test_live_load_never_seeks_and_is_nonblocking(self):
        c=self.controller()
        p=MpvIpcPlayer(self.config)
        commands=[]
        with patch.object(p,'start'),patch.object(p,'_remember_track_choices'),patch.object(p,'_command',side_effect=lambda cmd,**kw:commands.append(cmd)):
            p.load(self.target)
        self.assertTrue(any(cmd[0]=='loadfile' and cmd[1]=='http://lan/live.ts' for cmd in commands))
        self.assertFalse(any(cmd[0]=='seek' for cmd in commands))


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self,*args):pass


class RealLivePlaybackTests(unittest.TestCase):
    setUp=fixtures.SchedulingTests.setUp
    tearDown=fixtures.SchedulingTests.tearDown

    def test_bundled_mpv_decodes_http_hls_and_transport_stream(self):
        bundle=self.project/'dist/release-candidate-0.9.55/CRTV-Sim-Portable-0.9.55/tools'
        mpv=bundle/'mpv/mpv.exe';ffmpeg=bundle/'ffmpeg/ffmpeg.exe'
        if not mpv.is_file() or not ffmpeg.is_file():
            self.skipTest('Bundled Windows media tools not available')
        completed=subprocess.run([str(ffmpeg),'-hide_banner','-loglevel','error','-y',
            '-f','lavfi','-i','testsrc2=size=160x120:rate=10','-f','lavfi','-i','sine=frequency=440',
            '-t','10','-c:v','libx264','-preset','ultrafast','-g','10','-c:a','aac',
            '-f','hls','-hls_time','1','-hls_list_size','0',str(self.temporary/'live.m3u8')],
            capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(completed.returncode,0,completed.stderr.decode(errors='replace'))
        # A live manifest has no ENDLIST; mpv chooses its live edge itself.
        manifest=self.temporary/'live.m3u8'
        manifest.write_text(manifest.read_text().replace('#EXT-X-ENDLIST',''))
        server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(QuietHandler,directory=str(self.temporary)))
        server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        config=replace(self.config,data={**self.config.data,'storage_roots':{**self.config.data['storage_roots'],'state':str(self.temporary/'state')},'platform':{'name':'windows'},'player':{
            'mpv':str(mpv),'fullscreen':False,'foreground_on_start':False,'extra_args':['--no-config','--load-scripts=no','--vo=null','--ao=null','--force-window=no']}})
        try:
            for name in ['live.m3u8','live0.ts']:
                url=f'http://127.0.0.1:{server.server_port}/{name}'
                self.assertEqual(inspect_url(url)['kind'],'direct')
                player=MpvIpcPlayer(config,executable=str(mpv))
                now=datetime.now().astimezone()
                target=PlaybackTarget(-4,None,'Test','iptv_live',now,now+timedelta(seconds=30),None,'iptv_live',0,30,stream_url=url,feed_id=1)
                try:
                    player.load(target)
                    deadline=time.monotonic()+12
                    decoded=False
                    while time.monotonic()<deadline and not player.live_broken():
                        if player.live_ready():
                            tracks=player._command(['get_property','track-list'])
                            if any(t['type']=='video' for t in tracks) and any(t['type']=='audio' for t in tracks):
                                decoded=player.playback_position() is not None
                                if decoded:break
                        time.sleep(.1)
                    self.assertTrue(decoded,f'{name} did not decode through live-loading path')
                finally:player.stop()
            # Exercise the independent setup-time preview, not just its player.
            from retrotv.iptv_preview import IPTVPreview
            feed=IPTVService(self.connection,config).register({'direct':True,'url':f'http://127.0.0.1:{server.server_port}/live.m3u8'})['id']
            preview=IPTVPreview(config)
            preview.start(feed)
            deadline=time.monotonic()+12
            while time.monotonic()<deadline and preview.snapshot()['state']=='connecting':
                time.sleep(.1)
            self.assertEqual(preview.snapshot()['state'],'playing',preview.snapshot())
            preview.stop()
            self.assertFalse(preview.thread.is_alive())
            self.assertIsNone(preview.player)

            player=MpvIpcPlayer(config,executable=str(mpv))
            try:
                player.load(replace(target,stream_url=f'http://127.0.0.1:{server.server_port}/missing.ts'))
                deadline=time.monotonic()+5
                while not player.live_broken() and time.monotonic()<deadline:
                    time.sleep(.1)
                self.assertTrue(player.live_broken(),'HTTP 404 must be reported as a live-load failure')
            finally:player.stop()
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)
