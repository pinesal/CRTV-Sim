import io
import json
import shutil
import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from retrotv.breakpoints import BreakpointAnalyzer
from retrotv.config import AppConfig
from retrotv.db import apply_migrations, ensure_storage_root
from retrotv.duplicates import DuplicateService
from retrotv.holiday_review import HolidayReviewService
from retrotv.library_actions import action_counts
from retrotv.shadows import ShadowService
from retrotv.web import INDEX_HTML, make_handler


class LibraryActionCountsTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(__file__).resolve().parents[1]
        self.config = AppConfig(self.project / 'unused.toml', self.project, {
            'application': {'database_path': ':memory:'},
            'storage_roots': {'media': str(self.project / '.test-tmp' / 'unused-media')},
        })
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        apply_migrations(self.connection, self.project / 'migrations')
        self.root_id = ensure_storage_root(self.connection, 'media')

    def asset(self, asset_id, *, review='ready', integrity='ready', duration=1200):
        self.connection.execute('''INSERT INTO assets(id,source_root_id,source_relative_path,
            original_filename,file_size,modified_ns,media_type,review_status,integrity_status,
            enabled_for_scheduling,first_seen_at,last_seen_at)
            VALUES(?,?,?, ?,100,200,'episode',?,?,1,'now','now')''',
            (asset_id, self.root_id, f'fox/night/episodes/Show/{asset_id}.mp4',
             f'{asset_id}.mp4', review, integrity))
        self.connection.execute('INSERT INTO asset_media_info(asset_id,duration_seconds) VALUES(?,?)',
                                (asset_id, duration))

    def counts(self):
        return {key: value['count'] for key, value in action_counts(self.connection, self.config)['counts'].items()}

    def test_empty_library_has_explicit_zeroes(self):
        with patch.object(Path, 'resolve', side_effect=AssertionError('Heading counts must not resolve network paths')):
            result = self.counts()
        self.assertEqual(len(result), 9)
        self.assertEqual(set(result.values()), {0})

    def test_media_count_is_union_and_inventory_matches_its_own_review_definition(self):
        for asset_id in range(1, 6):
            self.asset(asset_id, review='open' if asset_id == 1 else 'ready',
                       integrity='missing' if asset_id == 5 else 'ready')
        for asset_id in (1, 2, 5):
            self.connection.execute("INSERT INTO normalization_profiles(asset_id,analysis_status) VALUES(?,'review')", (asset_id,))
        for asset_id in (1, 2, 3):
            self.connection.execute("""INSERT INTO audio_normalization_profiles(asset_id,source_signature,analysis_status)
                VALUES(?,'test','review')""", (asset_id,))
        self.connection.execute("""INSERT INTO commercial_duration_reviews(asset_id,source_signature,
            duration_seconds,threshold_seconds,decision,updated_at) VALUES(1,'test',600,180,'pending','now')""")
        self.connection.execute("""INSERT INTO shadow_files(shadow_relative_path,target_relative_path,
            file_size,modified_ns,status,first_seen_at,last_seen_at)
            VALUES('fox/missing.shadow','gone.mp4',1,1,'review','now','now')""")
        result = self.counts()
        self.assertEqual(result['library-review-count'], 3)  # 1 has three reasons, counted once
        self.assertEqual(result['inventory-review-count'], 2)  # Library + shadow, not aspect/audio
        self.assertEqual(result['shadow-review-count'], ShadowService(self.connection, self.config).inventory()['summary']['review'])

    def test_duplicate_sets_match_review_service_including_changed_membership(self):
        for asset_id in range(1, 10):
            self.asset(asset_id, integrity='missing' if asset_id == 9 else 'ready')
            group = 'a' if asset_id <= 3 else 'b' if asset_id <= 5 else 'c' if asset_id <= 7 else 'd'
            self.connection.execute("INSERT INTO asset_content_hashes VALUES(?,?,?,?,?,?,?)",
                                    (asset_id, group * 64, group * 64, 100, 100, 200, 'now'))
        # a acquired another copy since review; b is intentional; d has only one current member.
        self.connection.executemany('INSERT INTO duplicate_reviews VALUES(?,?,?,?)',
            [('a' * 64, 'intentional', 2, 'now'), ('b' * 64, 'intentional', 2, 'now')])
        result = self.counts()
        self.assertEqual(result['duplicate-review-count'], 2)
        self.assertEqual(result['duplicate-review-count'], DuplicateService(self.connection, self.config).groups()['pending'])
        DuplicateService(self.connection, self.config).review('a' * 64, 'intentional')
        self.assertEqual(self.counts()['duplicate-review-count'], 1)

    def test_counts_are_unpaginated_read_only_and_ignore_schedule_tables(self):
        for asset_id in range(1, 1003):
            self.asset(asset_id, review='open')
            self.connection.execute("INSERT INTO holiday_filename_reviews VALUES(?,'halloween',?,'spooky','pending','now')",
                                    (asset_id, f'{asset_id}.mp4'))
        self.connection.execute("UPDATE holiday_filename_reviews SET status='dismissed' WHERE asset_id=1")
        writes = self.connection.total_changes
        queries = []
        self.connection.set_trace_callback(queries.append)
        result = self.counts()
        self.connection.set_trace_callback(None)
        self.assertEqual(result['library-review-count'], 1002)
        self.assertEqual(result['holiday-review-count'], 1001)
        self.assertEqual(result['holiday-review-count'], len(HolidayReviewService(self.connection, self.config).pending()))
        self.assertEqual(self.connection.total_changes, writes)
        self.assertFalse(any('schedule_entries' in query or 'recurring_' in query for query in queries))

    def test_act_break_counts_programs_not_cuts_and_separates_automatic_work(self):
        analyzer = BreakpointAnalyzer(None, self.config)
        signature = analyzer._signature({'file_size': 100, 'modified_ns': 200})
        for asset_id in range(1, 8):
            self.asset(asset_id, review='open' if asset_id == 6 else 'ready',
                       duration=600 if asset_id == 7 else 1200)
            if asset_id != 1:
                self.connection.execute("""INSERT INTO asset_break_analyses(asset_id,source_signature,status,refresh_error)
                    VALUES(?,?,?,?)""", (asset_id, 'old-policy' if asset_id == 4 else signature,
                                         'error' if asset_id == 3 else 'analyzed', 'retry' if asset_id == 5 else None))
        self.connection.executemany("""INSERT INTO asset_break_candidates(asset_id,offset_ms,method,score,selected,created_at)
            VALUES(?,?,?,?,1,'now')""", [(2, 400000, 'midpoint', .2), (2, 800000, 'scene_fallback', .4),
                                       (3, 500000, 'quiet_pause', .5), (4, 500000, 'still_card', .7),
                                       (6, 500000, 'midpoint', .2), (7, 200000, 'midpoint', .2)])
        result = self.counts()
        self.assertEqual(result['break-analysis-count'], 4)  # absent, error, stale revision, failed refresh
        self.assertEqual(result['break-fallback-count'], 2)  # multiple cuts on #2 are one program

    def test_external_outages_exclude_disconnected_sources_and_initial_scans(self):
        for index, (enabled, availability, status, error) in enumerate([
            (1, 'unavailable', 'error', 'offline'), (0, 'unavailable', 'error', 'offline'),
            (1, 'unknown', 'pending', None), (1, 'available', 'error', 'bad file'),
            (1, 'available', 'complete', None)], 2):
            self.connection.execute("INSERT INTO storage_roots(id,logical_name,source_kind,enabled) VALUES(?,?,'external',?)",
                                    (index, f'external-{index}', enabled))
            self.connection.execute("INSERT INTO external_sources(root_id,series_title,availability,scan_status,scan_error) VALUES(?,'Show',?,?,?)",
                                    (index, availability, status, error))
        self.assertEqual(self.counts()['external-action-count'], 2)
        self.connection.execute("INSERT INTO channels(slug,display_name) VALUES('weather','RetroWeather')")
        self.connection.execute("INSERT INTO external_assignments(root_id,channel_slug,scope) VALUES(6,'weather','all-day')")
        self.assertEqual(self.counts()['external-action-count'], 3)
        self.connection.execute("UPDATE external_assignments SET enabled=0")
        self.assertEqual(self.counts()['external-action-count'], 2)

    def test_count_endpoint_does_not_build_inventory(self):
        self.asset(1, review='open')
        self.connection.commit()
        handler = object.__new__(make_handler(self.config))
        handler.path = '/api/library/action-counts?limit=1&search=irrelevant'
        handler.command = 'GET'
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        with patch('retrotv.web.connect_database', return_value=self.connection), \
                patch('retrotv.web._media_inventory', side_effect=AssertionError('Expensive inventory called')):
            handler.do_GET()
        self.assertEqual(json.loads(handler.wfile.getvalue())['counts']['library-review-count']['count'], 1)


class LibraryActionHeadingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required for UI behavior check')
    def test_live_badges_refresh_after_actions_keep_filters_and_handle_failure_and_races(self):
        start = INDEX_HTML.index('const panelCountIds =')
        end = INDEX_HTML.index('function notice(', start)
        script = r'''
          const assert=require('assert');
          function element(){const classes=new Set();return {textContent:'…',dataset:{},title:'',
            setAttribute(name,value){this[name]=value},classList:{contains:n=>classes.has(n),
              toggle(n,on){on?classes.add(n):classes.delete(n)},add:n=>classes.add(n),remove:n=>classes.delete(n)}}}
          const elements={};const $=id=>elements[id] ||= element();
          const document={hidden:false};let scheduled;
          const setTimeout=fn=>{scheduled=fn;return 1},clearTimeout=()=>{scheduled=null};
          let payload,broken=false,release;const calls=[];
          async function fetch(path){calls.push(path);if(broken)throw Error('offline');
            if(release!==undefined)return new Promise(resolve=>{release=resolve});
            return {ok:true,json:async()=>payload};}
        ''' + INDEX_HTML[start:end] + r'''
          (async()=>{
            payload={pending:42,groups:[]};await api('/api/duplicates?filter=intentional');
            assert.equal($('duplicate-review-count').textContent,'42 review');
            assert.ok($('duplicate-review-count').classList.contains('has-pending'));
            payload={counts:{'duplicate-review-count':{count:0,label:'review',detail:'sets'}}};
            await loadPanelCounts();assert.equal($('duplicate-review-count').textContent,'0 review');
            assert.ok(!$('duplicate-review-count').classList.contains('has-pending'));
            // Failed refresh retains the previous number; a never-loaded count is unknown, not zero.
            broken=true;await loadPanelCounts();
            assert.equal($('duplicate-review-count').textContent,'0 review');
            assert.ok($('duplicate-review-count').classList.contains('count-stale'));
            assert.equal($('shadow-review-count').textContent,'Status unavailable');broken=false;
            // An old request cannot overwrite counts after a mutation.
            release=null;const old=loadPanelCounts();await Promise.resolve();
            queuePanelCounts();release({ok:true,json:async()=>({counts:{'duplicate-review-count':{count:99,label:'review',detail:''}}})});
            await old;assert.equal($('duplicate-review-count').textContent,'0 review');release=undefined;
            payload={counts:{'duplicate-review-count':{count:3,label:'review',detail:'sets'}}};
            await scheduled();assert.equal($('duplicate-review-count').textContent,'3 review');
            assert.ok(!$('duplicate-review-count').classList.contains('count-stale'));
            // Successful review POST queues counts, without reloading tables or their filters.
            scheduled=null;payload={ok:true};await api('/api/duplicates/hash',{method:'POST'});
            assert.equal(typeof scheduled,'function');
            scheduled=null;await api('/api/assets/1/act-break-workshop',{method:'POST',body:'{"action":"frame"}'});
            assert.equal(scheduled,null);
            document.hidden=true;const before=calls.length;await loadPanelCounts();assert.equal(calls.length,before);
            document.hidden=false;
            updatePanelCounts('/api/weather-graphics',{slots:[{assets:[]},{assets:[{valid:true},{valid:false}]}]});
            assert.equal($('weather-action-count').textContent,'1 invalid files');
            updatePanelCounts('/api/announcement-profiles',{items:[
              {generic:{usable:true,status:'failed'}},{generic:{usable:false,status:'failed'}},
              {generic:{usable:false,status:'missing'}},
              {daypart_slug:'morning',override_enabled:false,generic:{usable:false,status:'failed'}}]});
            assert.equal($('announcer-action-count').textContent,'1 recording issues');
          })().catch(error=>{console.error(error);process.exitCode=1});
        '''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True,
                                encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_badges_are_in_summaries_and_do_not_start_at_a_false_zero(self):
        import re
        ids = ['external-action-count', 'library-review-count', 'inventory-review-count',
               'bumper-review-count', 'shadow-review-count', 'duplicate-review-count',
               'holiday-review-count', 'break-analysis-count', 'break-fallback-count',
               'graphics-action-count', 'weather-action-count', 'announcer-action-count']
        for badge in ids:
            self.assertRegex(INDEX_HTML, rf'<summary>[^\n]*id="{re.escape(badge)}"[^\n]*>…</span>')
        self.assertIn('setInterval(() => { if (!document.hidden) loadPanelCounts(); }, 60000)', INDEX_HTML)


if __name__ == '__main__':
    unittest.main()
