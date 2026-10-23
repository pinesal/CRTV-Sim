import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import test_breakpoints as breakpoint_fixtures
from retrotv.act_break_workshop import ActBreakWorkshop
from retrotv.act_break_workshop_ui import ACT_BREAK_WORKSHOP_JS
from retrotv.breakpoints import BreakpointAnalyzer, resolve_breakpoint_media
from retrotv.cleanup import CleanupSummary, GeneratedAssetCleanup
from retrotv.process_control import ProcessCancelled
from retrotv.scheduling import ScheduleBuilder
from retrotv.web import make_handler


class ActBreakWorkshopTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_workshop_client_laziness_selection_save_reset_and_single_player(self):
        script = r'''
          const assert=require('assert');const window={};
          const timers=[];const intervals=[];
          function setTimeout(fn){timers.push(fn);return fn;}
          function clearTimeout(fn){if(fn)fn.cancelled=true;}
          function setInterval(fn){intervals.push(fn);return fn;}
          function clearInterval(fn){if(fn)fn.cancelled=true;}
          class Element{
            constructor(tag){this.tag=tag;this.children=[];this.style={};this.value='';this.hidden=false;this.loads=0;}
            append(...nodes){this.children.push(...nodes);}
            replaceChildren(...nodes){this.children=nodes;}
            after(node){this.sibling=node;}
            remove(){this.removed=true;}
            pause(){this.paused=true;}
            load(){this.loads++;}
            async play(){this.paused=false;}
            removeAttribute(name){this[name]='';}
            getAttribute(name){return this[name]||null;}
            set src(value){this._src=value;if(this.tag==='img')Promise.resolve().then(()=>this.onload?.());}
            get src(){return this._src;}
          }
          const document={createElement:tag=>new Element(tag)};
          async function flush(){for(let n=0;n<30;n++)await Promise.resolve();}
          async function drain(){while(timers.length){const fn=timers.shift();if(!fn.cancelled)await fn();}await flush();}
          const calls=[];let saved=0,lastOffset=0,manual=false,hold=false,release;
          const item={id:1,duration_seconds:2640,series_title:'Show',display_title:'Episode',targets:[
            {index:1,target_ms:600000,selected:{offset_ms:600000,method:'fade'}},
            {index:2,target_ms:1500000,selected:{offset_ms:1500000,method:'scene_fallback'}}]};
          function ready(offset,playback=false){
            lastOffset=offset;return {state:'ready',generation:calls.length,result:{offset_ms:offset,source_seconds:offset/1000,
              duration_ms:2640000,start_ms:offset-30000,end_ms:offset+30000,frame_token:'frame-'+offset,
              frame_url:'frame-'+offset,clip_url:playback?'clip':null,mapping:playback?[[0,offset/1000-30],[25,offset/1000-5],
                [30,offset/1000],[35,offset/1000+5],[60,offset/1000+30]]:[]}};
          }
          async function api(path,options){
            const body=options?JSON.parse(options.body):null;calls.push({path,body});
            if(path.includes('/act-break-workshop')){
              if(body.action==='open')return {session_id:'session-'+calls.length};
              if(body.action==='close')return {state:'closed'};
              if(hold){hold=false;return new Promise(resolve=>release=resolve);}
              return ready(body.action==='step'?lastOffset+body.direction*40:body.offset_ms,body.action==='playback');
            }
            if(path.endsWith('/breakpoint')){manual=body.action==='manual';return {};}
            if(path.startsWith('/api/breakpoints?'))return {items:[{...item,targets:item.targets.map(t=>
              t.index===2?{...t,manual,selected:{offset_ms:manual?lastOffset:1500000,method:manual?'manual':'midpoint'}}:t)}]};
            throw Error('Unexpected endpoint '+path);
          }
          const options={api,format:ms=>String(ms),onSaved:()=>saved++};
        ''' + ACT_BREAK_WORKSHOP_JS + r'''
          (async()=>{
            const row=new Element('tr');row.children=Array.from({length:8},()=>new Element('td'));
            const w=ActBreakWorkshopUI.open(row,item,options);
            assert.strictEqual(w.targetIndex,2);assert.strictEqual(calls.length,0);
            assert.strictEqual(w.place.disabled,true);await drain();
            assert.strictEqual(w.position,1497000);assert.strictEqual(w.place.disabled,false);
            assert.strictEqual(w.video.loads,0);assert.strictEqual(w.zoom.disabled,true);
            assert.ok(!calls.some(c=>c.body?.action==='playback'));
            await w.next.onclick();await flush();assert.strictEqual(w.position,1497040);
            await w.place.onclick();assert.strictEqual(saved,1);assert.strictEqual(row.sibling.removed,undefined);
            const save=calls.find(c=>c.path.endsWith('/breakpoint'));
            assert.strictEqual(save.body.offset_ms,1497040);assert.strictEqual(save.body.target_index,2);
            assert.strictEqual(save.body.frame_token,'frame-1497040');assert.strictEqual(w.reset.hidden,false);
            assert.strictEqual(w.video.loads,0);
            await w.reset.onclick();assert.strictEqual(saved,2);assert.strictEqual(w.reset.hidden,true);
            w.timestamp.value='00:10:20.500';w.timestamp.oninput();assert.strictEqual(w.place.disabled,true);
            const buttons=w.panel.children.flatMap(n=>n.children||[]);
            buttons.find(b=>b.textContent==='Go to timestamp').onclick();await drain();
            assert.strictEqual(w.position,620500);assert.strictEqual(w.place.disabled,false);
            await w.playAround();assert.strictEqual(w.playing,true);assert.strictEqual(w.place.disabled,true);
            assert.strictEqual(w.video.loads,1);assert.strictEqual(w.zoom.disabled,false);
            assert.strictEqual(calls.filter(c=>c.body?.action==='playback').length,1);
            assert.strictEqual(w.stopAt,625500);w.video.currentTime=35;w.video.ontimeupdate();await drain();
            assert.strictEqual(w.playing,false);assert.strictEqual(w.position,625500);
            hold=true;const old=w.prepare('preview');await flush();
            w.seek(800000,false);await drain();release(ready(700000));await old;await flush();
            assert.strictEqual(w.position,800000);
            const second=ActBreakWorkshopUI.open(new Element('tr'),item,options);
            assert.strictEqual(w.closed,true);assert.strictEqual(w.video.paused,true);assert.strictEqual(w.detailRow.removed,true);
            assert.ok(calls.some(c=>c.body?.action==='close'));
            await drain();second.close();assert.strictEqual(ActBreakWorkshopUI.current,null);
            assert.ok(intervals.every(fn=>fn.cancelled));
          })().catch(error=>{console.error(error);process.exitCode=1});
        '''
        result=subprocess.run(['node','-e',script],capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)

    def setUp(self):
        breakpoint_fixtures.BreakpointTests.setUp(self)
        self.config.data['storage_roots']['state'] = str(self.root / 'state')
        self.stop = threading.Event()
        self.service = ActBreakWorkshop(self.config, stop_event=self.stop)
        self.source = self.media / 'fox/all-day/episodes/Show/episode.mp4'

    def tearDown(self):
        self.service.close()
        breakpoint_fixtures.BreakpointTests.tearDown(self)

    def wait(self, session_id, generation=None):
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            data = self.service.status(1, session_id)
            if data['state'] not in {'queued', 'running'} and (generation is None or data['generation'] == generation):
                self.assertEqual(data['state'], 'ready', data)
                return data['result']
            time.sleep(.02)
        self.fail('Workshop did not complete')

    def fake_build(self, source, offset, direction, cancel):
        directory = self.service._touch(source['identity'])
        clip = directory / 'clip.mp4'; clip.write_bytes(b'preview')
        frame = directory / 'frame.jpg'; frame.write_bytes(b'frame')
        return {'clip_token':'clip-token', 'frame_token':'frame-token',
                'offset_ms':round(offset), 'source_seconds':offset / 1000,
                'mapping':[[0, offset/1000]], 'duration_ms':1380000,
                'start_ms':0, 'end_ms':60000}, {'clip-token':clip,'frame-token':frame}

    def video(self, filters=None, *, duration=4, rate=5, audio=False):
        args = ['ffmpeg','-v','error','-y','-f','lavfi','-i',f'testsrc2=s=160x120:r={rate}:d={duration}']
        if audio:
            args += ['-f','lavfi','-i',f'sine=frequency=440:sample_rate=16000:duration={duration}','-c:a','aac']
        if filters:
            args += ['-vf', filters]
        subprocess.run([*args,'-fps_mode','vfr','-enc_time_base','1:1000','-c:v','ffv1',str(self.source)], check=True,
                       stdin=subprocess.DEVNULL, capture_output=True)

    def test_open_is_lazy_and_artifacts_are_asset_scoped_opaque_ids(self):
        before = self.source.read_bytes()
        with patch.object(self.service, '_build', side_effect=self.fake_build) as build:
            opened = self.service.request(1, {'action':'open'})
            build.assert_not_called()
            self.assertEqual(self.service.threads, [])
            session = opened['session_id']
            queued = self.service.request(1, {'session_id':session,'offset_ms':600000})
            result = self.wait(session, queued['generation'])
            self.service.validate_selection(1, session, result['frame_token'], 600000)
            with self.assertRaisesRegex(ValueError, 'Selected frame changed'):
                self.service.validate_selection(1, session, result['frame_token'], 600001)
            with self.assertRaises(ValueError):
                self.service.artifact(2, session, result['frame_token'])
            with self.assertRaises(ValueError):
                self.service.artifact(1, session, '../../original.mp4')
            self.assertTrue(self.service.artifact(1, session, result['frame_token']).is_file())
            self.source.write_bytes(b'changed video')
            with self.assertRaisesRegex(ValueError, 'Working video changed'):
                self.service.validate_selection(1, session, result['frame_token'], 600000)
            self.assertEqual(self.service.status(1,session)['state'], 'error')
        self.assertEqual(before, b'video')

    def test_missing_and_invalid_sources_and_requests_fail_without_workers(self):
        for payload in ({'offset_ms':float('nan')}, {'offset_ms':-1}, {'action':'unknown'}):
            with self.assertRaises(ValueError):
                self.service.request(1, payload)
        self.source.unlink()
        with self.assertRaises(FileNotFoundError):
            self.service.request(1, {'action':'open'})
        self.assertEqual(self.service.threads, [])

    def test_latest_request_wins_and_at_most_two_jobs_run(self):
        lock = threading.Lock(); entered = threading.Event(); release = threading.Event()
        active = 0; peak = 0

        def build(source, offset, direction, cancel):
            nonlocal active, peak
            with lock:
                active += 1; peak = max(peak, active)
            entered.set()
            try:
                while not release.wait(.01):
                    if cancel.is_set():
                        raise ProcessCancelled('Superseded')
                return self.fake_build(source, offset, direction, cancel)
            finally:
                with lock:
                    active -= 1

        with patch.object(self.service, '_build', side_effect=build):
            first = self.service.request(1, {'offset_ms':1000})
            self.assertTrue(entered.wait(5))
            session = first['session_id']
            for offset in range(2000,12000,1000):
                latest = self.service.request(1, {'session_id':session,'offset_ms':offset})
                with self.service.condition:
                    self.assertLessEqual(len(self.service.pending), 1)
            release.set()
            self.assertEqual(self.wait(session, latest['generation'])['offset_ms'], 11000)
            self.assertLessEqual(peak, 2)

    def test_shutdown_cancels_work_and_rejects_new_requests(self):
        entered = threading.Event()

        def build(source, offset, direction, cancel):
            entered.set()
            while not cancel.is_set():
                time.sleep(.01)
            raise ProcessCancelled('Stopped')

        with patch.object(self.service, '_build', side_effect=build):
            self.service.request(1, {'offset_ms':1000})
            self.assertTrue(entered.wait(5))
            self.stop.set()
            self.service.close()
            self.assertFalse(any(worker.is_alive() for worker in self.service.threads))
            with self.assertRaisesRegex(ValueError, 'shutting down'):
                self.service.request(1, {'action':'open'})

    def test_shutdown_waits_for_an_in_progress_manual_save(self):
        with patch.object(self.service, '_build', side_effect=self.fake_build):
            session = self.service.request(1, {'offset_ms':600000})['session_id']
            result = self.wait(session)
        entered = threading.Event();finished = threading.Event()

        def save(*args):
            entered.set();self.service.closed.wait(5);finished.set()

        payload = {'action':'manual','workshop_session':session,'frame_token':result['frame_token'],
                   'offset_ms':600000,'target_index':1}
        with patch.object(BreakpointAnalyzer,'set_manual',side_effect=save), \
             patch.object(BreakpointAnalyzer,'recorded_layout',return_value={'identity':self.service._source(1)['layout_identity']}):
            worker = threading.Thread(target=lambda:self.service.save(None,1,payload))
            worker.start()
            self.assertTrue(entered.wait(5))
            self.service.close()
            self.assertTrue(finished.is_set())
            worker.join(5)
            self.assertFalse(worker.is_alive())

    def test_integer_pts_are_used_instead_of_rounded_display_times(self):
        output = '[showinfo] config in time_base: 1/30000, frame_rate: 30000/1001\n' \
                 '[showinfo] n: 0 pts: 216001001 pts_time:7200.03'
        self.assertEqual(self.service._times(output), [216001001/30000])

    def test_preview_failure_is_explicit_and_close_cancels_session(self):
        with patch.object(self.service, '_build', side_effect=RuntimeError('decoder failed')):
            data = self.service.request(1, {'offset_ms':1000})
            session = data['session_id']
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = self.service.status(1, session)
                if state['state'] == 'error':
                    break
                time.sleep(.01)
            self.assertEqual(state['error'], 'decoder failed')
            self.service.close_session(1, session)
            with self.assertRaises(ValueError):
                self.service.status(1, session)

    def test_staged_proxy_and_external_root_use_shared_read_only_resolution(self):
        proxy = self.config.storage_root('normalized') / 'working.mp4'
        proxy.parent.mkdir(parents=True);proxy.write_bytes(b'working copy')
        self.connection.execute("INSERT INTO normalization_profiles(asset_id,proxy_status,proxy_relative_path) VALUES(1,'ready','working.mp4')")
        self.connection.commit()
        self.assertEqual(resolve_breakpoint_media(self.connection, self.config, 1), proxy.resolve())
        self.connection.execute("UPDATE storage_roots SET source_kind='external',absolute_path=? WHERE logical_name='media'", (str(self.media.resolve()),))
        self.connection.commit()
        self.assertEqual(resolve_breakpoint_media(self.connection, self.config, 1), self.source.resolve())
        original = self.source.read_bytes()
        with patch.object(self.service, '_build', side_effect=self.fake_build):
            session = self.service.request(1, {'offset_ms':1000})['session_id']
            self.wait(session)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(list(self.media.rglob('*.*')), [self.source])

    def test_inactive_preview_cleanup_preserves_active_lease_and_unrelated_files(self):
        root = self.config.storage_root('previews') / 'act-breaks'
        old = root / ('a' * 64); active = root / ('b' * 64)
        now = time.time()
        for directory, age in ((old,3601),(active,0)):
            directory.mkdir(parents=True)
            (directory / 'clip.mp4').write_bytes(b'preview')
            lease = directory / '.lease';lease.touch();os.utime(lease,(now-age,now-age))
        unrelated = root / 'notes.txt';unrelated.write_text('keep')
        service = GeneratedAssetCleanup(self.connection,self.config)
        summary = CleanupSummary()
        service._prune_act_break_previews(summary,set(),now,3600)
        self.assertFalse(old.exists());self.assertTrue((active/'clip.mp4').exists())
        self.assertTrue(unrelated.is_file());self.assertEqual(summary.errors,[])

    def test_manual_frame_choice_changes_schedule_fingerprint_and_preserves_other_overrides(self):
        self.connection.execute('UPDATE asset_media_info SET duration_seconds=2640')
        self.connection.commit()
        analyzer = BreakpointAnalyzer(self.connection,self.config)
        with patch.object(analyzer,'_run_detection',return_value=''):
            analyzer.set_manual(1,600000,1)
            before = ScheduleBuilder(self.connection,self.config)._configuration_fingerprint('fox')
            analyzer.set_manual(1,1400000,2)
            after = ScheduleBuilder(self.connection,self.config)._configuration_fingerprint('fox')
            self.assertNotEqual(before,after)
            self.assertEqual(analyzer.selected_offsets(1)[:2],[600000,1400000])
            analyzer.analyze_asset(1)
            self.assertEqual(analyzer.selected_offsets(1)[:2],[600000,1400000])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_scrubbing_and_stepping_never_encode_clips_or_repeat_metadata_probe(self):
        self.video(duration=8, rate=25)
        with patch.object(self.service, '_build_clip_locked', side_effect=AssertionError('Scrub encoded video')), \
                patch.object(self.service, '_probe', wraps=self.service._probe) as probe, \
                patch.object(self.service, '_run', wraps=self.service._run) as run:
            session = self.service.request(1, {'offset_ms':4200})['session_id']
            result = self.wait(session)
            self.assertEqual(result['offset_ms'],4200)
            self.service.request(1, {'session_id':session,'action':'step','direction':1})
            self.assertEqual(self.wait(session)['offset_ms'],4240)
            self.service.request(1, {'session_id':session,'offset_ms':4400})
            self.assertEqual(self.wait(session)['offset_ms'],4400)
            self.assertEqual(probe.call_count,1)
            timing_calls = [c.args[0] for c in run.call_args_list if 'null' in c.args[0]]
            self.assertEqual(len(timing_calls),1)
            self.assertEqual(timing_calls[0][timing_calls[0].index('-t')+1],'2')
            self.assertLess(timing_calls[0].index('-ss'),timing_calls[0].index('-i'))
            self.assertIsNone(result['clip_url'])
            self.assertEqual(list(self.service.root.rglob('*.mp4')),[])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_long_gop_seeking_late_in_program_uses_short_timestamp_decode(self):
        subprocess.run([
            'ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=s=160x120:r=25:d=70',
            '-c:v','libx264','-preset','ultrafast','-g','250','-bf','3',str(self.source),
        ],check=True,capture_output=True,timeout=30)
        with patch.object(self.service,'_run',wraps=self.service._run) as run:
            session = self.service.request(1,{'offset_ms':59400})['session_id']
            result = self.wait(session)
            self.assertEqual(result['offset_ms'],59400)
            self.service.request(1,{'session_id':session,'action':'step','direction':1})
            self.assertEqual(self.wait(session)['offset_ms'],59440)
            calls=[c.args[0] for c in run.call_args_list if 'null' in c.args[0]]
            self.assertEqual(len(calls),1)
            self.assertEqual(calls[0][calls[0].index('-t')+1],'2')
            self.assertIsNone(result['clip_token'])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_playback_window_is_short_and_reused_across_nearby_seeks(self):
        self.video(duration=70, rate=5)
        session = self.service.request(1, {'action':'playback','offset_ms':30000})['session_id']
        result = self.wait(session)
        self.assertEqual(result['end_ms'] - result['start_ms'],20000)
        with patch.object(self.service, '_build_clip_locked', side_effect=AssertionError('Cached clip rebuilt')), \
                patch.object(self.service, '_probe', side_effect=AssertionError('Metadata reprobed')):
            self.service.request(1, {'session_id':session,'offset_ms':35000})
            sought = self.wait(session)
            self.assertEqual(sought['clip_token'],result['clip_token'])
            self.assertEqual(sought['offset_ms'],35000)
            self.service.request(1, {'session_id':session,'action':'step','direction':1})
            self.assertEqual(self.wait(session)['offset_ms'],35200)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_cfr_frame_selection_matches_independently_decoded_frame(self):
        self.video(audio=True)
        original = hashlib.sha256(self.source.read_bytes()).hexdigest()
        data = self.service.request(1, {'offset_ms':1400});session=data['session_id']
        result = self.wait(session)
        self.assertEqual(result['offset_ms'],1400)
        expected = self.root/'expected.jpg'
        subprocess.run(['ffmpeg','-v','error','-y','-i',str(self.source),'-vf',
                        "select='eq(n,7)',scale=w='min(640,iw)':h=-2",'-frames:v','1',
                        '-c:v','mjpeg','-threads','1','-q:v','2','-update','1',str(expected)],
                       check=True,capture_output=True)
        frame = self.service.artifact(1,session,result['frame_token'])
        self.assertEqual(frame.read_bytes(),expected.read_bytes())
        self.assertIsNone(result['clip_token'])
        self.assertEqual(result['mapping'], [])
        self.service.request(1, {'session_id':session,'action':'playback','offset_ms':1400})
        result = self.wait(session)
        clip = self.service.artifact(1,session,result['clip_token'])
        probe = self.service._probe(clip, threading.Event())
        self.assertTrue(any(stream['codec_type']=='audio' for stream in probe['streams']))
        self.service.request(1, {'session_id':session,'action':'step','direction':1})
        self.assertEqual(self.wait(session)['offset_ms'],1600)
        self.service.request(1, {'session_id':session,'action':'step','direction':-1})
        self.assertEqual(self.wait(session)['offset_ms'],1400)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),original)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_vfr_nonzero_origin_and_window_boundary(self):
        self.video(r'settb=1/1000,setpts=if(lt(N\,5)\,N*200\,1000+(N-5)*400)')
        session=self.service.request(1, {'offset_ms':1400})['session_id']
        self.assertEqual(self.wait(session)['offset_ms'],1400)
        self.service.request(1, {'session_id':session,'action':'step','direction':1})
        self.assertEqual(self.wait(session)['offset_ms'],1800)
        self.service.close_session(1,session)
        self.video('setpts=PTS+5/TB')
        session=self.service.request(1, {'offset_ms':1400})['session_id']
        result=self.wait(session)
        self.assertEqual(result['duration_ms'],4000)
        self.assertEqual(result['offset_ms'],1400)
        self.service.request(1, {'session_id':session,'action':'playback','offset_ms':1400})
        self.assertEqual(self.wait(session)['mapping'][0],[0,0])
        self.service.close_session(1,session)
        self.video(duration=70,rate=2)
        session=self.service.request(1, {'offset_ms':59500})['session_id']
        self.assertEqual(self.wait(session)['offset_ms'],59500)
        self.service.request(1, {'session_id':session,'action':'step','direction':1})
        self.assertEqual(self.wait(session)['offset_ms'],60000)
        self.service.request(1, {'session_id':session,'offset_ms':0})
        self.assertEqual(self.wait(session)['offset_ms'],0)
        self.service.request(1, {'session_id':session,'action':'step','direction':-1})
        self.assertEqual(self.wait(session)['offset_ms'],0)

    def test_http_session_status_artifact_save_and_legacy_compatibility(self):
        handler=make_handler(self.config)
        server=ThreadingHTTPServer(('127.0.0.1',0),handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'

        def post(path,body):
            with urlopen(Request(base+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})) as response:
                return json.load(response)

        try:
            with patch.object(ActBreakWorkshop,'_build',side_effect=self.fake_build), \
                    patch.object(BreakpointAnalyzer,'_run_detection',return_value=''):
                route='/api/assets/1/act-break-workshop'
                opened=post(route,{'offset_ms':600000});session=opened['session_id']
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    with urlopen(base+route+'?session_id='+session) as response:state=json.load(response)
                    if state['state']=='ready':break
                    time.sleep(.02)
                self.assertEqual(state['state'],'ready',state)
                result=state['result']
                with urlopen(base+result['frame_url']) as response:self.assertEqual(response.read(),b'frame')
                body={'action':'manual','target_index':1,'offset_ms':600000,
                      'workshop_session':session,'frame_token':result['frame_token']}
                self.assertEqual(post('/api/assets/1/breakpoint',body)['method'],'manual')
                with urlopen(base+'/api/breakpoints?asset_id=1') as response:
                    self.assertTrue(json.load(response)['items'][0]['targets'][0]['manual'])
                body['offset_ms']=600001
                with self.assertRaises(HTTPError):post('/api/assets/1/breakpoint',body)
                self.assertEqual(post('/api/assets/1/breakpoint',{'action':'manual','offset_ms':610000})['offset_ms'],610000)
                post(route,{'action':'close','session_id':session})
                with self.assertRaises(HTTPError):urlopen(base+route+'?session_id='+session)
                with urlopen(base+'/static/act-break-workshop.js') as response:
                    self.assertIn(b'Place Act Break',response.read())
        finally:
            handler.close_workshop();server.shutdown();server.server_close();thread.join()


if __name__=='__main__':
    unittest.main()
