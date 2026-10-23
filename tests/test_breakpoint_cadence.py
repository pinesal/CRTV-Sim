import json
import subprocess
import threading
import unittest
from unittest.mock import patch

import test_breakpoints as fixtures
from retrotv.breakpoints import BreakpointAnalyzer
from retrotv.break_selection import match_manual, select_breaks
from retrotv.program_timing import break_targets, normal_package_ms
from retrotv.process_control import ProcessCancelled


def cue(point, method='fade', **signals):
    return {'offset_ms': point, 'method': method, 'score': .8, 'signals': signals}


class TimingTests(unittest.TestCase):
    def test_packages_and_targets(self):
        for seconds, minutes, count in [(1380,30,1),(2490.2,60,3),(2519.3,60,3),
                (4200,90,5),(5400,90,5),(6000,120,7),(7200,120,7),(10800,180,11)]:
            with self.subTest(seconds=seconds):
                duration = round(seconds * 1000)
                package = normal_package_ms(duration, 1_800_000, 1_800_000)
                self.assertEqual(package, minutes * 60_000)
                targets = break_targets(duration, package, 300_000)
                self.assertEqual(len(targets), count)
                points = [0, *[t['target_ms'] for t in targets], duration]
                self.assertTrue(all(b-a >= 300_000 for a,b in zip(points,points[1:])))
                self.assertEqual([t['role'] for t in targets][::2], ['interior']*((count+1)//2))

    def test_rounding_custom_slots_and_spacing_cap(self):
        self.assertEqual(normal_package_ms(1_800_000,1_800_000,1_800_000),1_800_000)
        self.assertEqual(normal_package_ms(1_800_001,1_800_000,1_800_000),3_600_000)
        self.assertEqual(normal_package_ms(4_200_000,2_700_000,1_800_000),5_400_000)
        self.assertEqual(len(break_targets(600_000,3_600_000,300_000)),1)
        self.assertEqual(break_targets(599_999,3_600_000,300_000),[])

    def test_joint_assignment_does_not_let_first_cut_block_second(self):
        targets=[{'index':1,'target_ms':700_000,'role':'interior'},
                 {'index':2,'target_ms':1_000_000,'role':'portion_boundary'}]
        selected,_=select_breaks(targets,[cue(500_000),cue(800_000),cue(1_000_000)],
                                 {},300_000,240_000,False,1_800_000)
        self.assertEqual([c['offset_ms'] for c in selected],[500_000,1_000_000])

    def test_strong_cue_beyond_window_and_weak_cues_within_window(self):
        targets=[{'index':1,'target_ms':600_000,'role':'interior'}]
        for fallback in (True,False):
            selected,_=select_breaks(targets,[cue(1_000_000),cue(601_000,'quiet_pause')],
                                     {},300_000,240_000,fallback,1_800_000)
            self.assertEqual(selected[0]['offset_ms'],1_000_000)
            self.assertTrue(selected[0]['signals']['outside_preferred_window'])
        selected,_=select_breaks(targets,[cue(1_000_000,'still_card')],{},300_000,240_000,False,1_800_000)
        self.assertEqual(selected,[])

    def test_manual_matching_is_nearest_distinct_monotone(self):
        targets=break_targets(2_490_200,3_600_000,300_000)
        self.assertEqual(match_manual(targets,{1:775_000,2:1_369_000}),{1:775_000,2:1_369_000})
        selected,_=select_breaks(targets,[cue(800_000),cue(1_350_000),cue(2_100_000)],
            {1:775_000,2:1_369_000},300_000,240_000,True,2_490_200)
        self.assertEqual(len(selected),3)
        self.assertEqual([c['offset_ms'] for c in selected][:2],[775_000,1_369_000])


class LayoutTests(unittest.TestCase):
    setUp=fixtures.BreakpointTests.setUp
    tearDown=fixtures.BreakpointTests.tearDown

    def service(self):
        service=BreakpointAnalyzer(self.connection,self.config)
        return service

    def test_policy_refresh_retains_cuts_and_publishes_layout(self):
        service=self.service()
        with patch.object(service,'_collect_candidates',return_value=[]):service.analyze_asset(1)
        before=service.recorded_layout(1)
        self.config.data['scheduling']['midroll_search_window_minutes']=5
        updated=self.service()
        self.assertEqual(updated.selected_offsets(1),[690_000])
        self.assertEqual(updated.recorded_layout(1),before)
        self.assertEqual(len(updated.pending_assets()),1)
        with patch.object(updated,'_collect_candidates',return_value=[]):updated.analyze_asset(1)
        self.assertNotEqual(updated.recorded_layout(1)['identity'],before['identity'])
        self.assertEqual(updated.pending_assets(),[])

    def test_failed_refresh_preserves_previous_layout_and_retries(self):
        service=self.service()
        with patch.object(service,'_collect_candidates',return_value=[]):service.analyze_asset(1)
        before=service.recorded_layout(1)
        with patch.object(service,'_collect_candidates',side_effect=RuntimeError('decode failure')):
            self.assertEqual(service.analyze_asset(1).status,'error')
        self.assertEqual(service.selected_offsets(1),[690_000])
        self.assertEqual(service.recorded_layout(1),before)
        self.assertEqual(len(service.pending_assets()),1)
        self.assertIn('decode failure',self.connection.execute('SELECT refresh_error FROM asset_break_analyses').fetchone()[0])

    def test_first_failed_analysis_shows_new_cadence_without_inventing_cuts(self):
        self.connection.execute('UPDATE asset_media_info SET duration_seconds=2490.2')
        self.connection.commit()
        service=self.service()
        with patch.object(service,'_collect_candidates',side_effect=RuntimeError('decode failure')):
            self.assertEqual(service.analyze_asset(1).status,'error')
        self.assertEqual(service.selected_offsets(1),[])
        self.assertEqual(len(service.recorded_layout(1)['targets']),3)
        self.assertEqual(len(service.pending_assets()),1)

    def test_legacy_manual_overflow_never_discards_timestamps(self):
        self.connection.execute('UPDATE asset_media_info SET duration_seconds=7200')
        points=[600_000 * i for i in range(1,9)]
        self.connection.executemany('INSERT INTO asset_break_overrides VALUES (1,?,?,\'old\')',enumerate(points,1))
        self.connection.commit()
        service=self.service()
        with patch.object(service,'_run_detection',side_effect=AssertionError('all manual; no scan')):
            service.analyze_asset(1)
        layout=service.recorded_layout(1)
        self.assertEqual(layout['intended_count'],7)
        self.assertTrue(layout['manual_overflow'])
        self.assertEqual(service.selected_offsets(1),points)
        with patch.object(service,'_collect_candidates',return_value=[]):service.reset_manual(1,8)
        self.assertEqual(service.selected_offsets(1),points[:7])
        self.assertFalse(service.recorded_layout(1)['manual_overflow'])

    def test_legacy_two_target_manuals_reconcile_without_timestamp_changes(self):
        self.connection.execute('UPDATE asset_media_info SET duration_seconds=2490.2')
        self.connection.executemany('INSERT INTO asset_break_overrides VALUES (1,?,?,\'old\')',[(1,775_000),(2,1_369_000)])
        self.connection.execute("INSERT INTO asset_break_analyses(asset_id,source_signature,status) VALUES (1,'5:10:breaks-v7','manual')")
        self.connection.commit()
        service=self.service()
        self.assertEqual(len(service.recorded_layout(1)['targets']),2)
        with patch.object(service,'_collect_candidates',return_value=[]):service.analyze_asset(1)
        self.assertEqual(len(service.recorded_layout(1)['targets']),3)
        self.assertEqual(list(service._manuals(1).values()),[775_000,1_369_000])
        self.assertEqual(len(service.selected_offsets(1)),3)

    def test_cancel_and_transaction_failure_keep_entire_old_result(self):
        service=self.service()
        with patch.object(service,'_collect_candidates',return_value=[]):service.analyze_asset(1)
        before=service.recorded_layout(1)
        with patch.object(service,'_collect_candidates',side_effect=ProcessCancelled('cancel')):
            with self.assertRaises(ProcessCancelled):service.analyze_asset(1)
        self.connection.execute("CREATE TRIGGER fail_layout BEFORE UPDATE OF target_layout_json ON asset_break_analyses BEGIN SELECT RAISE(ABORT,'test failure'); END")
        self.connection.commit()
        with self.assertRaises(Exception):service.set_manual(1,650_000)
        self.assertEqual(service._manuals(1),{})
        self.assertEqual(service.selected_offsets(1),[690_000])
        self.assertEqual(service.recorded_layout(1),before)

    def test_background_refresh_cannot_erase_a_new_manual_choice(self):
        from retrotv.db import connect_database
        service=self.service()
        with patch.object(service,'_collect_candidates',return_value=[]):service.analyze_asset(1)
        def concurrent_edit(*args):
            other=connect_database(self.config.database_path)
            try:BreakpointAnalyzer(other,self.config).set_manual(1,650_000)
            finally:other.close()
            return []
        with patch.object(service,'_collect_candidates',side_effect=concurrent_edit):
            with self.assertRaisesRegex(ValueError,'changed during analysis'):service.analyze_asset(1)
        self.assertEqual(service.selected_offsets(1),[650_000])
        self.assertEqual(service._manuals(1),{1:650_000})

    def test_first_manual_edit_respects_selected_target(self):
        self.connection.execute('UPDATE asset_media_info SET duration_seconds=2640')
        self.connection.commit()
        service=self.service()
        with patch.object(service,'_collect_candidates',return_value=[]):
            service.set_manual(1,1_000_000,target_index=2)
        self.assertEqual(service._manuals(1),{2:1_000_000})
        self.assertIn(1_000_000,service.selected_offsets(1))

    def test_real_noisy_fade_and_abrupt_tape_flash(self):
        ffmpeg=self.project/'dist/CRTV-Sim-Portable-0.9.55-IPTV-Test-2/tools/ffmpeg/ffmpeg.exe'
        if not ffmpeg.is_file():self.skipTest('Bundled FFmpeg unavailable')
        self.config.data['tools']={'ffmpeg':str(ffmpeg),'ffprobe':str(ffmpeg.with_name('ffprobe.exe'))}
        service=self.service();service.minimum_segment_ms=1000
        for fade in (True,False):
            hold=1 if fade else .134
            source=self.root/f'noisy-{fade}.mp4'
            leading='fade=t=out:st=2.4:d=0.6' if fade else 'null'
            following='fade=t=in:st=0:d=0.6' if fade else 'null'
            filters=(f'[0:v]{leading}[a];[2:v]{following}[c];'
                     "[a][1:v][c]concat=n=3:v=1:a=0,noise=alls=6:allf=t,"
                     "lutyuv=y='clip(val+18,16,235)',drawbox=x=0:y=116:w=160:h=4:color=white:t=fill[v]")
            subprocess.run([str(ffmpeg),'-v','error','-y','-f','lavfi','-i','testsrc2=s=160x120:r=30:d=3',
                '-f','lavfi','-i',f'color=c=black:s=160x120:r=30:d={hold}',
                '-f','lavfi','-i','testsrc2=s=160x120:r=30:d=3',
                '-f','lavfi','-i',f'sine=frequency=440:duration={6+hold}',
                '-filter_complex',filters,'-map','[v]','-map','3:a','-c:v','libx264','-preset','ultrafast',
                '-c:a','aac','-shortest',str(source)],capture_output=True,check=True,timeout=30)
            duration=round((6+hold)*1000)
            log=service._run_detection(source,0,duration)
            intervals=service._fade_intervals(log,0)
            if fade:
                self.assertTrue(intervals)
                self.assertTrue(any(i['noisy_black'] and not i['strict_black'] for i in intervals))
                self.assertAlmostEqual(intervals[0]['black_start_ms'],3000,delta=250)
            else:self.assertEqual(intervals,[])

    def test_stale_workshop_cannot_save_even_without_client_identity(self):
        from retrotv.act_break_workshop import ActBreakWorkshop
        service=self.service()
        with patch.object(service,'_collect_candidates',return_value=[]):service.analyze_asset(1)
        workshop=ActBreakWorkshop(self.config)
        try:
            opened=workshop.request(1,{'action':'open'})
            service.set_manual(1,650_000)
            with self.assertRaisesRegex(ValueError,'targets changed'):
                workshop.save(self.connection,1,{'action':'reset','target_index':1,'workshop_session':opened['session_id']})
        finally:workshop.close()

    def test_chunk_discovery_merges_halos_and_checks_cancellation(self):
        service=self.service()
        calls=[]
        def detect(path,left,right):
            calls.append((left,right))
            return f'black_start:{(780000-left)/1000} black_end:{(781000-left)/1000} black_duration:1'
        with patch.object(service,'_run_detection',side_effect=detect):
            found=service._collect_candidates(None,1_380_000,[])
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]['offset_ms'],780_500)
        self.assertEqual(len(calls),2)
        self.assertTrue(all(b-a <= service.CHUNK_MS+2*service.CHUNK_OVERLAP_MS for a,b in calls))
        service.cancel_event=threading.Event();service.cancel_event.set()
        with self.assertRaises(ProcessCancelled):service._collect_candidates(None,1_380_000,[])


class NoisyFadeTests(unittest.TestCase):
    def setUp(self):self.service=BreakpointAnalyzer(None,type('Config',(),{'data':{}})())

    def log(self, dark=.2, fade=True, bounded=True):
        lines=[f'[blackdetect@noisy] black_start:2 black_end:{2+dark} black_duration:{dark}']
        for i in range(150):
            t=i/30
            if fade and 1.5<=t<2:value=80-(t-1.5)*90
            elif 2<=t<=2+dark:value=35+(i%2)
            else:value=80 if bounded else 38
            lines.extend([f'[metadata@luma] frame:{i} pts_time:{t}',f'[metadata@luma] lavfi.signalstats.YAVG={value}'])
        return '\n'.join(lines)

    def test_noise_fade_and_short_real_fade_need_complete_shoulders(self):
        for duration in (.133,.3,1.4):
            with self.subTest(duration=duration):
                intervals=self.service._fade_intervals(self.log(duration),0)
                self.assertEqual(len(intervals),1)
                self.assertGreaterEqual(intervals[0]['before_contrast'],12)
                self.assertGreaterEqual(intervals[0]['after_contrast'],12)
                self.assertGreaterEqual(intervals[0]['fade_in_ms'],100)
        self.assertEqual(self.service._fade_intervals(self.log(fade=False),0),[])
        self.assertEqual(self.service._fade_intervals(self.log(bounded=False),0),[])

    def test_merge_strict_and_tolerant_and_do_not_make_luma_frames_scenes(self):
        log=self.log(.3)+'\n[blackdetect@strict] black_start:2.05 black_end:2.2 black_duration:0.15'
        found=self.service._fade_intervals(log,300_000)
        self.assertEqual(len(found),1)
        self.assertTrue(found[0]['strict_black'] and found[0]['noisy_black'])
        self.assertEqual(found[0]['black_start_ms'],302_000)
        self.assertEqual(self.service._scene_events(log),[])
        self.assertEqual(len(self.service._fade_intervals('[blackdetect@strict] black_start:2 black_end:2.1 black_duration:0.1',0)),1)
