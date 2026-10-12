import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from threading import Event
from unittest.mock import patch

from retrotv.asset_paths import resolve_asset_path, require_managed_asset, contained_path
from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database
from retrotv.external_media import ExternalMediaService, require_scheduled_channel
from retrotv.probe import ProbeResult
from retrotv.scanner import LibraryScanner
from retrotv.scheduling import ScheduleBuilder


class Inspector:
    def probe(self, path):
        return ProbeResult(True, {'duration_seconds':1380, 'width':640,'height':480,'video_codec':'h264','audio_codec':'aac'}, '{}')
    def fingerprint(self, path):
        from retrotv.probe import fingerprint_file
        return fingerprint_file(path)
    def sample(self, path, start):
        from retrotv.probe import DecodeResult
        return DecodeResult(True)


class ExternalMediaTests(unittest.TestCase):
    def setUp(self):
        fixture_root = Path(__file__).resolve().parents[1]/'.test-tmp'
        fixture_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=fixture_root)
        self.root = Path(self.temp.name)
        self.media = self.root/'media'
        self.media.mkdir()
        self.series = self.root/'existing'/'Doug'
        (self.series/'Season 1').mkdir(parents=True)
        for name in ['S01E10.mp4','S01E02.mp4','S01E01.mp4']:
            (self.series/'Season 1'/name).write_bytes(name.encode())
        project = Path(__file__).resolve().parents[1]
        self.config = AppConfig(self.root/'profile.toml',project,{
            'application':{'database_path':str(self.root/'state'/'app.sqlite3')},
            'storage_roots':{'media':str(self.media),'state':str(self.root/'state'),
                'normalized':str(self.root/'normalized'),'previews':str(self.root/'previews'),
                'originals_backup':str(self.root/'backups')},
            'library':{'channels':['nickelodeon','fox']},
            'scheduling':{'timezone':'UTC','dayparts':[
                {'slug':'morning','start':'06:00','end':'12:00'},
                {'slug':'afternoon','start':'12:00','end':'16:00'},
                {'slug':'evening','start':'16:00','end':'20:00'},
                {'slug':'night','start':'20:00','end':'06:00'}]},
        })
        self.db = connect_database(self.config.database_path)
        apply_migrations(self.db,project/'migrations')
        for slug in ['nickelodeon','fox']:
            ScheduleBuilder(self.db,self.config).ensure_channel(slug)
        self.service = ExternalMediaService(self.db,self.config,inspector=Inspector())

    def tearDown(self):
        self.db.close()
        import gc
        gc.collect()  # Older Dashboard handlers use sqlite transaction contexts.
        self.temp.cleanup()

    def add(self,scope='daytime',assignments=None):
        result=self.service.register({'path':str(self.series),'series_title':'Doug','assignments':assignments or [{'channel':'nickelodeon','scope':scope}]})
        self.source=result['root_id']
        self.service.scan(self.source)
        return self.source

    def test_import_order_and_multiple_assignments(self):
        self.add(assignments=[{'channel':'nickelodeon','scope':'daytime'},{'channel':'fox','scope':'night'}])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM assets').fetchone()[0],3)
        scheduler=ScheduleBuilder(self.db,self.config)
        candidates=scheduler._candidates('nickelodeon','morning',{'episode'})
        self.assertEqual(len(candidates),3)
        ordered=scheduler._eligible_base_series_asset_order('nickelodeon')['Doug']
        names=[self.db.execute('SELECT original_filename FROM assets WHERE id=?',(i,)).fetchone()[0] for i in ordered]
        self.assertEqual(names,['S01E01.mp4','S01E02.mp4','S01E10.mp4'])
        self.assertEqual(len(scheduler._candidates('fox','night',{'episode'})),3)
        self.assertEqual(len(scheduler._candidates('fox','morning',{'episode'})),0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM normalization_profiles').fetchone()[0],0)

    def test_rescan_new_file_move_and_disconnect(self):
        self.add()
        before={r['original_filename']:r['id'] for r in self.db.execute('SELECT * FROM assets')}
        self.service.scan(self.source)
        self.assertEqual(before,{r['original_filename']:r['id'] for r in self.db.execute('SELECT * FROM assets')})
        original=self.series/'Season 1'/'S01E01.mp4'
        moved=self.series/'S01E01-renamed.mp4'
        original.rename(moved)
        self.service.scan(self.source)
        self.assertEqual(self.db.execute('SELECT id FROM assets WHERE original_filename=?',(moved.name,)).fetchone()[0],before[original.name])
        (self.series/'S01E03.mp4').write_bytes(b'new episode')
        self.service.scan(self.source)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM assets').fetchone()[0],4)
        self.service.update(self.source,{'enabled':False})
        self.assertFalse(ScheduleBuilder(self.db,self.config)._candidates('nickelodeon','morning',{'episode'}))
        self.assertTrue(moved.exists())
        self.assertEqual(self.add(),self.source)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM assets').fetchone()[0],4)

    def test_unavailable_preserves_records_and_progress(self):
        self.add()
        offline=self.series.with_name('offline')
        self.series.rename(offline)
        self.assertEqual(self.service.scan(self.source)['status'],'unavailable')
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM assets WHERE integrity_status='ready'").fetchone()[0],3)
        self.assertFalse(ScheduleBuilder(self.db,self.config)._candidates('nickelodeon','morning',{'episode'}))
        offline.rename(self.series)
        self.service.scan(self.source)
        self.assertEqual(len(ScheduleBuilder(self.db,self.config)._candidates('nickelodeon','morning',{'episode'})),3)

    def test_resolution_and_readonly_all_services(self):
        self.add()
        asset=self.db.execute('SELECT id FROM assets LIMIT 1').fetchone()[0]
        self.assertTrue(resolve_asset_path(self.db,self.config,asset).is_relative_to(self.series))
        from retrotv.normalization import NormalizationService
        from retrotv.audio_normalization import AudioNormalizationService
        from retrotv.promotion import PromotionService
        from retrotv.normalization_review import NormalizationReviewService
        from retrotv.commercial_tails import CommercialTailService
        from retrotv.holiday_review import HolidayReviewService
        from retrotv.duplicates import DuplicateService
        actions=[lambda:require_managed_asset(self.db,self.config,asset),
            lambda:NormalizationService(self.db,self.config).analyze_asset(asset),
            lambda:AudioNormalizationService(self.db,self.config).analyze_asset(asset),
            lambda:PromotionService(self.db,self.config).replace_with_backup(asset),
            lambda:NormalizationReviewService(self.db,self.config).reopen(asset),
            lambda:CommercialTailService(self.db,self.config).analyze_asset(asset),
            lambda:HolidayReviewService(self.db,self.config).move(asset,'halloween','fox/all-day/episodes'),
            lambda:DuplicateService(self.db,self.config).apply_normalization_recipe(asset)]
        before={p.relative_to(self.series):p.read_bytes() for p in self.series.rglob('*') if p.is_file()}
        for action in actions:
            with self.subTest(action=action):
                with self.assertRaisesRegex(ValueError,'read-only'):
                    action()
        self.assertEqual(before,{p.relative_to(self.series):p.read_bytes() for p in self.series.rglob('*') if p.is_file()})
        self.assertFalse(AudioNormalizationService(self.db,self.config).pending_assets())
        LibraryScanner(self.db,self.config,inspector=Inspector())._backfill_normalization_profiles()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM normalization_profiles').fetchone()[0],0)

    def test_overlap_and_traversal(self):
        self.add()
        for folder in [self.series/'Season 1',self.series.parent,self.media]:
            with self.subTest(folder=folder),self.assertRaises(ValueError):
                self.service.register({'path':str(folder),'series_title':'Other','assignments':[{'channel':'fox','scope':'night'}]})
        for relative in ['../outside.mp4','C:\\video.mp4','/video.mp4']:
            with self.assertRaises(ValueError):
                contained_path(self.series,relative)

    def test_external_only_channel_survives_scan(self):
        result=self.service.register({'path':str(self.series),'series_title':'Doug','new_channel':'External TV','scope':'all-day'})
        self.service.scan(result['root_id'])
        LibraryScanner(self.db,self.config,inspector=Inspector()).scan()
        self.assertEqual(self.db.execute("SELECT enabled FROM channels WHERE slug='external-tv'").fetchone()[0],1)

    def test_pace_rejected_without_changing_progress(self):
        self.db.execute("INSERT INTO channel_playback_preferences(channel_id,playback_mode,updated_at) SELECT id,'watch_at_your_pace','now' FROM channels WHERE slug='fox'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError,'scheduled channel'):
            self.add(assignments=[{'channel':'fox','scope':'all-day'}])
        self.add()
        with self.assertRaisesRegex(ValueError,'Disconnect'):
            require_scheduled_channel(self.db,'nickelodeon')
        self.service.update(self.source,{'enabled':False})
        require_scheduled_channel(self.db,'nickelodeon')

    def test_cancel_and_partial_scan_do_not_remove_assets(self):
        self.add()
        event=Event();event.set()
        self.service.scan(self.source,cancel_event=event)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM assets WHERE integrity_status='ready'").fetchone()[0],3)
        with patch('retrotv.external_media.os.walk', side_effect=OSError('access lost')):
            self.service.scan(self.source)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM assets WHERE integrity_status='ready'").fetchone()[0],3)

    def test_removed_episode_reconciles_without_deleting_files(self):
        self.add()
        path = self.series/'Season 1'/'S01E01.mp4'
        asset_id=self.db.execute('SELECT id FROM assets WHERE original_filename=?',(path.name,)).fetchone()[0]
        path.unlink()
        self.service.scan(self.source)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM assets WHERE integrity_status='missing'").fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM assets').fetchone()[0],3)
        path.write_bytes(path.name.encode())
        self.service.scan(self.source)
        row=self.db.execute('SELECT id,review_status,enabled_for_scheduling FROM assets WHERE original_filename=?',(path.name,)).fetchone()
        self.assertEqual(tuple(row),(asset_id,'ready',1))

    def test_random_and_recurring_schedule_playback_and_inventory(self):
        from datetime import datetime, timedelta
        from retrotv.playback import ScheduleResolver
        from retrotv.web import _media_inventory
        from retrotv.guide import GuideService
        self.add('all-day')
        for recurring in (False,True):
            with self.subTest(recurring=recurring):
                builder=ScheduleBuilder(self.db,self.config)
                builder.update_schedule_preferences('nickelodeon',recurring_enabled=recurring,unique_weekends=recurring)
                day=date.today()+timedelta(days=2 if recurring else 1)
                generation=builder.generate_day('nickelodeon',day)
                entries=[r for r in builder.entries(generation.generation_id) if r['asset_id'] and r['is_program']]
                self.assertTrue(entries)
                entry=entries[0]
                when=datetime.fromisoformat(entry['starts_at'])+timedelta(seconds=20)
                target=ScheduleResolver(self.db,self.config).resolve('nickelodeon',when)
                self.assertEqual(target.asset_id,entry['asset_id'])
                self.assertEqual(target.path,resolve_asset_path(self.db,self.config,entry['asset_id']))
                self.assertGreaterEqual(target.media_offset_seconds,20)
                inventory=_media_inventory(self.db)
                external=[i for i in inventory['items'] if i['row_kind']=='external']
                self.assertEqual(len(external),1)
                self.assertEqual(external[0]['channel_slug'],'nickelodeon')
                self.assertEqual(external[0]['item_count'],3)
                self.assertFalse(external[0]['recurring_unplaced'])
                GuideService(self.db,self.config).snapshot(when)

    def test_missing_playback_keeps_original_entry_boundary(self):
        from datetime import datetime,timedelta
        from retrotv.playback import ScheduleResolver
        self.add('all-day')
        builder=ScheduleBuilder(self.db,self.config)
        generation=builder.generate_day('nickelodeon',date.today())
        entry=next(r for r in builder.entries(generation.generation_id) if r['asset_id'] and r['is_program'])
        resolve_asset_path(self.db,self.config,entry['asset_id']).unlink()
        when=datetime.fromisoformat(entry['ends_at'])-timedelta(seconds=5)
        slate=self.root/'slate.mp4';slate.write_bytes(b'slate')
        with patch('retrotv.playback.ensure_continuity_slate',return_value=slate),patch('retrotv.graphics.GraphicsService.select_asset',return_value=None):
            target=ScheduleResolver(self.db,self.config).resolve('nickelodeon',when)
        self.assertEqual(target.ends_at,datetime.fromisoformat(entry['ends_at']))
        self.assertEqual(target.missing_media['asset_id'],entry['asset_id'])

    def test_custom_dayparts_fingerprint_and_locked_schedule(self):
        self.add('all-day')
        builder=ScheduleBuilder(self.db,self.config)
        day=date.today()
        generation=builder.generate_day('nickelodeon',day)
        self.db.execute('UPDATE schedule_generations SET locked=1 WHERE id=?',(generation.generation_id,))
        self.db.commit()
        before=builder._configuration_fingerprint('nickelodeon')
        self.service.update(self.source,{'assignments':[{'channel':'fox','scope':'night'}]})
        after=ScheduleBuilder(self.db,self.config)._configuration_fingerprint('nickelodeon')
        self.assertNotEqual(before,after)
        with self.assertRaisesRegex(ValueError,'locked'):
            ScheduleBuilder(self.db,self.config).generate_day('nickelodeon',day)
        self.assertTrue(builder.entries(generation.generation_id))
        self.config.data['scheduling']['dayparts']=[{'slug':'custom','start':'00:00','end':'00:00'}]
        service=ExternalMediaService(self.db,self.config,inspector=Inspector())
        service.update(self.source,{'assignments':[{'channel':'fox','scope':'all-day'}]})
        self.assertEqual({r[0] for r in self.db.execute('SELECT DISTINCT daypart_slug FROM asset_eligibility')},{'custom'})

    def test_readonly_breakpoint_analysis_and_no_pending_audio(self):
        from retrotv.breakpoints import BreakpointAnalyzer
        from retrotv.guide import GuideService
        self.add()
        analyzer=BreakpointAnalyzer(self.db,self.config)
        row=analyzer._asset(self.db.execute('SELECT id FROM assets LIMIT 1').fetchone()[0])
        self.assertTrue(analyzer._playback_path(row).is_relative_to(self.series))
        with patch.object(analyzer,'_run_detection',return_value=''),patch.object(analyzer,'_chapter_starts',return_value=[]):
            analyzer.analyze_asset(row['id'])
        self.assertEqual(GuideService(self.db,self.config)._pending_action_summary()['audio_analysis_pending'],0)

    def test_duplicate_registration_and_reconnect_to_pace_rejected(self):
        self.add()
        with self.assertRaisesRegex(ValueError,'already registered'):
            self.add()
        self.service.update(self.source,{'enabled':False})
        self.db.execute("INSERT INTO channel_playback_preferences(channel_id,playback_mode,updated_at) SELECT id,'watch_at_your_pace','now' FROM channels WHERE slug='nickelodeon'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError,'scheduled channel'):
            self.service.update(self.source,{'enabled':True})

    def test_http_registration_edit_stream_and_readonly_rejection(self):
        import json
        import threading
        from http.server import ThreadingHTTPServer
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        from retrotv.web import make_handler, INDEX_HTML
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.config))
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        url=f'http://127.0.0.1:{server.server_port}'
        def post(path,payload):
            request=Request(url+path,json.dumps(payload).encode(),{'Content-Type':'application/json'})
            with urlopen(request,timeout=10) as response:
                return json.load(response)
        try:
            with patch('retrotv.external_media.FFmpegInspector',return_value=Inspector()):
                result=post('/api/external-media',{'path':str(self.series),'series_title':'Doug','assignments':[{'channel':'nickelodeon','scope':'afternoon'}]})
                source=result['root_id']
                with urlopen(url+'/api/external-media',timeout=10) as response:
                    catalog=json.load(response)
                self.assertEqual(catalog['sources'][0]['episode_count'],3)
                asset=self.db.execute('SELECT id FROM assets LIMIT 1').fetchone()[0]
                with urlopen(url+f'/api/assets/{asset}/play',timeout=10) as response:
                    self.assertEqual(response.read(),resolve_asset_path(self.db,self.config,asset).read_bytes())
                with self.assertRaises(HTTPError) as refused:
                    post(f'/api/assets/{asset}/normalization',{'action':'analyze'})
                self.assertIn('read-only',refused.exception.read().decode())
                post(f'/api/external-media/{source}',{'assignments':[{'channel':'fox','scope':'night'}]})
                post(f'/api/external-media/{source}',{'enabled':False})
                self.assertTrue((self.series/'Season 1'/'S01E01.mp4').is_file())
            self.assertIn('External Media Sources',INDEX_HTML)
            self.assertIn('if (item.source_kind === "external")',INDEX_HTML)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)

    def test_ambiguous_moves_do_not_guess_identity(self):
        for path in (self.series/'Season 1').glob('*.mp4'):
            path.write_bytes(b'identical data')
        self.add()
        original_ids={r[0] for r in self.db.execute('SELECT id FROM assets')}
        for path in (self.series/'Season 1').glob('*.mp4'):
            path.unlink()
        (self.series/'new-name.mp4').write_bytes(b'identical data')
        self.service.scan(self.source)
        active=self.db.execute("SELECT id FROM assets WHERE integrity_status='ready'").fetchone()[0]
        self.assertNotIn(active,original_ids)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM assets').fetchone()[0],4)

    def test_corrupt_file_has_review_without_normalization(self):
        self.add()
        (self.series/'broken.mp4').write_bytes(b'corrupt')
        with patch.object(self.service.inspector,'probe',return_value=ProbeResult(False,{},error_detail='bad container')):
            self.service.scan(self.source)
        row=self.db.execute("SELECT id,enabled_for_scheduling FROM assets WHERE original_filename='broken.mp4'").fetchone()
        self.assertEqual(row['enabled_for_scheduling'],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM normalization_profiles WHERE asset_id=?',(row['id'],)).fetchone()[0],0)
        self.assertTrue(self.db.execute("SELECT 1 FROM review_items WHERE asset_id=? AND reason_code='external_probe'",(row['id'],)).fetchone())

    def test_managed_aliases_cannot_edit_registered_external_files(self):
        from retrotv.asset_paths import require_writable_path
        self.add()
        source=resolve_asset_path(self.db,self.config,self.db.execute('SELECT id FROM assets LIMIT 1').fetchone()[0])
        with self.assertRaisesRegex(ValueError,'read-only'):
            require_writable_path(self.db,source)
        # The root-resolution boundary also refuses a managed entry that resolves to an external path.
        alias=self.media/'alias.mp4'
        with patch('retrotv.asset_paths.Path.resolve',autospec=True,side_effect=lambda path:source if path==alias else path):
            with self.assertRaisesRegex(ValueError,'read-only'):
                require_writable_path(self.db,alias)

    def test_shadow_creation_does_not_reinterpret_external_paths(self):
        from retrotv.shadows import ShadowService
        self.add()
        service=ShadowService(self.db,self.config)
        asset=self.db.execute('SELECT id FROM assets LIMIT 1').fetchone()[0]
        self.assertFalse(service.source_folders())
        with self.assertRaisesRegex(ValueError, 'No indexed media'):
            service._source_assets(asset_id=asset)
        with self.assertRaisesRegex(ValueError, 'No indexed media'):
            service._source_assets(folder='Season 1')

    def test_channel_deletion_detaches_only_its_external_assignment(self):
        from retrotv.channel_management import ChannelDeletionService
        self.add(assignments=[{'channel':'nickelodeon','scope':'daytime'},{'channel':'fox','scope':'night'}])
        service=ChannelDeletionService(self.db,self.config)
        preview=service.preview('nickelodeon')
        service.delete('nickelodeon',confirmation_text=preview['confirmation_text'],acknowledge_rescan=True)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM assets').fetchone()[0],3)
        self.assertEqual([r[0] for r in self.db.execute('SELECT channel_slug FROM external_assignments')],['fox'])
        self.assertEqual(len(ScheduleBuilder(self.db,self.config)._candidates('fox','night',{'episode'})),3)
        self.assertEqual(len(list(self.series.rglob('*.mp4'))),3)
