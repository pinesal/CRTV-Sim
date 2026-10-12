import json
import sqlite3
import shutil
import unittest
import uuid
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from retrotv.config import AppConfig
from retrotv.web import (
    DASHBOARD_HTML, INDEX_HTML, INTERNET_MEDIA_HTML, SETTINGS_HTML, _asset_query, _media_inventory,
    forget_missing_asset_record, serve,
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
        self.assertIn("No upcoming placement", INDEX_HTML)
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
                programming_owner TEXT,generation_id INTEGER
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
