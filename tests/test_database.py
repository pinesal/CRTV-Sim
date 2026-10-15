import sqlite3
import shutil
import unittest
import uuid
from pathlib import Path

from retrotv.db import apply_migrations, connect_database, ensure_storage_root


class DatabaseTests(unittest.TestCase):
    def test_migrations_and_logical_storage_root(self):
        project = Path(__file__).resolve().parents[1]
        test_temp = project / ".test-tmp"
        test_temp.mkdir(exist_ok=True)
        directory = test_temp / f"database-{uuid.uuid4().hex}"
        directory.mkdir()
        try:
            database = directory / "retrotv.sqlite3"
            connection = connect_database(database)
            try:
                applied = apply_migrations(connection, project / "migrations")
                self.assertEqual(
                    applied,
                    [
                        "001_library_foundation.sql",
                        "002_normalization.sql",
                        "003_linear_scheduling.sql",
                        "004_normalization_promotions.sql",
                        "005_seasonal_programming.sql",
                        "006_complete_blocks.sql",
                        "007_tv_guide.sql",
                        "008_demand_announcements_and_graphics.sql",
                        "009_graphics_music.sql",
                        "010_program_breakpoints.sql",
                        "011_ai_announcement_profiles.sql",
                        "012_audio_normalization.sql",
                        "013_guide_music.sql",
                        "014_exact_duplicates.sql",
                        "015_shadow_files.sql",
                        "016_daypart_branding.sql",
                        "017_channel_schedule_preferences.sql",
                        "018_channel_presentation_format.sql",
                        "019_cleanup_retention.sql",
                        "020_internet_media.sql",
                        "021_internet_media_refinements.sql",
                        "022_maintenance_housekeeping.sql",
                        "023_weekly_special_graphics.sql",
                        "024_weekly_announcer_profiles.sql",
                        "025_holiday_scopes_and_commercial_review.sql",
                        "026_bumper_cue_overrides.sql",
                        "027_break_target_overrides.sql",
                        "028_holiday_filename_reviews.sql",
                        "029_weekly_special_promos.sql",
                        "030_weekly_promo_settings.sql",
                        "031_commercial_tail_cleanup.sql",
                        "032_recurring_lineup_state.sql",
                        "033_normalization_rereview.sql",
                        "034_profile_tts_providers.sql",
                        "035_external_media.sql",
                        "036_generic_announcement_fallback.sql",
                    ],
                )
                self.assertEqual(
                    apply_migrations(connection, project / "migrations"),
                    [],
                )
                root_id = ensure_storage_root(connection, "media")
                row = connection.execute(
                    "SELECT * FROM storage_roots WHERE id = ?",
                    (root_id,),
                ).fetchone()
                self.assertEqual(row["logical_name"], "media")
                columns = {
                    item["name"]
                    for item in connection.execute("PRAGMA table_info(storage_roots)")
                }
                self.assertNotIn("windows_path", columns)
                self.assertNotIn("raspberry_pi_path", columns)
                promotion_columns = {
                    item["name"]
                    for item in connection.execute(
                        "PRAGMA table_info(normalization_promotions)"
                    )
                }
                self.assertIn("backup_deleted_at", promotion_columns)
            finally:
                connection.close()
        finally:
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
