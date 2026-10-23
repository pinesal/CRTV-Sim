import io
import json
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

from retrotv.iptv import IPTVService, inspect_url, stream_url, SafeRedirect, select_feed
from retrotv.scheduling import ScheduleBuilder
from retrotv.guide import GuideService
from retrotv.playback import ScheduleResolver
import test_scheduling as fixtures
import test_external_media as external_fixtures


class IPTVTests(unittest.TestCase):
    setUp = fixtures.SchedulingTests.setUp
    tearDown = fixtures.SchedulingTests.tearDown
    add_asset = fixtures.SchedulingTests.add_asset

    def service(self):
        ScheduleBuilder(self.connection,self.config).ensure_channel('fox')
        return IPTVService(self.connection,self.config)

    def add(self, scope='all-day', **extra):
        return self.service().register({'url':'http://127.0.0.1/live.m3u8','direct':True,'name':'Live feed',
            'assignments':[{'channel':'fox','scope':scope}],**extra})['id']

    def generate(self, when=date(2026,7,27)):
        builder = ScheduleBuilder(self.connection,self.config)
        result = builder.generate_day('fox',when)
        self.assertEqual(result.validation_status,'valid')
        return builder, builder.entries(result.generation_id)

    def test_live_only_channel_and_shared_programs(self):
        self.add_asset(1,media_type='episode',daypart='all-day',duration=1200,channel=None,series='Shared show')
        self.add()
        builder, rows = self.generate()
        self.assertTrue(rows)
        self.assertEqual({r['entry_type'] for r in rows},{'iptv_live'})
        self.assertTrue(all(r['asset_id'] is None for r in rows))
        listings=GuideService(self.connection,self.config)._program_listings(rows[0]['generation_id'])
        self.assertTrue(all(r.title=='Live feed' for r in listings))
        target=ScheduleResolver(self.connection,self.config).resolve('fox',datetime.fromisoformat(rows[0]['starts_at'])+timedelta(minutes=1))
        self.assertEqual(target.stream_url,'http://127.0.0.1/live.m3u8')
        self.assertIsNone(target.path)

    def test_local_broad_program_causes_conflict_supporting_media_does_not(self):
        self.add_asset(1,media_type='commercial',daypart='all-day',duration=30)
        self.add('morning')
        _, rows=self.generate()
        self.assertTrue(any(r['entry_type']=='iptv_live' for r in rows))
        self.add_asset(2,media_type='episode',daypart='all-day',duration=1200)
        _, rows=self.generate()
        conflict=[r for r in rows if r['entry_type']=='iptv_conflict']
        self.assertTrue(conflict)
        self.assertIn('placement',json.loads(conflict[0]['explanation_json'])['iptv']['local_conflicts'][0])

    def test_feed_precedence_and_equal_specificity_conflict(self):
        self.add()
        morning=self.add('morning',url='http://127.0.0.1/morning.ts',name='Morning')
        _, rows=self.generate()
        six=next(r for r in rows if datetime.fromisoformat(r['starts_at']).hour==6)
        self.assertEqual(json.loads(six['explanation_json'])['iptv']['feed_id'],morning)
        self.add('morning',url='http://127.0.0.1/other.ts')
        _, rows=self.generate()
        self.assertTrue(any(r['entry_type']=='iptv_conflict' for r in rows))

    def test_daytime_and_custom_scope(self):
        self.config.data['scheduling']['dayparts'][1]['slug']='custom'
        self.add('daytime')
        custom=self.add('custom',url='http://127.0.0.1/custom.ts')
        _,rows=self.generate()
        self.assertTrue(any(json.loads(r['explanation_json']).get('iptv',{}).get('feed_id')==custom for r in rows))

    def test_weekly_feed_owns_window_and_local_special_conflicts(self):
        self.config.data['seasonal']={'windows':[{'slug':'special','channel':'fox','weekdays':['monday'],
            'start':'20:00','end':'22:00','collection':'special','guide_label':'Special'}]}
        self.add()
        special=self.add(url='http://127.0.0.1/special.ts',assignments=[{'channel':'fox','weekly_slug':'special'}])
        _,rows=self.generate()
        window=[r for r in rows if r['programming_owner']=='special']
        self.assertEqual({json.loads(r['explanation_json'])['iptv']['feed_id'] for r in window},{special})
        self.add_asset(3,media_type='episode',daypart='evening',duration=1200,collection_type='weekly',collection_slug='special')
        _,rows=self.generate()
        self.assertTrue(any(r['entry_type']=='iptv_conflict' and r['programming_owner']=='special' for r in rows))

    def test_local_weekly_special_interrupts_and_handoff_card(self):
        self.config.data['seasonal']={'windows':[{'slug':'special','channel':'fox','weekdays':['monday'],
            'start':'20:00','end':'22:00','collection':'special','guide_label':'Special'}]}
        self.add_asset(1,media_type='episode',daypart='evening',duration=1200,collection_type='weekly',collection_slug='special')
        self.add()
        _,rows=self.generate()
        cards=[r for r in rows if json.loads(r['explanation_json']).get('iptv_handoff')]
        self.assertEqual(len(cards),1)
        self.assertEqual(datetime.fromisoformat(cards[0]['starts_at']).strftime('%H:%M:%S'),'19:59:30')
        self.assertTrue(any(r['asset_id']==1 and r['programming_owner']=='special' for r in rows))
        self.assertTrue(any(r['entry_type']=='iptv_live' and datetime.fromisoformat(r['starts_at']).hour==22 for r in rows))

    def test_shared_holiday_exempt_channel_holiday_conflicts(self):
        self.config.data['seasonal']={'holidays':{'halloween':{'start':'10-01','end':'10-31','mode':'boost'}}}
        self.add_asset(1,media_type='episode',daypart='morning',duration=1200,channel=None,collection_type='holiday',collection_slug='halloween')
        self.add()
        _,rows=self.generate(date(2026,10,20))
        self.assertFalse(any(r['entry_type']=='iptv_conflict' for r in rows))
        self.add_asset(2,media_type='episode',daypart='morning',duration=1200,collection_type='holiday',collection_slug='halloween')
        _,rows=self.generate(date(2026,10,20))
        self.assertTrue(any(r['entry_type']=='iptv_conflict' for r in rows))

    def test_assignments_reuse_and_forbidden_channels(self):
        service=self.service()
        ScheduleBuilder(self.connection,self.config).ensure_channel('pbs')
        feed=self.add(assignments=[{'channel':'fox','scope':'morning'},{'channel':'pbs','scope':'all-day'}])
        self.assertEqual(self.connection.execute('SELECT count(*) FROM iptv_feeds').fetchone()[0],1)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM iptv_assignments').fetchone()[0],2)
        for channel in ['weather','guide']:
            ScheduleBuilder(self.connection,self.config).ensure_channel(channel)
            with self.assertRaises(ValueError):
                service.update(feed,{'assignments':[{'channel':channel,'scope':'all-day'}]})
        from retrotv.internet_media import InternetMediaService
        with self.assertRaisesRegex(ValueError,'IPTV'):
            InternetMediaService(self.connection,self.config).configure_channel({'slug':'fox','display_name':'FOX','playback_mode':'watch_at_your_pace'})

    def test_new_channel_removal_and_stable_locked_schedule(self):
        service=self.service()
        payload={'url':'http://lan/live.ts','direct':True,'new_channel':'Live Channel'}
        feed=service.register(payload,tested_selection=select_feed(payload))['id']
        self.assertTrue(self.connection.execute("SELECT enabled FROM channels WHERE slug='live-channel'").fetchone()[0])
        _,rows=self.generate()
        gen=rows[0]['generation_id']
        self.connection.execute('UPDATE schedule_generations SET locked=1 WHERE id=?',(gen,));self.connection.commit()
        self.service().remove(feed)
        builder=ScheduleBuilder(self.connection,self.config)
        self.assertTrue(builder.generation_is_current('fox',date(2026,7,27)))
        with self.assertRaisesRegex(ValueError,'locked'):
            builder.generate_day('fox',date(2026,7,27))

    def test_new_channel_requires_test_before_any_database_or_folder_changes(self):
        service=self.service()
        payload={'url':'http://lan/live.ts','direct':True,'new_channel':'Untested Channel'}
        with patch('retrotv.media_folders.ensure_library_scaffold') as scaffold:
            with self.assertRaisesRegex(ValueError,'Test this feed successfully'):
                service.register(payload)
            with self.assertRaisesRegex(ValueError,'selected feed changed'):
                service.register(payload,tested_selection=select_feed({**payload,'url':'http://other/live.ts'}))
            scaffold.assert_not_called()
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM iptv_sources').fetchone()[0],0)
        self.assertIsNone(self.connection.execute("SELECT id FROM channels WHERE slug='untested-channel'").fetchone())

    def test_disabling_invalidates_only_affected_channel(self):
        feed=self.add()
        builder,rows=self.generate()
        self.assertTrue(builder.generation_is_current('fox',date(2026,7,27)))
        self.service().update(feed,{'enabled':False})
        self.assertFalse(ScheduleBuilder(self.connection,self.config).generation_is_current('fox',date(2026,7,27)))

    def test_channel_shadow_of_shared_target_counts_but_shared_shadow_does_not(self):
        self.add_asset(1,media_type='episode',daypart='all-day',duration=1200,channel=None,series='Shared')
        self.add()
        self.connection.execute('''INSERT INTO shadow_files(shadow_relative_path,target_asset_id,target_relative_path,
            media_type,channel_slug,daypart_slug,file_size,modified_ns,status,first_seen_at,last_seen_at)
            VALUES ('fox/morning/episodes/Shared/one.shadow',1,'shared/all-day/episodes/one.mp4',
            'episode','fox','morning',1,1,'ready',?,?)''',(self.now,self.now))
        self.connection.execute("INSERT INTO shadow_eligibility(shadow_id,channel_slug,daypart_slug,eligibility_source) VALUES (1,'fox','morning','test')")
        self.connection.commit()
        _,rows=self.generate()
        self.assertTrue(any(r['entry_type']=='iptv_conflict' for r in rows))
        self.connection.execute('UPDATE shadow_files SET channel_slug=NULL WHERE id=1')
        self.connection.execute("UPDATE shadow_eligibility SET channel_slug='*' WHERE shadow_id=1")
        self.connection.commit()
        _,rows=self.generate()
        self.assertFalse(any(r['entry_type']=='iptv_conflict' for r in rows))

    def test_recurring_progress_and_slots_survive_live_conflict(self):
        self.add_asset(1,media_type='episode',daypart='all-day',duration=1200,series='Regular')
        self.add_asset(2,media_type='episode',daypart='all-day',duration=1200,series='Regular')
        builder=ScheduleBuilder(self.connection,self.config)
        builder.update_schedule_preferences('fox',recurring_enabled=True,unique_weekends=True)
        first=builder.generate_day('fox',date(2026,7,27))
        before=json.loads(self.connection.execute('SELECT state_json FROM recurring_lineup_snapshots WHERE generation_id=?',(first.generation_id,)).fetchone()[0])
        feed=self.add()
        second=ScheduleBuilder(self.connection,self.config).generate_day('fox',date(2026,7,28))
        after=json.loads(self.connection.execute('SELECT state_json FROM recurring_lineup_snapshots WHERE generation_id=?',(second.generation_id,)).fetchone()[0])
        self.assertEqual(after['progress'],before['progress'])
        self.assertEqual(after['slots'],before['slots'])
        self.service().update(feed,{'enabled':False})
        third=ScheduleBuilder(self.connection,self.config).generate_day('fox',date(2026,7,29))
        self.assertTrue(any(r['asset_id'] for r in ScheduleBuilder(self.connection,self.config).entries(third.generation_id)))

    def test_scan_keeps_iptv_only_channel_enabled_and_delete_preserves_feed(self):
        feed=self.add()
        from retrotv.scanner import LibraryScanner
        self.config.storage_root('media').mkdir(parents=True,exist_ok=True)
        LibraryScanner(self.connection,self.config)._sync_channels_from_folders(self.config.storage_root('media'))
        self.assertTrue(self.connection.execute("SELECT enabled FROM channels WHERE slug='fox'").fetchone()[0])
        self.connection.execute("DELETE FROM channels WHERE slug='fox'");self.connection.commit()
        self.assertEqual(self.connection.execute('SELECT count(*) FROM iptv_assignments').fetchone()[0],0)
        self.assertEqual(self.service()._feed(feed)['id'],feed)

    def test_midnight_handoff_created_after_next_day_guide_without_rewriting_locked(self):
        self.config.data['seasonal']={'windows':[{'slug':'late','channel':'fox','weekdays':['monday'],
            'start':'22:00','end':'00:00','collection':'late','guide_label':'Late'}]}
        self.add_asset(1,media_type='episode',daypart='all-day',duration=1200,series='Regular')
        self.add(assignments=[{'channel':'fox','weekly_slug':'late'}])
        builder,rows=self.generate(date(2027,1,4))
        gen=rows[0]['generation_id']
        builder.generate_day('fox',date(2027,1,5))
        cards=[r for r in builder.entries(gen) if json.loads(r['explanation_json']).get('iptv_midnight')]
        self.assertEqual(len(cards),1)
        self.assertEqual(datetime.fromisoformat(cards[0]['starts_at']).strftime('%H:%M:%S'),'23:59:30')
        opportunity=self.connection.execute('SELECT facts_json FROM announcement_opportunities WHERE schedule_entry_id=?',(cards[0]['id'],)).fetchone()
        self.assertEqual(json.loads(opportunity[0])['listings'][0]['title'],'Regular')
        self.connection.execute('UPDATE schedule_generations SET locked=1 WHERE id=?',(gen,));self.connection.commit()
        before=[dict(r) for r in builder.entries(gen)]
        builder.generate_day('fox',date(2027,1,5))
        self.assertEqual([dict(r) for r in builder.entries(gen)],before)

    def test_playlist_reorder_url_change_removal_and_failure(self):
        catalog={'kind':'playlist','feeds':[{'key':'id:one','name':'One','url':'http://lan/one.ts'}]}
        with patch('retrotv.iptv.inspect_url',return_value=catalog):
            feed=self.service().register({'url':'http://lan/list.m3u','selection_key':'id:one'})['id']
        catalog['feeds']=[{'key':'id:two','name':'Two','url':'http://lan/two.ts'},
                          {'key':'id:one','name':'One','url':'http://lan/changed.ts'}]
        with patch('retrotv.iptv.inspect_url',return_value=catalog):
            self.service().refresh(feed)
        self.assertEqual(self.service().playable_url(feed),'http://lan/changed.ts')
        with patch('retrotv.iptv.inspect_url',side_effect=OSError('offline')):
            self.service().refresh(feed)
        self.assertEqual(self.service().playable_url(feed),'http://lan/changed.ts')
        catalog['feeds']=catalog['feeds'][:1]
        with patch('retrotv.iptv.inspect_url',return_value=catalog):
            self.service().refresh(feed)
        with self.assertRaisesRegex(ValueError,'Stream not found'):
            self.service().playable_url(feed)

    def test_corrupt_or_ambiguous_registration_is_not_saved(self):
        with patch('retrotv.iptv.inspect_url',return_value={'kind':'playlist','feeds':[
            {'key':'id:one','name':'A','url':'http://lan/a.ts'},{'key':'id:one','name':'B','url':'http://lan/b.ts'}]}):
            with self.assertRaises(ValueError):
                self.service().register({'url':'http://lan/list.m3u','selection_key':'id:one'})
        self.assertEqual(self.connection.execute('SELECT count(*) FROM iptv_feeds').fetchone()[0],0)

    def test_deleted_feed_identity_is_not_reused_by_unrelated_stream(self):
        feed=self.add()
        builder,rows=self.generate()
        self.service().remove(feed)
        replacement=self.add(url='http://127.0.0.1/unrelated.ts')
        self.assertNotEqual(replacement,feed)
        with patch('retrotv.playback.ensure_continuity_slate',return_value=self.temporary/'slate.mp4'):
            target=ScheduleResolver(self.connection,self.config).resolve('fox',datetime.fromisoformat(rows[0]['starts_at'])+timedelta(minutes=1))
        self.assertIsNone(target.stream_url)
        self.assertEqual(target.entry_type,'channel_fallback')

    def test_cancelled_refresh_does_not_publish_new_metadata(self):
        import threading
        feed=self.add();stop=threading.Event()
        def cancelled(*args,**kwargs):
            stop.set()
            return {'kind':'direct','feeds':[{'key':'direct','name':'Changed','url':'http://lan/changed.ts'}]}
        with patch('retrotv.iptv.inspect_url',side_effect=cancelled):
            result=self.service().refresh(feed,stop_event=stop)
        self.assertTrue(result['cancelled'])
        self.assertEqual(self.service().playable_url(feed),'http://127.0.0.1/live.m3u8')


class ExternalIPTVTests(unittest.TestCase):
    setUp=external_fixtures.ExternalMediaTests.setUp
    tearDown=external_fixtures.ExternalMediaTests.tearDown
    add=external_fixtures.ExternalMediaTests.add

    def test_external_assignment_causes_conflict_without_changing_source_files(self):
        self.add('morning')
        before={p.relative_to(self.series):p.read_bytes() for p in self.series.rglob('*') if p.is_file()}
        IPTVService(self.db,self.config).register({'url':'http://lan/live.ts','direct':True,
            'assignments':[{'channel':'nickelodeon','scope':'morning'}]})
        builder=ScheduleBuilder(self.db,self.config)
        gen=builder.generate_day('nickelodeon',date(2026,7,27))
        self.assertTrue(any(r['entry_type']=='iptv_conflict' for r in builder.entries(gen.generation_id)))
        self.assertEqual(before,{p.relative_to(self.series):p.read_bytes() for p in self.series.rglob('*') if p.is_file()})


class PlaylistTests(unittest.TestCase):
    def inspect(self, text, url='http://lan/channels/list.m3u'):
        response=io.BytesIO(text.encode());response.url=url;response.headers={'Content-Type':'application/x-mpegURL'}
        with patch('retrotv.iptv.build_opener') as opener:
            opener.return_value.open.return_value=response
            return inspect_url(url)

    def test_hls_manifest_is_one_feed(self):
        for text in ['#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=10\nquality.m3u8',
                     '#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\nsegment.ts']:
            result=self.inspect(text)
            self.assertEqual(result['kind'],'direct')
            self.assertEqual(len(result['feeds']),1)

    def test_relative_urls_and_options_ignored(self):
        result=self.inspect('#EXTM3U\n#EXTINF:-1 tvg-id="one",Channel one\n#EXTVLCOPT:http-referrer=bad\n../live.ts')
        self.assertEqual(result['feeds'][0],{'key':'id:one','name':'Channel one','url':'http://lan/live.ts','ambiguous':False})

    def test_reject_unsupported_credentials_and_oversized(self):
        for value in ['file:///c:/secret','http://user:pass@lan/live','rtsp://lan/live','http://lan/\ncommand']:
            with self.assertRaises(ValueError):stream_url(value)
        with self.assertRaises(ValueError):self.inspect('#EXTM3U\nfile:///secret')
        with self.assertRaises(ValueError):self.inspect('#EXTM3U\n'+'#'*2_000_001)
        with self.assertRaises(ValueError):SafeRedirect().redirect_request(None,None,302,'',{},'file:///secret')

    def test_ambiguous_ids_visible(self):
        result=self.inspect('#EXTM3U\n#EXTINF:-1 tvg-id="a",A\na.ts\n#EXTINF:-1 tvg-id="a",B\nb.ts')
        self.assertTrue(all(f['ambiguous'] for f in result['feeds']))

    def test_mixed_playlist_skips_unsupported_entries_and_keeps_metadata_separate(self):
        result=self.inspect('#EXTM3U\n#EXTINF:-1 tvg-id="one",First\nfirst.ts\n'
            '#EXTINF:-1 tvg-id="unsupported",Unsupported\nrtsp://lan/live\n'
            '#EXTINF:-1 tvg-id="login",Login\nhttps://user:secret@lan/live\n'
            '#EXTINF:-1 tvg-id="legacy",Legacy\nrtmp://lan/live\n'
            'second.ts\n#EXTINF:-1 tvg-id="three",Third\nthird.m3u8')
        self.assertEqual([f['key'] for f in result['feeds']],
            ['id:one','url:http://lan/channels/second.ts','id:three'])
        self.assertEqual(len(result['skipped_entries']),3)
        self.assertNotIn('secret',json.dumps(result))
        self.assertEqual(result['feeds'][1]['name'],'http://lan/channels/second.ts')

    def test_all_unsupported_entries_report_playlist_problem_not_input_url(self):
        with self.assertRaisesRegex(ValueError,'no supported HTTP/HTTPS feeds.*2 unsupported'):
            self.inspect('#EXTM3U\nrtsp://lan/live\nfile:///secret')

if __name__=='__main__': unittest.main()
