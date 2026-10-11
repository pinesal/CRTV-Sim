import unittest

from retrotv.classification import classify_path


CHANNELS = {"fox", "nickelodeon", "cartoon-network", "mtv", "discovery"}
HOLIDAYS = {"halloween", "christmas"}


class ClassificationTests(unittest.TestCase):
    def test_shared_holiday_episodes_expand_scope_without_channel_restriction(self):
        result = self.classify("shared/specials/holidays/halloween/daytime/episodes/Goosebumps/Season 1/Haunted.mp4")
        self.assertTrue(result.is_ready)
        self.assertEqual(result.series_title, "Goosebumps")
        self.assertEqual({item.channel_slug for item in result.eligibility}, {"*"})
        self.assertEqual({item.daypart_slug for item in result.eligibility}, {"morning", "afternoon", "evening"})

    def classify(self, path: str):
        return classify_path(
            path,
            known_channels=CHANNELS,
            known_holidays=HOLIDAYS,
        )

    def test_channel_episode_infers_series_and_daypart(self):
        result = self.classify(
            "fox/night/episodes/The Simpsons/Treehouse.of.Horror.mkv"
        )
        self.assertTrue(result.is_ready)
        self.assertEqual(result.channel_slug, "fox")
        self.assertEqual(result.daypart_slug, "night")
        self.assertEqual(result.media_type, "episode")
        self.assertEqual(result.series_title, "The Simpsons")

    def test_shared_daytime_expands_without_night(self):
        result = self.classify("shared/daytime/commercials/cereal-ad.mp4")
        self.assertTrue(result.is_ready)
        self.assertEqual(
            {item.daypart_slug for item in result.eligibility},
            {"morning", "afternoon", "evening"},
        )
        self.assertNotIn(
            "night",
            {item.daypart_slug for item in result.eligibility},
        )

    def test_channel_daytime_expands_without_night(self):
        result = self.classify(
            "nickelodeon/daytime/bumpers/Orange Splat.mp4"
        )
        self.assertTrue(result.is_ready)
        self.assertEqual(result.channel_slug, "nickelodeon")
        self.assertEqual(result.daypart_slug, "daytime")
        self.assertEqual(
            {item.daypart_slug for item in result.eligibility},
            {"morning", "afternoon", "evening"},
        )
        self.assertNotIn(
            "night",
            {item.daypart_slug for item in result.eligibility},
        )

    def test_channel_all_day_includes_night(self):
        result = self.classify(
            "nickelodeon/all-day/bumpers/Anytime Station ID.mp4"
        )
        self.assertTrue(result.is_ready)
        self.assertEqual(
            {item.daypart_slug for item in result.eligibility},
            {"morning", "afternoon", "evening", "night"},
        )

    def test_shared_all_day_expands_to_configured_future_daypart(self):
        result = classify_path(
            "shared/all-day/filler/overnight-loop.mp4",
            known_channels=CHANNELS,
            known_holidays=HOLIDAYS,
            known_dayparts={"morning", "afternoon", "evening", "night", "over-night", "all-day", "daytime"},
        )
        self.assertTrue(result.is_ready)
        self.assertIn("over-night", {item.daypart_slug for item in result.eligibility})

    def test_shared_night_is_night_only(self):
        result = self.classify("shared/night/commercials/late-ad.mp4")
        self.assertTrue(result.is_ready)
        self.assertEqual(
            {item.daypart_slug for item in result.eligibility},
            {"night"},
        )

    def test_shared_holiday_movie_is_all_channel_candidate(self):
        result = self.classify(
            "shared/specials/holidays/halloween/movies/Monster Movie.mp4"
        )
        self.assertTrue(result.is_ready)
        self.assertEqual(result.media_type, "movie")
        self.assertEqual(result.holiday_slug, "halloween")
        self.assertEqual(result.eligibility[0].channel_slug, "*")

    def test_holiday_night_block_and_weekly_variant_have_distinct_scopes(self):
        night = self.classify(
            "nickelodeon/specials/holidays/halloween/night/blocks/Nick at Nite.mp4"
        )
        self.assertTrue(night.is_ready)
        self.assertEqual(night.daypart_slug, "night")
        self.assertEqual({item.daypart_slug for item in night.eligibility}, {"night"})
        self.assertIsNone(night.holiday_weekly_slug)
        weekly = self.classify(
            "nickelodeon/specials/holidays/halloween/specials/weekly/snick-main/episodes/Show/Part 1.mp4"
        )
        self.assertTrue(weekly.is_ready)
        self.assertEqual(weekly.holiday_weekly_slug, "snick-main")
        self.assertEqual(weekly.series_title, "Show")

        daytime = self.classify(
            "nickelodeon/specials/holidays/halloween/daytime/episodes/Count Duckula/Season 1/1x01.mp4"
        )
        self.assertTrue(daytime.is_ready)
        self.assertEqual(daytime.series_title, "Count Duckula")
        self.assertEqual(
            {item.daypart_slug for item in daytime.eligibility},
            {"morning", "afternoon", "evening"},
        )

        all_day = self.classify(
            "nickelodeon/specials/holidays/halloween/all-day/blocks/Halloween.mp4"
        )
        self.assertTrue(all_day.is_ready)
        self.assertIn("night", {item.daypart_slug for item in all_day.eligibility})

    def test_prior_holiday_layout_remains_readable(self):
        old_daypart = self.classify(
            "nickelodeon/specials/holidays/halloween/dayparts/night/blocks/Old.mp4"
        )
        old_weekly = self.classify(
            "nickelodeon/specials/holidays/halloween/weekly/snick-main/episodes/Show/Old.mp4"
        )
        self.assertTrue(old_daypart.is_ready)
        self.assertTrue(old_weekly.is_ready)
        self.assertEqual(old_daypart.daypart_slug, "night")
        self.assertEqual(old_weekly.holiday_weekly_slug, "snick-main")

    def test_legacy_holiday_folder_stays_ordinary_all_day(self):
        legacy = self.classify(
            "nickelodeon/specials/holidays/halloween/blocks/Legacy.mp4"
        )
        self.assertTrue(legacy.is_ready)
        self.assertIsNone(legacy.daypart_slug)
        self.assertIsNone(legacy.holiday_weekly_slug)

    def test_shared_holiday_block_requires_review(self):
        result = self.classify(
            "shared/specials/holidays/halloween/blocks/Fox Block.mp4"
        )
        self.assertFalse(result.is_ready)
        self.assertIn(
            "shared_program_requires_channels",
            {reason for reason, _ in result.review_reasons},
        )

    def test_misspelled_holiday_suggests_halloween(self):
        result = self.classify(
            "shared/specials/holidays/holloween/movies/Movie.mp4"
        )
        details = " ".join(detail for _, detail in result.review_reasons)
        self.assertIn("halloween", details)


if __name__ == "__main__":
    unittest.main()
