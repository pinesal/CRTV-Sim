import unittest
import sqlite3

from retrotv.bumper_cues import (
    bumper_review_catalog, classify_bumper, save_bumper_override, series_matches,
)
from retrotv.web import _unmatched_named_bumpers


class BumperCueTests(unittest.TestCase):
    def test_fox_kids_transcription_variants(self):
        starts = (
            "We'll be right back. Sheesh, what do.mp4",
            "We'll be back after these important messages.mp4",
            "Stay tuned, we'll be right back.mp4",
            "Howdy, partner. We'll be back right after.mp4",
            "After these messages, I'll show you how.mp4",
            "Coming up after the break.mp4",
            "Dont go away!.mp4",
            "We will be write back.mp4",
        )
        ends = (
            "Now back to our program. Sheesh! Don't.mp4",
            "Back to our show, gang. Hmm, where.mp4",
            "And now back to our regular program.mp4",
            "All right, don't get excited. We're back.mp4",
            "Welcome back!.mp4",
            "Now bake to our program.mp4",
        )
        for name in starts:
            with self.subTest(name=name):
                self.assertEqual(classify_bumper(name).position, "break_start")
        for name in ends:
            with self.subTest(name=name):
                self.assertEqual(classify_bumper(name).position, "break_end")

    def test_named_shows_and_weak_words(self):
        self.assertEqual(
            classify_bumper("Chip and Dale's Rescue Rangers will return.mp4").series_hint,
            "chip and dale rescue rangers",
        )
        self.assertEqual(
            classify_bumper("Now, back to the Flintstones!.mp4").series_hint,
            "Flintstones",
        )
        self.assertEqual(
            classify_bumper("Back to Rugrats.mp4").series_hint,
            "Rugrats",
        )
        for name in (
            "Now Wisecracks about the hat, and we'll.mp4",
            "I'm back from another adventure and you're.mp4",
            "Don't touch that dial, 'cause now the.mp4",
            "Stay tuned for a new cartoon.mp4",
            "Yeesh! Whoever heard of a duck getting.mp4",
            "Nick_bumper_Come_back_to_me_again.mp4",
        ):
            with self.subTest(name=name):
                self.assertEqual(classify_bumper(name).position, "general")
        self.assertTrue(series_matches("Flintstones", "The Flintstones"))
        self.assertTrue(series_matches("Chip and Dale Rescue Rangers", "Chip 'n Dale Rescue Rangers"))
        self.assertFalse(series_matches("Flintstones", "The Simpsons"))

    def test_unmatched_named_bumpers_are_visible_for_review(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE assets(id INTEGER, media_type TEXT, series_title TEXT,
                display_title TEXT, original_filename TEXT, source_relative_path TEXT,
                integrity_status TEXT, review_status TEXT, enabled_for_scheduling INTEGER);
            CREATE TABLE shadow_files(series_title TEXT, status TEXT);
            CREATE TABLE bumper_cue_overrides(asset_id INTEGER PRIMARY KEY,
                position TEXT NOT NULL, series_title TEXT, updated_at TEXT);
        """)
        connection.executemany(
            "INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (1, "episode", "The Flintstones", "S01E01", "S01E01.mp4",
                 "fox/episodes/S01E01.mp4", "ready", "ready", 1),
                (2, "bumper", None, "Flintstones bumper", "Now back to the Flintstones.mp4",
                 "shared/bumpers/Now back to the Flintstones.mp4", "ready", "ready", 1),
                (3, "bumper", None, "Chip and Dale bumper",
                 "Chip and Dale's Rescue Rangers will return.mp4",
                 "shared/bumpers/Chip and Dale's Rescue Rangers will return.mp4",
                 "ready", "ready", 1),
            ],
        )
        try:
            self.assertEqual([item["id"] for item in _unmatched_named_bumpers(connection)], [3])
            save_bumper_override(connection, 3, "break_start", "The Flintstones")
            self.assertEqual(_unmatched_named_bumpers(connection), [])
            self.assertEqual(bumper_review_catalog(connection)["pending_count"], 0)
            save_bumper_override(connection, 3, "general", None)
            self.assertEqual(bumper_review_catalog(connection)["pending_count"], 0)
            self.assertEqual(
                next(item for item in bumper_review_catalog(connection)["items"]
                     if item["id"] == 3)["override_position"],
                "general",
            )
            save_bumper_override(connection, 3, "auto", None)
            self.assertEqual([item["id"] for item in _unmatched_named_bumpers(connection)], [3])
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
