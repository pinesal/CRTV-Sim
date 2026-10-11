import json
import shutil
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database, ensure_storage_root, utc_now
from retrotv.holiday_review import HolidayReviewService
from retrotv.web import make_handler


class HolidayReviewTests(unittest.TestCase):
    def test_destination_suggestions_preserve_scope_and_descendants(self):
        from retrotv.holiday_review import destination_relative_path
        cases = [
            ("fox/evening/episodes/The Drew Carey Show/Season 01/Christmas.mp4", "christmas", "fox/specials/holidays/christmas/evening/episodes"),
            ("shared/all-day/commercials/Pumpkin.mp4", "halloween", "shared/specials/holidays/halloween/all-day/commercials"),
            ("fox/specials/holidays/halloween/night/movies/Christmas.mp4", "christmas", "fox/specials/holidays/christmas/night/movies"),
            ("fox/specials/weekly/snick/episodes/Christmas.mp4", "christmas", ""),
            ("unknown/night/episodes/Christmas.mp4", "christmas", ""),
            ("../fox/night/episodes/Christmas.mp4", "christmas", ""),
        ]
        for source, holiday, expected in cases:
            self.assertEqual(self.service.suggested_destination(source, holiday), expected)
        source, _, folder = cases[0]
        self.assertEqual(destination_relative_path(source, folder).as_posix(),
                         folder + "/The Drew Carey Show/Season 01/Christmas.mp4")
        self.add(1, source)
        self.service.reconcile()
        self.assertEqual(self.service.pending()[0]["suggested_destination_folder"], folder)

    def setUp(self):
        project = Path(__file__).resolve().parents[1]
        self.root = project / ".test-tmp" / f"holiday-review-{uuid.uuid4().hex}"
        self.media = self.root / "media"
        self.media.mkdir(parents=True)
        self.config = AppConfig(
            self.root / "test.toml", project,
            {"application": {"database_path": str(self.root / "db.sqlite3")},
             "storage_roots": {"media": str(self.media)},
             "library": {"holidays": ["halloween", "christmas"]},
             "seasonal": {"holidays": {"halloween": {}, "christmas": {}}},
             "scheduling": {"dayparts": [{"slug": "night"}]}}
        )
        self.connection = connect_database(self.config.database_path)
        apply_migrations(self.connection, project / "migrations")
        self.root_id = ensure_storage_root(self.connection, "media")
        self.connection.execute(
            "INSERT INTO channels(slug,display_name,enabled) VALUES ('fox','Fox',1)"
        )
        self.connection.commit()
        self.service = HolidayReviewService(self.connection, self.config)

    def tearDown(self):
        self.connection.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def add(self, asset_id, relative, media_type="episode", *, ready=True, holiday=None):
        path = self.media / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        now = utc_now()
        self.connection.execute("""
            INSERT INTO assets(id,source_root_id,source_relative_path,original_filename,
                file_size,modified_ns,media_type,holiday_slug,integrity_status,
                review_status,enabled_for_scheduling,first_seen_at,last_seen_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (asset_id, self.root_id, Path(relative).as_posix(), path.name, 5, 1,
              media_type, holiday, "ready", "ready" if ready else "open",
              int(ready), now, now))
        self.connection.commit()
        return path

    def test_phrase_variations_and_unknown_names(self):
        cases = [
            ("Trick-or-Treat.mp4", "halloween", "Trick-or-Treat"),
            ("Trick_r_Treat.mp4", "halloween", "Trick_r_Treat"),
            ("Jack-o'-lantern.mp4", "halloween", "Jack-o'-lantern"),
            ("Hallowe'en Special.mp4", "halloween", "Hallowe'en"),
            ("A Haunted House.mp4", "halloween", "Haunted"),
            ("Christmas_Eve.mp4", "christmas", "Christmas"),
            ("4th.of.July.mp4", "independence-day", "4th.of.July"),
        ]
        for filename, holiday, clue in cases:
            with self.subTest(filename=filename):
                self.assertEqual(self.service.matches(filename)[holiday], clue)
        self.assertEqual(self.service.matches("S01E01.mp4"), {})
        self.assertEqual(self.service.matches("ghostwriter.mp4"), {})

    def test_custom_clues_extend_or_replace_defaults(self):
        self.config.data["library"]["holiday_filename_clues"] = {
            "halloween": ["boo night"], "summer-solstice": ["longest day"]
        }
        self.assertEqual(self.service.matches("Boo_Night.mp4"),
                         {"halloween": "Boo_Night"})
        self.assertEqual(self.service.matches("Longest-Day.mp4"),
                         {"summer-solstice": "Longest-Day"})
        self.assertNotIn("halloween", self.service.matches("Pumpkin.mp4"))

    def test_all_media_types_are_reviewed_without_disabling_them(self):
        types = ["episode", "movie", "commercial", "bumper", "promo",
                 "station_id", "short", "complete_block", "filler", None]
        for index, media_type in enumerate(types, 1):
            self.add(index, f"fox/night/episodes/Pumpkin_{index}.mp4", media_type)
        self.service.reconcile()
        rows = self.service.pending()
        self.assertEqual(len(rows), len(types))
        self.assertEqual({row["media_type"] for row in rows}, set(types))
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM assets WHERE enabled_for_scheduling=1"
        ).fetchone()[0], len(types))

    def test_dismissal_persists_until_filename_changes(self):
        self.add(1, "fox/night/episodes/Halloween.mp4")
        self.service.reconcile()
        self.service.dismiss(1, "halloween")
        self.service.reconcile()
        self.assertEqual(self.service.pending(), [])
        self.connection.execute("""
            UPDATE assets SET original_filename='Halloween_Night.mp4' WHERE id=1
        """)
        self.connection.commit()
        self.service.reconcile()
        self.assertEqual(len(self.service.pending()), 1)

    def test_already_sorted_and_multiple_holidays(self):
        self.add(1, "fox/specials/holidays/halloween/night/episodes/Halloween.mp4",
                 holiday="halloween")
        self.add(2, "fox/night/episodes/Halloween Christmas.mp4")
        self.service.reconcile()
        self.assertEqual({row["holiday_slug"] for row in self.service.pending()},
                         {"halloween", "christmas"})
        self.assertEqual({row["asset_id"] for row in self.service.pending()}, {2})

    def test_explicit_move_updates_asset_and_rejects_unsafe_targets(self):
        original = self.add(1, "fox/night/episodes/Pumpkin.mp4")
        self.service.reconcile()
        with self.assertRaisesRegex(ValueError, "active"):
            self.service.move(1, "halloween", "fox/night/promos",
                              active_path=str(original))
        with self.assertRaisesRegex(ValueError, "inside"):
            self.service.move(1, "halloween", "../outside")
        with self.assertRaisesRegex(ValueError, "inside"):
            self.service.move(1, "halloween", str(self.root / "outside"))
        collision = self.media / "fox/night/promos/Pumpkin.mp4"
        collision.parent.mkdir(parents=True)
        collision.write_bytes(b"other")
        with self.assertRaisesRegex(ValueError, "exists"):
            self.service.move(1, "halloween", "fox/night/promos")
        collision.unlink()
        result = self.service.move(1, "halloween", "fox/night/promos")
        self.assertFalse(result["library_review_needed"])
        self.assertTrue(collision.is_file())
        self.assertFalse(original.exists())
        row = self.connection.execute("""
            SELECT id,media_type,source_relative_path,enabled_for_scheduling FROM assets WHERE id=1
        """).fetchone()
        self.assertEqual(tuple(row), (1, "promo", "fox/night/promos/Pumpkin.mp4", 1))
        self.assertEqual(self.service.pending(), [])
        self.service.reconcile()
        self.assertEqual(self.service.pending(), [])  # Explicit move resolves the suggestion.

    def test_new_unrecognized_folder_moves_but_requires_library_review(self):
        self.add(1, "fox/night/episodes/Haunted.mp4")
        self.service.reconcile()
        result = self.service.move(1, "halloween", "unsorted/new-folder")
        self.assertTrue(result["library_review_needed"])
        self.assertTrue((self.media / "unsorted/new-folder/Haunted.mp4").is_file())
        row = self.connection.execute(
            "SELECT review_status,enabled_for_scheduling FROM assets WHERE id=1"
        ).fetchone()
        self.assertEqual(tuple(row), ("open", 0))

    def test_move_to_category_root_preserves_series_and_season(self):
        self.add(1, "fox/night/episodes/DuckTales/Season 01/Haunted S01E01.mp4")
        self.service.reconcile()
        item = self.service.pending()[0]
        self.assertEqual(item["source_category"], "episodes")
        self.assertEqual(item["source_subfolders"], ["DuckTales", "Season 01"])
        result = self.service.move(1, "halloween",
                                   "fox/specials/holidays/halloween/night/episodes")
        self.assertEqual(result["moved_to"],
                         "fox/specials/holidays/halloween/night/episodes/"
                         "DuckTales/Season 01/Haunted S01E01.mp4")
        self.assertTrue((self.media / result["moved_to"]).is_file())

    def test_explicit_series_destination_does_not_duplicate_series(self):
        self.add(1, "fox/night/episodes/DuckTales/Haunted.mp4")
        self.service.reconcile()
        result = self.service.move(1, "halloween",
                                   "fox/specials/holidays/halloween/night/episodes/DuckTales")
        self.assertEqual(result["moved_to"],
                         "fox/specials/holidays/halloween/night/episodes/DuckTales/Haunted.mp4")

    def test_review_api_lists_and_moves_to_explicit_folder(self):
        self.add(1, "fox/night/episodes/Pumpkin Special.mp4")
        empty_holiday_folder = self.media / "fox/specials/holidays/halloween/night/movies"
        empty_holiday_folder.mkdir(parents=True)
        self.service.reconcile()
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.config))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(base + "/api/holiday-reviews") as response:
                listing = json.load(response)
            self.assertEqual(len(listing["items"]), 1)
            self.assertEqual(listing["items"][0]["matched_clue"], "Pumpkin")
            self.assertIn("fox/specials/holidays/halloween/night/movies", listing["folders"])
            payload = json.dumps({"asset_id": 1, "holiday": "halloween", "action": "move",
                                  "folder": "fox/night/promos"}).encode()
            request = Request(base + "/api/holiday-reviews/action", data=payload,
                              headers={"Content-Type": "application/json"})
            with urlopen(request) as response:
                result = json.load(response)
            self.assertEqual(result["moved_to"], "fox/night/promos/Pumpkin Special.mp4")
            with urlopen(base + "/api/holiday-reviews") as response:
                listing = json.load(response)
            self.assertEqual(listing["items"], [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
