import threading
import time
import unittest
import struct
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import test_scheduling as fixtures
from bundled_media import bundled_tools
from retrotv.iptv_cover import LiveLoadingCover
from retrotv.playback import MpvIpcPlayer, PlaybackTarget, PlaybackError


class LoadingCoverTests(unittest.TestCase):
    setUp=fixtures.SchedulingTests.setUp
    tearDown=fixtures.SchedulingTests.tearDown

    def test_channel_art_is_prepared_opaque_cached_and_never_modifies_original(self):
        from dataclasses import replace
        def png(color):
            def chunk(name, data):
                return struct.pack('>I',len(data))+name+data+struct.pack('>I',zlib.crc32(name+data))
            return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',160,120,8,6,0,0,0))
                    +chunk(b'IDAT',zlib.compress((b'\0'+bytes(color)*160)*120))+chunk(b'IEND',b''))
        ffmpeg=bundled_tools(self.project)/'ffmpeg/ffmpeg.exe'
        if not ffmpeg.is_file():self.skipTest('Bundled FFmpeg unavailable')
        source=self.temporary/'media/channel/graphics/interstitials/art.png'
        source.parent.mkdir(parents=True)
        source.write_bytes(png((250,30,70,128)))
        original=source.read_bytes()
        config=replace(self.config,data={**self.config.data,
            'tools':{'ffmpeg':str(ffmpeg)},'storage_roots':{**self.config.data['storage_roots'],'state':str(self.temporary/'state')}})
        self.connection.commit()
        loader=LiveLoadingCover(config)
        when=datetime.now().astimezone()
        try:
            with patch('retrotv.graphics.GraphicsService.select_asset',return_value={'relative_path':'channel/graphics/interstitials/art.png'}) as select:
                frame=loader._prepare('channel',when,threading.Event())
                select.assert_called_with('channel','interstitials',when)
                data=frame.read_bytes()
                self.assertEqual(len(data),640*480*4)
                self.assertEqual(set(data[3::4]),{255})
                self.assertEqual(source.read_bytes(),original)
                with patch('retrotv.iptv_cover.subprocess.Popen',side_effect=AssertionError('cached art must not be re-encoded')):
                    self.assertEqual(loader._prepare('channel',when,threading.Event()),frame)
                source.write_bytes(png((0,0,255,255)))
                self.assertNotEqual(loader._prepare('channel',when,threading.Event()),frame)
        finally:loader.close()
        self.assertFalse(loader.thread.is_alive())

    def test_latest_channel_wins_and_cancellation_prevents_stale_art(self):
        loader=LiveLoadingCover(self.config)
        entered=threading.Event();finished=threading.Event()
        def prepare(channel,when,cancel):
            if channel=='first':
                entered.set();cancel.wait(2)
            else:finished.set()
            return Path(channel)
        try:
            with patch.object(loader,'_prepare',side_effect=prepare):
                loader.request('first',datetime.now().astimezone())
                self.assertTrue(entered.wait(2))
                loader.request('second',datetime.now().astimezone())
                self.assertTrue(finished.wait(2))
                deadline=time.monotonic()+2
                while loader.snapshot()!=Path('second') and time.monotonic()<deadline:time.sleep(.01)
                self.assertEqual(loader.snapshot(),Path('second'))
                loader.cancel();self.assertIsNone(loader.snapshot())
        finally:loader.close()

    def test_missing_art_and_failed_decode_leave_fallback_available(self):
        self.connection.commit();loader=LiveLoadingCover(self.config)
        try:
            with patch('retrotv.graphics.GraphicsService.select_asset',return_value=None):
                self.assertIsNone(loader._prepare('channel',datetime.now().astimezone(),threading.Event()))
        finally:loader.close()

    def test_bitmap_covers_frame_resizes_and_clears_only_after_decoded_video(self):
        player=MpvIpcPlayer(self.config)
        loader=Mock();loader.snapshot.return_value=Path('cover.bgra')
        player._live_cover_service=loader;player._live_cover_visible=True
        dimensions={'w':1280,'h':720};video=[None]
        def command(cmd,**kwargs):
            if cmd[:2]==['get_property','osd-dimensions']:return dimensions
            if cmd[:2]==['get_property','video-params']:return video[0]
            if cmd[:2]==['get_property','time-pos']:return 1
        with patch.object(player,'_command',side_effect=command) as ipc:
            player.show_text('CH 10  CHANNEL')
            self.assertFalse(any(c.args[0][0]=='show-text' for c in ipc.call_args_list))
            self.assertFalse(player.live_ready())
            add=next(c.args[0] for c in ipc.call_args_list if c.args[0][0]=='overlay-add')
            self.assertEqual(add[2:4],[160,0]);self.assertEqual(add[-2:],[960,720])
            player.file_loaded.set()
            self.assertFalse(player.live_ready())
            self.assertTrue(player._live_cover_visible)
            dimensions.update(w=640,h=480)
            player.live_ready()
            self.assertEqual([c.args[0] for c in ipc.call_args_list if c.args[0][0]=='overlay-add'][-1][-2:],[640,480])
            video[0]={'w':640,'h':480}
            self.assertTrue(player.live_ready())
            self.assertFalse(player._live_cover_visible)
            self.assertIn(['overlay-remove',63],[c.args[0] for c in ipc.call_args_list])
            self.assertEqual(ipc.call_args.args[0],['show-text','CH 10  CHANNEL',2000])

    def test_fallback_bars_are_separate_opaque_events_and_waiting_keeps_art(self):
        player=MpvIpcPlayer(self.config);player._live_cover_visible=True
        with patch.object(player,'_command',return_value={'w':640,'h':480}) as ipc:
            player._update_live_cover()
            cover=ipc.call_args.args[0][3]
            self.assertEqual(len(cover.splitlines()),7)
            self.assertTrue(all(r'\1a&H00&' in line for line in cover.splitlines()))
            loader=Mock(channel='channel');loader.snapshot.return_value=Path('art.bgra')
            player._live_cover_service=loader
            now=datetime.now().astimezone()
            player.show_live_waiting(PlaybackTarget(1,None,'Live','iptv_live',now,now+timedelta(minutes=1),None,'live',0,60,'channel'))
            self.assertIn(['stop'],[c.args[0] for c in ipc.call_args_list])
            self.assertTrue(player._live_cover_visible)
            self.assertEqual(player._live_cover_bitmap,Path('art.bgra'))
