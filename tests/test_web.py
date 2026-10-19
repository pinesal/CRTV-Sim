import json
import sqlite3
import shutil
import subprocess
import unittest
import uuid
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from retrotv.config import AppConfig
from retrotv.web import (
    DASHBOARD_HTML, INDEX_HTML, INTERNET_MEDIA_HTML, SETTINGS_HTML, _asset_query, _media_inventory,
    _inventory_reviews, forget_missing_asset_record, serve,
)


def add_storage_fixture(connection):
    """Include physical-root ownership in otherwise minimal inventory fixtures."""
    connection.execute("ALTER TABLE assets ADD COLUMN source_root_id INTEGER DEFAULT 1")
    connection.executescript("""
        CREATE TABLE storage_roots(id INTEGER PRIMARY KEY,logical_name TEXT,source_kind TEXT,absolute_path TEXT,enabled INTEGER);
        INSERT INTO storage_roots VALUES(1,'media','managed',NULL,1);
        CREATE TABLE external_sources(root_id INTEGER,availability TEXT);
        CREATE TABLE external_assignments(id INTEGER,root_id INTEGER,channel_slug TEXT,scope TEXT,enabled INTEGER);
    """)


class WebLibraryTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required for Dashboard behavior check')
    def test_inventory_shows_cache_during_refresh_and_preserves_it_on_failure(self):
        start = INDEX_HTML.index('function showInventory(data)')
        end = INDEX_HTML.index('async function openInventoryReview(item)', start)
        script = r'''
          const assert=require('assert');
          function element(){return {value:'',children:[],append(...nodes){this.children.push(...nodes)},
            replaceChildren(...nodes){this.children=nodes}};}
          const elements={};const $=name=>elements[name] ||= element();
          function Option(text,value){return {text,value};}
          function card(){return element();}
          function inventoryTypeLabel(value){return value;}
          let rendered=0,inventoryData=null,timerDelay;
          function renderInventory(){rendered++;}
          function loadInventoryReviews(){}
          const setTimeout=(_fn,delay)=>{timerDelay=delay;return 1},clearTimeout=()=>{};
          const calls=[];let release,broken=false;
          const original={summary:{review:15},media_types:['episode'],channels:['nick'],items:[{title:'Old show'}],
            cache:{ready:true,revision:1,updated_at:'2026-10-15T20:00:00Z',stale:true,refresh_due:true,refreshing:false}};
          const fresh={...original,items:[{title:'New show'}],cache:{...original.cache,revision:2,updated_at:'2026-10-15T20:01:00Z',stale:false,refresh_due:false}};
          async function api(path){
            calls.push(path);
            if(broken)throw Error('network unavailable');
            if(path==='/api/inventory?refresh=auto')return new Promise(resolve=>release=resolve);
            if(path==='/api/inventory?refresh=1')return fresh;
            return original;
          }
        ''' + INDEX_HTML[start:end] + r'''
          (async()=>{
            const pending=loadInventory();
            await Promise.resolve();await Promise.resolve();
            assert.strictEqual(rendered,1);
            assert.strictEqual(inventoryData.items[0].title,'Old show');
            assert.ok($('inventory-status').textContent.includes('Refreshing'));
            assert.strictEqual($('inventory-type').value,'');
            $('inventory-type').value='episode';$('inventory-channel').value='nick';
            release(fresh);await pending;
            assert.strictEqual(inventoryData.items[0].title,'New show');
            assert.strictEqual($('inventory-type').value,'episode');
            assert.strictEqual($('inventory-channel').value,'nick');
            assert.ok($('inventory-status').textContent.includes('Last updated'));
            assert.strictEqual(timerDelay,30000);
            const before=rendered;
            await loadInventory(true);
            assert.strictEqual(calls.at(-1),'/api/inventory?refresh=1');
            assert.strictEqual(rendered,before);
            broken=true;await loadInventory(true);
            assert.strictEqual(inventoryData.items[0].title,'New show');
            assert.strictEqual(rendered,before);
            assert.ok($('inventory-status').textContent.includes('last successful results'));
            assert.ok($('inventory-status').textContent.includes('Last updated'));
          })().catch(error=>{console.error(error);process.exitCode=1});
        '''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True,
                                encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_inventory_review_list_counts_files_instead_of_reasons_and_includes_broken_records(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.executescript("""
            CREATE TABLE assets(id INTEGER PRIMARY KEY,original_filename TEXT,
                display_title TEXT,source_relative_path TEXT,channel_slug TEXT,
                review_status TEXT,integrity_status TEXT,media_type TEXT);
            CREATE TABLE review_items(id INTEGER PRIMARY KEY,asset_id INTEGER,
                status TEXT,reason_code TEXT,detail TEXT);
            CREATE TABLE shadow_files(id INTEGER PRIMARY KEY,shadow_relative_path TEXT,
                channel_slug TEXT,detail TEXT,status TEXT);
            INSERT INTO assets VALUES
                (1,'long.mp4','Long ad','shared/all-day/commercials/long.mp4',NULL,'open','ready','commercial'),
                (2,'unknown.mp4',NULL,'fox/bad/unknown.mp4','fox','open','error',NULL),
                (3,'missing.mp4',NULL,'fox/night/missing.mp4','fox','open','missing',NULL),
                (4,'ready.mp4','Ready','fox/night/ready.mp4','fox','ready','ready','episode');
            INSERT INTO review_items VALUES
                (1,1,'open','long_commercial','Commercial is 10 minutes; threshold is 3 minutes'),
                (2,2,'open','unknown_daypart','Unknown daypart: bad'),
                (3,2,'open','probe_failed','Could not decode video'),
                (4,2,'resolved','old_reason','Old reason must not appear'),
                (5,4,'open','stale_reason','Ready assets are not counted');
            INSERT INTO shadow_files VALUES
                (1,'nick/night/episodes/missing.mp4.shadow','nick','Target media was not found','review'),
                (2,'nick/night/episodes/ready.mp4.shadow','nick',NULL,'ready');
        """)
        result = _inventory_reviews(connection)
        self.assertEqual((result['total'], result['library_count'], result['shadow_count']), (4, 3, 1))
        counted = connection.execute("""SELECT
            (SELECT COUNT(*) FROM assets WHERE review_status='open') +
            (SELECT COUNT(*) FROM shadow_files WHERE status='review')""").fetchone()[0]
        self.assertEqual(result['total'], counted)
        items = {(item['kind'], item['id']): item for item in result['items']}
        self.assertIn('Long commercial', items[('asset', 1)]['category'])
        self.assertEqual(items[('asset', 2)]['reasons'],
                         ['Unknown daypart: bad', 'Could not decode video'])
        self.assertEqual(items[('asset', 2)]['title'], 'unknown.mp4')
        self.assertIn('no recorded reason', items[('asset', 3)]['reasons'][0])
        self.assertEqual(items[('shadow', 1)]['reasons'], ['Target media was not found'])
        connection.execute("UPDATE assets SET review_status='ready'")
        connection.execute("UPDATE shadow_files SET status='ready'")
        empty = _inventory_reviews(connection)
        self.assertEqual(empty, {'items': [], 'total': 0, 'library_count': 0, 'shadow_count': 0})

    @unittest.skipUnless(shutil.which('node'), 'Node required for Dashboard behavior check')
    def test_inventory_review_card_opens_complete_list_and_routes_to_correct_review(self):
        card_start = INDEX_HTML.index('function card(label, value,')
        card_end = INDEX_HTML.index('async function loadStats()', card_start)
        start = INDEX_HTML.index('function showInventory(data)')
        end = INDEX_HTML.index('let shadowData = null;', start)
        script = r'''
          const assert=require('assert');
          const setTimeout=()=>1,clearTimeout=()=>{};
          function element(tag){return {tag,children:[],value:'',hidden:true,dataset:{},
            append(...nodes){this.children.push(...nodes)},
            replaceChildren(...nodes){this.children=nodes},focus(){this.focused=true},
            scrollIntoView(){this.scrolled=true},querySelector(){return count}};}
          const elements={};const $=name=>elements[name] ||= element(name);
          const sections={'media-library':element('section'),'shadow-files':element('section')};
          const document={createElement:element,querySelector(selector){return sections[selector.match(/data-section="([^"]+)"/)[1]]}};
          function Option(text,value){return {text,value};}
          function addCell(row,text){const cell=element('td');cell.textContent=text;row.append(cell);return cell;}
          function notice(text,kind){if(kind==='error')throw Error(text);}
          function renderInventory(){}
          let inventoryData,selectedId,loaded=0,shadowsLoaded=0,fail=false;
          async function load(){loaded++;selectedId=$('search').value;}
          async function loadShadows(){shadowsLoaded++;}
          const count=element('strong');
          const items=[{kind:'asset',id:5,title:'Ad',path:'shared/all-day/commercials/ad.mp4',category:'Long commercial',reasons:['Longer than 3 minutes']},
            {kind:'shadow',id:9,title:'gone.mp4.shadow',path:'nick/night/gone.mp4.shadow',category:'Shadow review',reasons:['Target media was not found']}];
          async function api(path){
            if(path==='/api/inventory')return {summary:{review:2},media_types:[],channels:[]};
            assert.strictEqual(path,'/api/inventory/reviews');
            if(fail)throw Error('offline');
            return {items,total:2,library_count:1,shadow_count:1};
          }
        ''' + INDEX_HTML[card_start:card_end] + INDEX_HTML[start:end] + r'''
          (async()=>{
            await loadInventory();
            const button=$('inventory-stats').children[6];
            assert.strictEqual(button.tag,'button');assert.strictEqual(button.type,'button');
            await button.onclick();
            assert.strictEqual($('inventory-reviews-panel').hidden,false);
            assert.strictEqual($('inventory-reviews-panel').focused,true);
            assert.strictEqual(count.textContent,2);
            const rows=$('inventory-reviews').children;assert.strictEqual(rows.length,2);
            assert.strictEqual(rows[1].children[0].children[1].textContent,items[1].path);
            assert.strictEqual(rows[1].children[3].children[0].textContent,'Target media was not found');
            await rows[0].children[4].children[0].onclick();
            assert.strictEqual(selectedId,'id:5');assert.strictEqual($('review').value,'library:open');
            assert.strictEqual(sections['media-library'].open,true);assert.strictEqual(loaded,1);
            await rows[1].children[4].children[0].onclick();
            assert.strictEqual(sections['shadow-files'].open,true);assert.strictEqual(shadowsLoaded,1);
            assert.strictEqual(count.focused,true);
            fail=true;await loadInventoryReviews();
            assert.strictEqual($('inventory-reviews').children.length,0);
            assert.ok($('inventory-reviews-status').textContent.includes('Could not load review items: offline'));
          })().catch(error=>{console.error(error);process.exitCode=1});
        '''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True,
                                encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node required for Dashboard behavior check')
    def test_individual_shadow_delete_previews_exact_file_before_deletion(self):
        start = INDEX_HTML.index('let shadowData = null;')
        end = INDEX_HTML.index('function updateShadowDestination()', start)
        load_start = INDEX_HTML.index('async function loadShadows()')
        load_end = INDEX_HTML.index('function byteSize(', load_start)
        script = r'''
          const assert = require('assert');
          function element(tag) { return {tag,children:[],value:'',dataset:{},hidden:true,
            get options(){return this.children},
            append(...nodes){this.children.push(...nodes)},
            replaceChildren(...nodes){this.children=nodes},
            showModal(){this.open=true},close(){this.open=false}}; }
          const elements = {};
          const $ = name => elements[name] ||= element(name);
          const document = {createElement:element};
          function Option(text,value){return {text,value};}
          function card(){return element('card');}
          function addCell(row,text){const cell=element('td');cell.textContent=text;row.append(cell);return cell;}
          function updateShadowDestination(){}
          let inventoryRefreshes=0;
          async function loadInventory(){inventoryRefreshes++;}
          let reviewRefreshes=0;
          async function loadInventoryReviews(){reviewRefreshes++;}
          function notice(text,kind){if(kind==='error')throw Error(text);}
          const calls=[];
          const items=[
            {id:11,status:'ready',shadow_relative_path:'fox/night/episodes/one.mp4.shadow'},
            {id:12,status:'review',shadow_relative_path:'nick/night/episodes/missing.mp4.shadow',detail:'Target media was not found'}
          ];
          async function api(path,options){
            const payload=options ? JSON.parse(options.body) : null;
            calls.push({path,payload});
            if(path==='/api/shadows')return {items,summary:{total:2,ready:1,review:1},options:{channels:[]},folders:[]};
            if(path==='/api/shadows/deletion-preview')return {shadow_ids:payload.shadow_ids,count:1,token:'preview-token'};
            if(path==='/api/shadows/delete')return {deleted:[items[1].shadow_relative_path],already_missing:[],failed:[]};
            throw Error('Unexpected endpoint: '+path);
          }
        ''' + INDEX_HTML[start:end] + INDEX_HTML[load_start:load_end] + r'''
          (async()=>{
            await loadShadows();
            const rows=$('shadows').children;
            assert.strictEqual(rows.length,2);
            assert.strictEqual(rows[0].children[6].children[0].textContent,'Delete');
            await rows[1].children[6].children[0].onclick();
            assert.strictEqual($('shadow-delete-dialog').open,true);
            assert.ok($('shadow-delete-summary').textContent.includes(items[1].shadow_relative_path));
            assert.deepStrictEqual(calls[1].payload,{shadow_ids:[12]});
            assert.ok(!calls.some(call=>call.path==='/api/shadows/delete'));
            $('inventory-reviews-panel').hidden=false;
            await deleteShadows();
            assert.deepStrictEqual(calls[2],{path:'/api/shadows/delete',payload:{shadow_ids:[12],token:'preview-token'}});
            assert.strictEqual($('shadow-delete-dialog').open,false);
            assert.strictEqual(inventoryRefreshes,1);
            assert.strictEqual(reviewRefreshes,1);
          })().catch(error=>{console.error(error);process.exitCode=1});
        '''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True,
                                encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node required for Dashboard behavior check')
    def test_act_break_titles_link_to_full_video_without_duplicate_series(self):
        start = INDEX_HTML.index('async function loadBreakpoints()')
        end = INDEX_HTML.index('function renderSchedule(', start)
        items = [
            {'id':1,'series_title':'Show','display_title':'S01E01'},
            {'id':2,'series_title':'Show','display_title':'Show — S01E01'},
            {'id':3,'series_title':'Show','display_title':'SHOW - Episode'},
            {'id':4,'series_title':None,'original_filename':'Special.mp4'},
            {'id':5,'series_title':'Show','display_title':'Showdown'},
            {'id':6,'series_title':'Show','display_title':'Show S01E02'},
        ]
        script = r'''
          function element(tag) { return {tag,children:[],append(...nodes){this.children.push(...nodes)},replaceChildren(){this.children=[]}}; }
          const elements = {'breakpoint-filter':{value:''},breakpoints:element('tbody'),'breakpoints-empty':{}};
          const $ = name => elements[name];
          const document = {createElement:element};
          function addCell(row, text) { const cell=element('td');cell.textContent=text;row.append(cell);return cell; }
        ''' + 'const window={};const api=async()=>({items:' + json.dumps(items) + '});\n' + INDEX_HTML[start:end] + r'''
          loadBreakpoints().then(()=>process.stdout.write(JSON.stringify(elements.breakpoints.children.map(row=>row.children[1].children[0]))));
        '''
        result = subprocess.run(['node','-e',script], capture_output=True, text=True, encoding='utf-8', check=True, timeout=15)
        links = json.loads(result.stdout)
        self.assertEqual([link['textContent'] for link in links],
                         ['Show — S01E01','Show — S01E01','SHOW - Episode','Special.mp4','Show — Showdown','Show S01E02'])
        self.assertEqual(links[0]['href'], '/api/assets/1/play')
        self.assertEqual(links[0]['target'], '_blank')

    def test_library_exposes_advisory_holiday_filename_review(self):
        self.assertIn("Holiday Filename Review", INDEX_HTML)
        self.assertIn('id="holiday-move-folder"', INDEX_HTML)
        self.assertIn("/api/holiday-reviews/action", INDEX_HTML)
        self.assertIn("matched_clue", INDEX_HTML)

    def test_internet_media_page_exposes_catalog_playlist_and_position_controls(self):
        self.assertIn("Watch at Your Pace", INTERNET_MEDIA_HTML)
        self.assertIn('id="preview-from"', INTERNET_MEDIA_HTML)
        self.assertIn('id="position-date"', INTERNET_MEDIA_HTML)
        self.assertIn('id="playlists"', INTERNET_MEDIA_HTML)
        self.assertIn("manual_order", INTERNET_MEDIA_HTML)
        self.assertIn("repairEpisode", INTERNET_MEDIA_HTML)
        self.assertIn("/api/internet-media/position", INTERNET_MEDIA_HTML)
        self.assertIn("restoreSelectedOption('source-channel',selectedSourceChannel)", INTERNET_MEDIA_HTML)
        self.assertIn("restoreSelectedOption('position-channel',selectedPositionChannel)", INTERNET_MEDIA_HTML)
        self.assertIn("captureScrollState()", INTERNET_MEDIA_HTML)
        self.assertIn("restoreScrollState(scrollState)", INTERNET_MEDIA_HTML)
        self.assertIn('id="start-beginning"', INTERNET_MEDIA_HTML)
        self.assertIn('id="delete-dialog"', INTERNET_MEDIA_HTML)
        self.assertIn("acknowledge_rescan", INTERNET_MEDIA_HTML)
        self.assertIn("deletion-preview", INTERNET_MEDIA_HTML)

    def test_server_binds_before_runtime_start_callback(self):
        callback = Mock()
        with patch("retrotv.web.ThreadingHTTPServer", side_effect=OSError("busy")):
            with self.assertRaisesRegex(OSError, "busy"):
                serve(Mock(), host="127.0.0.1", port=8765, before_serve=callback)
        callback.assert_not_called()

    def test_library_includes_channel_graphics_manager(self):
        self.assertIn("Channel Graphics", INDEX_HTML)
        self.assertIn("Graphics To Do", INDEX_HTML)
        self.assertIn("placeholder_todo", INDEX_HTML)
        self.assertIn("Generate and use AI graphic", INDEX_HTML)
        self.assertIn('id="graphic-art-direction"', INDEX_HTML)
        self.assertIn("Regenerate", INDEX_HTML)
        self.assertIn("Replace with file", INDEX_HTML)
        self.assertIn("delete-ai", INDEX_HTML)
        self.assertIn("/api/graphics/generate", INDEX_HTML)
        self.assertIn('id="graphic-channel"', INDEX_HTML)
        self.assertIn('id="upload-graphic"', INDEX_HTML)
        self.assertNotIn("Generate image (provider not configured)", INDEX_HTML)

    def test_library_includes_station_announcer_profiles(self):
        self.assertIn("Station Announcer Profiles", INDEX_HTML)
        self.assertIn("/api/announcement-profiles", INDEX_HTML)
        self.assertIn('id="weekly-announcer-profiles"', INDEX_HTML)
        self.assertIn('Show promos for this weekly special', INDEX_HTML)
        self.assertIn('Narration creative direction', INDEX_HTML)
        self.assertIn('className = "announcer-field"', INDEX_HTML)
        self.assertIn("Weekly Specials", INDEX_HTML)
        self.assertIn("Save & generate generic voice", INDEX_HTML)
        self.assertIn("Test announcer text", INDEX_HTML)
        self.assertIn("Test TTS voice", INDEX_HTML)
        self.assertIn("/test`,", INDEX_HTML)

    def test_weekly_promo_controls_live_only_in_library(self):
        self.assertNotIn('id="weekly-promo-controls"', DASHBOARD_HTML)
        self.assertNotIn("loadWeeklyPromoControls()", DASHBOARD_HTML)
        self.assertIn("Show promos for this weekly special", INDEX_HTML)
        self.assertIn("Narration creative direction", INDEX_HTML)

    def test_dashboard_includes_synthetic_weather_diagnostics(self):
        self.assertIn("Weather channel", DASHBOARD_HTML)
        self.assertIn('id="weather-generation"', DASHBOARD_HTML)
        self.assertIn('id="weather-warning"', DASHBOARD_HTML)
        self.assertIn('id="weather-companion"', DASHBOARD_HTML)
        self.assertIn('id="weather-restart"', DASHBOARD_HTML)
        self.assertIn("weather-restart", DASHBOARD_HTML)
        self.assertIn("Open RetroWeather", DASHBOARD_HTML)
        self.assertIn("synthetic_channels?.weather", DASHBOARD_HTML)

    def test_dashboard_exposes_maintenance_window_and_cleanup_controls(self):
        self.assertIn('id="maintenance-window-state"', DASHBOARD_HTML)
        self.assertIn('id="skip-maintenance-window"', DASHBOARD_HTML)
        self.assertIn('id="empty-internet-cache"', DASHBOARD_HTML)
        self.assertIn('id="compact-database"', DASHBOARD_HTML)
        self.assertIn("internet-cache-preview", DASHBOARD_HTML)

    def test_library_includes_exact_duplicate_review(self):
        self.assertIn("Duplicate Media Review", INDEX_HTML)
        self.assertIn("/api/duplicates", INDEX_HTML)
        self.assertIn("Keep all intentionally", INDEX_HTML)
        self.assertIn("Reused duplicate result", DASHBOARD_HTML)

    def test_forget_missing_record_detaches_historical_schedule_entries(self):
        root = Path(__file__).resolve().parents[1] / ".test-tmp" / f"web-{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        connection = None
        try:
            media = root / "media"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.executescript(
                """
                CREATE TABLE assets(
                    id INTEGER PRIMARY KEY, source_relative_path TEXT,
                    integrity_status TEXT, review_status TEXT
                );
                CREATE TABLE schedule_entries(
                    id INTEGER PRIMARY KEY, asset_id INTEGER,
                    media_start_ms INTEGER, media_end_ms INTEGER
                );
                CREATE TABLE normalization_promotions(asset_id INTEGER);
                """
            )
            connection.execute(
                "INSERT INTO assets VALUES (1, 'fox/old.mp4', 'missing', 'ignored')"
            )
            connection.execute(
                "INSERT INTO schedule_entries VALUES (1, 1, 0, 30000)"
            )
            connection.execute("INSERT INTO normalization_promotions VALUES (1)")
            config = AppConfig(
                profile_path=root / "test.toml",
                project_root=root,
                data={"storage_roots": {"media": str(media)}},
            )
            add_storage_fixture(connection)
            self.assertEqual(forget_missing_asset_record(connection, config, 1), 1)
            self.assertIsNone(
                connection.execute("SELECT * FROM assets WHERE id=1").fetchone()
            )
            entry = connection.execute(
                "SELECT asset_id, media_start_ms, media_end_ms FROM schedule_entries"
            ).fetchone()
            self.assertEqual(tuple(entry), (None, None, None))
        finally:
            if connection is not None:
                connection.close()
            shutil.rmtree(root, ignore_errors=True)

    def test_library_includes_programming_inventory(self):
        self.assertIn("Programming Inventory", INDEX_HTML)
        self.assertIn("/api/inventory", INDEX_HTML)
        self.assertIn("Fewer than 5 items", INDEX_HTML)
        self.assertIn("Unplaced recurring series", INDEX_HTML)
        self.assertIn("No upcoming programs", INDEX_HTML)
        self.assertIn("Not scheduled", INDEX_HTML)
        self.assertIn("Recurring Lineup", INDEX_HTML)
        self.assertIn("No recurring slot", INDEX_HTML)
        self.assertIn("Daypart or Collection", INDEX_HTML)
        self.assertIn("/api/inventory/open-folder", INDEX_HTML)
        self.assertIn("Shadow Files", INDEX_HTML)
        self.assertIn("Create shadows from folder", INDEX_HTML)
        self.assertIn("/api/shadows", INDEX_HTML)
        self.assertIn("Delete Shadows", INDEX_HTML)
        self.assertIn("inventory-shadow-row", INDEX_HTML)
        self.assertIn("/api/shadows/deletion-preview", INDEX_HTML)

    def test_inventory_keeps_native_and_shadow_rows_distinct(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE assets(
                id INTEGER PRIMARY KEY, original_filename TEXT, media_type TEXT,
                display_title TEXT, series_title TEXT, channel_slug TEXT,
                daypart_slug TEXT, collection_type TEXT, collection_slug TEXT,
                source_relative_path TEXT, integrity_status TEXT,
                review_status TEXT, enabled_for_scheduling INTEGER
            );
            CREATE TABLE asset_media_info(asset_id INTEGER, duration_seconds REAL);
            CREATE TABLE shadow_files(
                id INTEGER PRIMARY KEY,shadow_relative_path TEXT,target_asset_id INTEGER,
                series_title TEXT,channel_slug TEXT,daypart_slug TEXT,
                collection_type TEXT,collection_slug TEXT,status TEXT
            );
            INSERT INTO assets VALUES(
                1,'one.mp4','episode','One','Doug','nickelodeon','night',NULL,NULL,
                'nickelodeon/night/episodes/Doug/one.mp4','ready','ready',1
            );
            INSERT INTO asset_media_info VALUES(1,1320);
            INSERT INTO shadow_files VALUES(
                9,'nickelodeon/night/episodes/Doug/one.mp4.shadow',1,'Doug',
                'nickelodeon','night',NULL,NULL,'ready'
            );
            """
        )
        add_storage_fixture(connection)
        connection.execute("ALTER TABLE assets ADD COLUMN holiday_weekly_slug TEXT")
        connection.execute("ALTER TABLE shadow_files ADD COLUMN holiday_weekly_slug TEXT")
        rows = [
            item for item in _media_inventory(connection)["items"]
            if item["title"] == "Doug"
        ]
        self.assertEqual({item["row_kind"] for item in rows}, {"native", "shadow"})
        shadow = next(item for item in rows if item["row_kind"] == "shadow")
        self.assertEqual(shadow["shadow_ids"], [9])
        connection.close()

    def test_inventory_groups_series_by_scheduling_location(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE assets(
                id INTEGER PRIMARY KEY, original_filename TEXT, media_type TEXT,
                display_title TEXT, series_title TEXT, channel_slug TEXT,
                daypart_slug TEXT, collection_type TEXT, collection_slug TEXT,
                source_relative_path TEXT,
                integrity_status TEXT, review_status TEXT,
                enabled_for_scheduling INTEGER
            );
            CREATE TABLE asset_media_info(asset_id INTEGER, duration_seconds REAL);
            CREATE TABLE shadow_files(
                id INTEGER PRIMARY KEY,shadow_relative_path TEXT,target_asset_id INTEGER,
                series_title TEXT,channel_slug TEXT,daypart_slug TEXT,
                collection_type TEXT,collection_slug TEXT,status TEXT
            );
            """
        )
        rows = [
            (1, "s01e01.mp4", "episode", "S01E01", "The Simpsons", "fox", "evening", None, None, "fox/evening/episodes/The Simpsons/Season 1/s01e01.mp4", "ready", "ready", 1, 1320),
            (2, "s01e02.mp4", "episode", "S01E02", "The Simpsons", "fox", "evening", None, None, "fox/evening/episodes/The Simpsons/Season 2/s01e02.mp4", "ready", "ready", 1, 1320),
            (3, "s01e03.mp4", "episode", "S01E03", "The Simpsons", "fox", None, "weekly", "saturday-morning", "fox/specials/weekly/saturday-morning/episodes/The Simpsons/s01e03.mp4", "ready", "ready", 1, 1320),
            (4, "movie.mp4", "movie", "Movie Night", None, "hbo", "all-day", None, None, "hbo/all-day/movies/movie.mp4", "ready", "open", 0, 5400),
        ]
        for row in rows:
            connection.execute(
                """
                INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row[:-1],
            )
            connection.execute(
                "INSERT INTO asset_media_info VALUES (?, ?)", (row[0], row[-1])
            )
        add_storage_fixture(connection)
        connection.execute("ALTER TABLE assets ADD COLUMN holiday_weekly_slug TEXT")
        connection.execute("ALTER TABLE shadow_files ADD COLUMN holiday_weekly_slug TEXT")
        result = _media_inventory(connection)
        simpsons = [item for item in result["items"] if item["title"] == "The Simpsons"]
        self.assertEqual(len(simpsons), 2)
        self.assertEqual(
            {(item["location"], item["item_count"]) for item in simpsons},
            {("evening", 2), ("weekly: saturday-morning", 1)},
        )
        self.assertEqual(result["summary"]["series"], 1)
        self.assertEqual(result["summary"]["movies"], 1)
        self.assertEqual(result["summary"]["review"], 1)
        evening = next(item for item in simpsons if item["location"] == "evening")
        self.assertEqual(
            evening["folder_relative_path"],
            "fox/evening/episodes/The Simpsons",
        )
        connection.close()

    def test_inventory_reports_regular_series_temporarily_displaced_by_holiday(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE assets(
                id INTEGER PRIMARY KEY,original_filename TEXT,media_type TEXT,
                display_title TEXT,series_title TEXT,channel_slug TEXT,daypart_slug TEXT,
                collection_type TEXT,collection_slug TEXT,source_relative_path TEXT,
                integrity_status TEXT,review_status TEXT,enabled_for_scheduling INTEGER,
                holiday_weekly_slug TEXT
            );
            CREATE TABLE asset_media_info(asset_id INTEGER,duration_seconds REAL);
            CREATE TABLE shadow_files(
                id INTEGER PRIMARY KEY,shadow_relative_path TEXT,target_asset_id INTEGER,
                series_title TEXT,channel_slug TEXT,daypart_slug TEXT,
                collection_type TEXT,collection_slug TEXT,status TEXT,holiday_weekly_slug TEXT
            );
            CREATE TABLE channels(id INTEGER PRIMARY KEY,slug TEXT);
            CREATE TABLE channel_schedule_preferences(channel_id INTEGER,recurring_enabled INTEGER);
            CREATE TABLE schedule_generations(
                id INTEGER PRIMARY KEY,channel_id INTEGER,schedule_date TEXT,status TEXT
            );
            CREATE TABLE recurring_lineup_snapshots(
                generation_id INTEGER,channel_id INTEGER,schedule_date TEXT,state_json TEXT
            );
            CREATE TABLE schedule_entries(
                channel_id INTEGER,asset_id INTEGER,is_program INTEGER,daypart_slug TEXT,
                programming_owner TEXT,generation_id INTEGER,id INTEGER,
                sequence_group INTEGER,ends_at TEXT,explanation_json TEXT
            );
            INSERT INTO assets VALUES(
                1,'S01E01.mp4','episode','S01E01','Bewitched','nickelodeon','night',
                NULL,NULL,'nickelodeon/night/episodes/Bewitched/S01E01.mp4','ready','ready',1,NULL
            );
            INSERT INTO asset_media_info VALUES(1,1320);
            INSERT INTO channels VALUES(1,'nickelodeon');
            INSERT INTO channel_schedule_preferences VALUES(1,1);
            INSERT INTO schedule_generations VALUES(1,1,'2099-10-01','active');
        """)
        future = (date.today() + timedelta(days=3)).isoformat()
        state = {
            "slots": {"daily:night": {"1200": "Bewitched"}},
            "holiday_lineups": {"halloween": {
                "calendar": {"Bewitched": [{"asset_id": 9,"target_date": future,"daypart": "night",
                                                "series_folder_name": "Bewitched"}]}
            }},
        }
        connection.execute(
            "INSERT INTO recurring_lineup_snapshots VALUES(1,1,'2099-10-01',?)",
            (json.dumps(state),),
        )
        add_storage_fixture(connection)
        item = next(row for row in _media_inventory(connection)["items"]
                    if row["title"] == "Bewitched")
        self.assertFalse(item["recurring_unplaced"])
        self.assertTrue(item["recurring_assigned"])
        self.assertTrue(item["recurring_displaced"])
        self.assertEqual(item["holiday_upcoming_airings"][0]["target_date"], future)
        connection.close()

    def test_inventory_counts_airings_across_channel_modes_and_collections(self):
        from retrotv.db import apply_migrations

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        apply_migrations(connection, Path(__file__).resolve().parents[1] / "migrations")
        connection.execute("INSERT INTO storage_roots(id,logical_name) VALUES(1,'media')")
        for channel_id, slug in [(1, "pbs"), (2, "sci-fi")]:
            connection.execute("INSERT INTO channels(id,slug,display_name) VALUES(?,?,?)",
                               (channel_id, slug, slug))
            connection.execute(
                "INSERT INTO channel_schedule_preferences(channel_id,recurring_enabled,updated_at) VALUES(?,?,?)",
                (channel_id, int(channel_id == 2), "2099-10-01"))
        assets = [
            (1, "Pappyland", "pbs", "morning", "episode", None, None),
            (2, "Planet of the Apes", "sci-fi", "all-day", "episode", None, None),
            (3, "Pappyland", "pbs", "morning", "episode", "holiday", "halloween"),
            (4, "Movie", "pbs", "morning", "movie", None, None),
            (5, "Pappyland", "pbs", "night", "episode", None, None),
            (6, "Planet of the Apes", "sci-fi", "night", "episode", None, None),
            (7, "Shared Short", "shared", "daytime", "short", None, None),
            (8, "Block", "pbs", None, "complete_block", "weekly", "saturday"),
        ]
        for asset_id, title, channel, scope, media_type, collection, slug in assets:
            connection.execute("""
                INSERT INTO assets(id,source_root_id,source_relative_path,original_filename,
                    file_size,modified_ns,media_type,display_title,series_title,channel_slug,
                    daypart_slug,collection_type,collection_slug,integrity_status,review_status,
                    enabled_for_scheduling,first_seen_at,last_seen_at)
                VALUES(?,1,?,'episode.mp4',1,1,?,?,?,?,?,?,?,'ready','ready',1,'2099-10-01','2099-10-01')
                """, (asset_id, f"{channel}/{asset_id}/episode.mp4", media_type, title,
                         title if media_type in {"episode", "short"} else None,
                         channel, scope, collection, slug))
        connection.execute("""
            INSERT INTO shadow_files(id,shadow_relative_path,target_asset_id,series_title,
                channel_slug,daypart_slug,collection_type,collection_slug,status,
                first_seen_at,last_seen_at,target_relative_path,file_size,modified_ns)
            VALUES(1,'pbs/specials/weekly/reruns/episodes/Pappyland Reruns/episode.shadow',
                1,'Pappyland Reruns','pbs',NULL,'weekly','reruns','ready','2099-10-01','2099-10-01',
                'pbs/1/episode.mp4',1,1)
        """)
        connection.execute("""
            INSERT INTO shadow_files(id,shadow_relative_path,target_asset_id,series_title,
                channel_slug,daypart_slug,collection_type,collection_slug,status,
                first_seen_at,last_seen_at,target_relative_path,file_size,modified_ns)
            VALUES(2,'pbs/specials/weekly/same-name/episodes/Pappyland/episode.shadow',
                1,'Pappyland','pbs',NULL,'weekly','same-name','ready','2099-10-01','2099-10-01',
                'pbs/1/episode.mp4',1,1)
        """)
        for generation, channel, day, status in [
            (1, 1, "2099-10-15", "active"), (2, 2, "2099-10-15", "active"),
            (3, 1, "2099-10-16", "superseded"), (4, 1, "2000-01-01", "active"),
        ]:
            connection.execute("""INSERT INTO schedule_generations(id,channel_id,schedule_date,
                seed,configuration_fingerprint,status,generated_at) VALUES(?,?,?,'seed','fp',?,?)""",
                (generation, channel, day, status, day))

        def airing(asset_id, generation=1, channel=1, group=1, part="morning",
                   owner="base", selection=None, end="2099-10-15T07:30:00+00:00"):
            connection.execute("""INSERT INTO schedule_entries(generation_id,channel_id,asset_id,
                starts_at,ends_at,media_start_ms,media_end_ms,entry_type,title,daypart_slug,
                sequence_group,sequence_position,is_program,programming_owner,explanation_json)
                VALUES(?,?,?, ?,?,0,60000,'episode','Episode',?,?,1,1,?,?)""",
                (generation, channel, asset_id, end[:10] + "T07:00:00+00:00", end,
                 part, group, owner, json.dumps({"selection": selection}) if selection else "{}"))

        ordinary_selection = {"series": "Pappyland", "collection_type": None, "collection": None}
        airing(1, selection=ordinary_selection)  # Two acts form one airing.
        airing(1, selection=ordinary_selection)
        airing(1, group=2, selection=ordinary_selection)  # A real repeat is another airing.
        airing(1, generation=3)
        airing(1, generation=4, end="2000-01-01T07:30:00+00:00")
        airing(2, generation=2, channel=2, part="evening")
        airing(3, group=3, owner="halloween")
        airing(4, group=4)
        airing(7, group=5, part="afternoon")
        airing(8, group=6, owner="saturday")
        airing(1, group=7, owner="reruns", selection={"series": "Pappyland Reruns",
               "collection_type": "weekly", "collection": "reruns"})
        airing(1, group=8, owner="same-name", selection={"series": "Pappyland",
               "collection_type": "weekly", "collection": "same-name"})
        items = _media_inventory(connection)["items"]
        by_id = {row["preview_asset_id"]: row for row in items if row["row_kind"] == "native"}
        self.assertEqual(by_id[1]["upcoming_appearances"], 2)
        self.assertFalse(by_id[1]["recurring_channel"])
        self.assertFalse(by_id[1]["upcoming_not_scheduled"])
        self.assertEqual(by_id[2]["upcoming_appearances"], 1)
        self.assertTrue(by_id[2]["recurring_channel"])
        self.assertFalse(by_id[2]["recurring_unplaced"])
        for asset_id in (3, 4, 7, 8):
            self.assertEqual(by_id[asset_id]["upcoming_appearances"], 1)
        for asset_id in (5, 6):
            self.assertEqual(by_id[asset_id]["upcoming_appearances"], 0)
            self.assertTrue(by_id[asset_id]["upcoming_not_scheduled"])
        self.assertTrue(by_id[6]["recurring_unplaced"])
        for shadow in (row for row in items if row["row_kind"] == "shadow"):
            self.assertEqual(shadow["upcoming_appearances"], 1)

    def test_asset_query_can_filter_normalization_review_queue(self):
        clause, values = _asset_query({"analysis_status": ["review"]})
        self.assertIn("a.integrity_status IN ('ready', 'not_scanned')", clause)
        self.assertIn("n.analysis_status = ?", clause)
        self.assertEqual(values, ["review"])

    def test_asset_query_can_filter_audio_review_queue(self):
        clause, values = _asset_query({"audio_analysis_status": ["review"]})
        self.assertIn("a.integrity_status IN ('ready', 'not_scanned')", clause)
        self.assertIn("an.analysis_status = ?", clause)
        self.assertEqual(values, ["review"])

    def test_asset_query_accepts_exact_asset_id_searches(self):
        for search in ("123", "#123", "id:123"):
            clause, values = _asset_query({"search": [search]})
            self.assertIn("a.id = ?", clause)
            self.assertEqual(values, [123])

    def test_library_exposes_separate_review_filters(self):
        self.assertIn('value="library:open">Library review', INDEX_HTML)
        self.assertIn(
            'value="normalization:review">Aspect review',
            INDEX_HTML,
        )
        self.assertIn('value="audio:review">Audio review', INDEX_HTML)
        self.assertIn("Apply crop", INDEX_HTML)
        self.assertIn("Approve aspect", INDEX_HTML)
        self.assertIn("Apply + approve folder", INDEX_HTML)
        self.assertIn('action: "approve-folder"', INDEX_HTML)
        self.assertIn("Preview comparison", INDEX_HTML)
        self.assertIn("Preview correction", INDEX_HTML)
        self.assertIn("Approve shown correction", INDEX_HTML)
        self.assertIn('action:"draft-preview"', INDEX_HTML)
        self.assertIn('action:"approve-preview"', INDEX_HTML)
        self.assertIn("let installOriginalImage = null;", INDEX_HTML)
        self.assertNotIn("const installOriginalImage =", INDEX_HTML)
        self.assertIn(".aspect-preview-panel.expanded a", INDEX_HTML)
        self.assertIn("aspect-ratio:4 / 3", INDEX_HTML)
        self.assertIn("Settings changed — preview again before approval.", INDEX_HTML)
        self.assertIn('limit: reviewFilter.startsWith("normalization:") ? "1000"', INDEX_HTML)
        self.assertIn("Are you sure?", INDEX_HTML)
        self.assertIn("Convert approved media", DASHBOARD_HTML)
        self.assertIn("Pending corrections", DASHBOARD_HTML)
        self.assertIn("yellow [!] indicator", DASHBOARD_HTML)
        self.assertIn("Corrections pending", INDEX_HTML)
        self.assertNotIn("Proxies ready", INDEX_HTML)
        self.assertNotIn("Create correction", INDEX_HTML)
        self.assertIn("No conversion needed", DASHBOARD_HTML)
        self.assertIn("Clean files and expired backups now", DASHBOARD_HTML)
        self.assertIn("Open labeled layout guide", INDEX_HTML)
        self.assertIn('<select id="schedule-channel"', INDEX_HTML)
        self.assertIn('id="schedule-recurring"', INDEX_HTML)
        self.assertIn('id="schedule-unique-weekends"', INDEX_HTML)
        self.assertIn('id="schedule-presentation-format"', INDEX_HTML)
        self.assertIn('id="reroll-schedule"', INDEX_HTML)
        self.assertIn("Content Only", INDEX_HTML)
        self.assertIn("Schedule view filter", INDEX_HTML)
        self.assertIn("It does not change what airs.", INDEX_HTML)
        self.assertIn("Schedule settings", INDEX_HTML)
        self.assertIn("without inserted commercials, filler, bumpers, or Up Next", INDEX_HTML)
        self.assertIn("Re-roll creates a different valid arrangement", INDEX_HTML)
        self.assertIn("become the recurring base for later matching days", INDEX_HTML)
        self.assertIn("recurring_schedules_refreshed", INDEX_HTML)
        self.assertIn('/api/schedule/preferences', INDEX_HTML)
        self.assertIn('/api/channels', INDEX_HTML)
        self.assertIn("Act Breaks", INDEX_HTML)
        self.assertIn('id="breakpoint-filter"', INDEX_HTML)
        self.assertIn('value="midpoint">Generic halfway fallback', INDEX_HTML)
        self.assertIn("Act breaks", DASHBOARD_HTML)
        self.assertIn("Audio loudness normalization", DASHBOARD_HTML)
        self.assertIn("Analyze pending audio", DASHBOARD_HTML)
        self.assertIn("Normalize approved audio", DASHBOARD_HTML)
        self.assertIn("clearInterval(dashboardRefreshTimer)", DASHBOARD_HTML)
        self.assertIn("SHUTDOWN CONFIRMATION FAILED", DASHBOARD_HTML)

    def test_settings_page_covers_routine_configuration(self):
        self.assertIn("Routine configuration without TOML editing", SETTINGS_HTML)
        self.assertIn("Channel numbers", SETTINGS_HTML)
        self.assertIn("Dayparts", SETTINGS_HTML)
        self.assertIn("Holiday programming", SETTINGS_HTML)
        self.assertIn("Weekly programming blocks", SETTINGS_HTML)
        self.assertIn("Save &amp; update schedules", SETTINGS_HTML)
        self.assertIn("/api/settings", SETTINGS_HTML)


if __name__ == "__main__":
    unittest.main()
