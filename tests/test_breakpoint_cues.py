import json
import subprocess
import threading
import unittest
from unittest.mock import patch

import test_breakpoints as fixtures
from retrotv.breakpoints import BreakpointAnalyzer
from retrotv.process_control import ProcessCancelled


def cue_log(holds=(), scenes=(), quiet=None):
    lines=[]
    for left,right in holds:
        lines.extend([f'lavfi.freezedetect.freeze_start: {left}',f'lavfi.freezedetect.freeze_end: {right}'])
    for point,score in scenes:
        lines.extend([f'[metadata] frame:0 pts:1 pts_time:{point}',f'lavfi.scene_score={score}',f'[showinfo] pts_time:{point}'])
    if quiet:lines.extend([f'silence_start:{quiet[0]}',f'silence_end:{quiet[1]}'])
    return '\n'.join(lines)


class ExtendedCueTests(unittest.TestCase):
    setUp=fixtures.BreakpointTests.setUp
    tearDown=fixtures.BreakpointTests.tearDown

    def analyze(self, log):
        service=BreakpointAnalyzer(self.connection,self.config)
        with patch.object(service,'_chapter_starts',return_value=[]),patch.object(service,'_run_detection',return_value=fixtures.shift_window_log(log)):
            return service,service.analyze_asset(1)

    def test_priority_order_and_all_candidate_evidence_is_retained(self):
        log=cue_log([(230,234)],[(230,.8),(234,.6),(240,.9)],(245,247))
        for extra,expected in [('\nblack_start:190 black_end:190.2 black_duration:0.2','fade'),('', 'still_card')]:
            service,result=self.analyze(log+extra)
            self.assertEqual(result.method,expected)
            methods={row[0] for row in self.connection.execute('SELECT method FROM asset_break_candidates')}
            self.assertTrue({'still_card','quiet_pause','scene_fallback'}.issubset(methods))
        self.assertEqual(self.analyze(cue_log(scenes=[(240,.7)],quiet=(220,222)))[1].method,'quiet_pause')
        self.assertEqual(self.analyze(cue_log(scenes=[(240,.7)]))[1].method,'scene_fallback')
        self.assertEqual(self.analyze('')[1].method,'midpoint')

    def test_single_card_midpoint_and_adjacent_pair_boundary(self):
        service,result=self.analyze(cue_log([(238,242)],[(238,.7),(242,.6)]))
        self.assertEqual((result.method,result.offset_ms),('still_card',690000))
        service,result=self.analyze(cue_log([(236,240),(240.4,244.4)],[(236,.6),(240,.8),(240.4,.9),(244.4,.6)]))
        self.assertEqual((result.method,result.offset_ms),('still_card',690000))
        evidence=json.loads(self.connection.execute('SELECT signals_json FROM asset_break_candidates WHERE selected=1').fetchone()[0])
        self.assertTrue(evidence['paired_cards'])
        self.assertEqual(len(evidence['holds']),2)
        self.assertEqual(evidence['holds'][0]['duration_ms'],4000)

    def test_only_bounded_near_still_holds_in_duration_range_qualify(self):
        service=BreakpointAnalyzer(self.connection,self.config)
        for left,right,scenes in [(238,239.999,[(238,.5),(239.999,.5)]),
            (230,245.001,[(230,.5),(245.001,.5)]), (0,4,[(0,.5),(4,.5)]),
            (475,480,[(475,.5),(480,.5)]), (238,242,[(238,.5)]),
            (238,242,[(237.49,.5),(242,.5)]), (238,242,[]),
            (50,110,[(50,.7),(110,.8)])]:
            with self.subTest(hold=(left,right),scenes=scenes):
                self.assertEqual(service._still_candidates(cue_log([(left,right)],scenes),450000,930000,1380000),[])
        for duration in [2,15]:
            self.assertTrue(service._still_candidates(cue_log([(200,200+duration)],[(200,.5),(200+duration,.5)]),450000,930000,1380000))
        self.assertEqual(service._still_candidates('freeze_start:230',450000,930000,1380000),[])

    def test_fallback_policy_keeps_cards_but_removes_all_weak_methods(self):
        service,result=self.analyze(cue_log(scenes=[(240,.5)]))
        self.config.data['scheduling']['midroll_fallback']='none'
        self.assertEqual(service.selected_offsets(1),[])
        self.assertEqual(self.analyze(cue_log(scenes=[(240,.5)]))[1].method,'none')
        self.assertEqual(self.analyze(cue_log([(238,242)],[(238,.5),(242,.5)]))[1].method,'still_card')

    def test_scene_ties_use_strength_then_earliest_and_do_not_merge_nearby_frames(self):
        for scenes,expected in [([(239,.4),(241,.8)],691000),([(239,.8),(241,.8)],689000),
                                ([(239.9,.4),(240.1,.8)],690100)]:
            with self.subTest(scenes=scenes):
                self.assertEqual(self.analyze(cue_log(scenes=scenes))[1].offset_ms,expected)

    def test_previous_revision_remains_usable_and_manual_override_survives(self):
        service,result=self.analyze('')
        self.connection.execute("UPDATE asset_break_analyses SET source_signature='5:10:breaks-v6'")
        self.connection.commit()
        self.assertEqual(service.selected_offsets(1),[690000])
        self.assertEqual([r['id'] for r in service.pending_assets()],[1])
        with patch.object(service,'_run_detection',return_value=''):
            service.set_manual(1,650000)
        with patch.object(service,'_run_detection',side_effect=AssertionError('manual target must not be analyzed')):
            service.analyze_asset(1)
        self.assertEqual(service.selected_offsets(1),[650000])

    def test_cancellation_does_not_replace_existing_results_with_midpoint(self):
        service,result=self.analyze(cue_log(scenes=[(239,.8)]))
        with patch.object(service,'_run_detection',side_effect=ProcessCancelled('cancelled')):
            with self.assertRaises(ProcessCancelled):service.analyze_asset(1)
        self.assertEqual(service.selected_offsets(1),[689000])

    def test_api_filters_and_evidence_distinguish_cards_from_fallbacks(self):
        from http.server import ThreadingHTTPServer
        from urllib.request import urlopen
        from retrotv.web import make_handler
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.config))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def get(status):
            with urlopen(f'http://127.0.0.1:{server.server_port}/api/breakpoints?status={status}',timeout=10) as response:
                return json.load(response)['items']
        try:
            self.analyze(cue_log([(238,242)],[(238,.8),(242,.8)]))
            self.assertEqual(get('fallback'),[])
            evidence=get('still_card')[0]['selected_evidence'][0]
            self.assertEqual(evidence['signals']['holds'][0]['duration_ms'],4000)
            self.analyze(cue_log(scenes=[(240,.8)]))
            self.assertEqual(get('still_card'),[])
            self.assertEqual(get('scene_fallback')[0]['selected_method'],'scene_fallback')
            self.assertEqual(len(get('fallback')),1)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)

    def test_real_music_card_pair_noise_and_unbounded_photo(self):
        ffmpeg=self.project/'dist/CRTV-Sim-Portable-0.9.55-IPTV-Test-2/tools/ffmpeg/ffmpeg.exe'
        if not ffmpeg.is_file():self.skipTest('Bundled FFmpeg unavailable')
        self.config.data['tools']={'ffmpeg':str(ffmpeg),'ffprobe':str(ffmpeg.with_name('ffprobe.exe'))}
        service=BreakpointAnalyzer(self.connection,self.config)
        service.minimum_segment_ms=1000
        source=self.root/'cards.mp4'
        # Real motion, two different held cards, real motion, and uninterrupted music.
        filters="[0:v]trim=duration=4,setpts=PTS-STARTPTS[a];[1:v]trim=duration=3,setpts=PTS-STARTPTS[b];[2:v]trim=duration=3,setpts=PTS-STARTPTS[c];[3:v]trim=duration=4,setpts=PTS-STARTPTS[d];[a][b][c][d]concat=n=4:v=1:a=0,noise=alls=1:allf=t[v]"
        subprocess.run([str(ffmpeg),'-v','error','-y','-f','lavfi','-i','testsrc2=s=160x120:r=10',
            '-f','lavfi','-i','color=c=blue:s=160x120:r=10','-f','lavfi','-i','color=c=yellow:s=160x120:r=10',
            '-f','lavfi','-i','testsrc2=s=160x120:r=10','-f','lavfi','-i','sine=frequency=440:sample_rate=16000',
            '-filter_complex',filters,'-map','[v]','-map','4:a','-t','14','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(source)],
            check=True,capture_output=True,timeout=30)
        log=service._run_detection(source,0,14000)
        cards=service._still_candidates(log,0,14000,14000)
        self.assertTrue(cards,log[-3000:])
        paired=[c for c in cards if c['signals']['paired_cards']]
        self.assertTrue(paired)
        self.assertAlmostEqual(paired[0]['offset_ms'],7000,delta=150)
        self.assertEqual(service._quiet_candidates(log,0,14000,14000),[])
        # No bounded visual transitions: even a long still photograph is not a card cue.
        subprocess.run([str(ffmpeg),'-v','error','-y','-f','lavfi','-i','color=c=blue:s=160x120:r=10',
            '-t','6','-c:v','libx264',str(self.root/'photo.mp4')],check=True,capture_output=True,timeout=15)
        self.assertEqual(service._still_candidates(service._run_detection(self.root/'photo.mp4',0,6000),0,6000,6000),[])
