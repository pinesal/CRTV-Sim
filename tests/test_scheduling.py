import json
import random
import shutil
import unittest
import uuid
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database, ensure_storage_root, utc_now
from retrotv.blocks import BlockService
from retrotv.breakpoints import BreakpointAnalyzer
from retrotv.bumper_cues import save_bumper_override
from retrotv.scheduling import (
    BlockOpportunity,
    Candidate,
    ScheduleBuilder,
    TimelineItem,
)


class SchedulingTests(unittest.TestCase):
    def test_shared_holiday_episodes_are_selected_per_daypart_for_any_channel(self):
        self.add_asset(1, media_type="episode", daypart="evening", duration=1400, series="Haunted Show")
        self.connection.execute("""UPDATE assets SET channel_slug=NULL,collection_type='holiday',
            collection_slug='halloween',holiday_slug='halloween',source_relative_path=
            'shared/specials/holidays/halloween/evening/episodes/Haunted Show/episode.mp4' WHERE id=1""")
        self.connection.execute("UPDATE asset_eligibility SET channel_slug='*' WHERE asset_id=1")
        self.connection.commit()
        builder = ScheduleBuilder(self.connection, self.config)
        def candidates(channel, holiday, daypart):
            return builder._special_candidates(channel, daypart, ("episode",),
                collection_type="holiday", collection_slug=holiday, rule_slug=holiday)
        for channel in ("fox", "nickelodeon"):
            self.assertEqual([item.asset_id for item in candidates(channel, "halloween", "evening")], [1])
            self.assertEqual(candidates(channel, "halloween", "morning"), [])
            self.assertEqual(candidates(channel, "christmas", "evening"), [])

    def setUp(self):
        self.project = Path(__file__).resolve().parents[1]
        base = self.project / ".test-tmp"
        base.mkdir(exist_ok=True)
        self.temporary = base / f"scheduling-{uuid.uuid4().hex}"
        self.temporary.mkdir()
        self.config = AppConfig(
            profile_path=self.temporary / "test.toml",
            project_root=self.project,
            data={
                "application": {
                    "database_path": str(self.temporary / "retrotv.sqlite3"),
                    "timezone": "America/Los_Angeles",
                },
                "storage_roots": {
                    "media": str(self.temporary / "media"),
                    "normalized": str(self.temporary / "normalized"),
                    "previews": str(self.temporary / "previews"),
                },
                "scheduling": {
                    "timezone": "America/Los_Angeles",
                    "schedule_version": 1,
                    "commercials_per_break_min": 1,
                    "commercials_per_break_max": 1,
                    "dayparts": [
                        {
                            "slug": "morning",
                            "start": "06:00",
                            "end": "12:00",
                            "boundary_policy": "must_fit",
                        },
                        {
                            "slug": "afternoon",
                            "start": "12:00",
                            "end": "17:00",
                            "boundary_policy": "must_fit",
                        },
                        {
                            "slug": "evening",
                            "start": "17:00",
                            "end": "22:00",
                            "boundary_policy": "must_fit",
                        },
                        {
                            "slug": "night",
                            "start": "22:00",
                            "end": "06:00",
                            "boundary_policy": "must_fit",
                        },
                    ],
                },
                "blocks": {
                    "selection_probability": 0.0,
                    "maximum_per_daypart": 1,
                    "repeat_cooldown_days": 30,
                    "boundary_policy": "may_overrun",
                },
            },
        )
        self.connection = connect_database(self.config.database_path)
        apply_migrations(self.connection, self.project / "migrations")
        self.root_id = ensure_storage_root(self.connection, "media")
        self.now = utc_now()

    def tearDown(self):
        self.connection.close()
        shutil.rmtree(self.temporary, ignore_errors=True)

    def test_new_eligible_media_makes_active_schedule_stale(self):
        self.add_asset(
            1,
            media_type="episode",
            daypart="morning",
            duration=1200,
            series="Test Show",
        )
        builder = ScheduleBuilder(self.connection, self.config)
        target = date(2026, 7, 27)
        builder.generate_day("fox", target)
        self.assertTrue(
            ScheduleBuilder(self.connection, self.config).generation_is_current(
                "fox", target
            )
        )
        self.add_asset(
            2,
            media_type="episode",
            daypart="morning",
            duration=1200,
            series="Another Show",
        )
        self.assertFalse(
            ScheduleBuilder(self.connection, self.config).generation_is_current(
                "fox", target
            )
        )

    def test_changing_a_later_selected_break_invalidates_schedule(self):
        self.add_asset(1, media_type="episode", daypart="all-day",
                       duration=44 * 60, series="Hour Series")
        builder = ScheduleBuilder(self.connection, self.config)
        before = builder._configuration_fingerprint("fox")
        self.connection.execute(
            "UPDATE asset_break_candidates SET offset_ms=2060000 WHERE asset_id=1 AND offset_ms=2070000"
        )
        self.connection.commit()
        self.assertNotEqual(before, ScheduleBuilder(
            self.connection, self.config)._configuration_fingerprint("fox"))

    def test_quiet_pause_reanalysis_invalidates_generated_schedule(self):
        self.add_asset(1, media_type="episode", daypart="all-day", duration=23 * 60, series="Series")
        target = date(2026, 7, 27)
        builder = ScheduleBuilder(self.connection, self.config)
        builder.generate_day("fox", target)
        self.assertTrue(builder.generation_is_current("fox", target))
        self.connection.execute(
            "UPDATE asset_break_analyses SET source_signature='1:1:breaks-v6',selected_method='quiet_pause' WHERE asset_id=1"
        )
        self.connection.execute(
            "UPDATE asset_break_candidates SET method='quiet_pause',offset_ms=688000,signals_json=? WHERE asset_id=1",
            (json.dumps({"fallback":True,"silence":True,"silence_duration_ms":800}),),
        )
        self.connection.commit()
        self.assertFalse(ScheduleBuilder(self.connection, self.config).generation_is_current("fox", target))
        self.assertEqual(BreakpointAnalyzer(self.connection, self.config).selected_offsets(1), [688000])

    def add_asset(
        self,
        asset_id: int,
        *,
        media_type: str,
        daypart: str,
        duration: float,
        series: str | None = None,
        channel: str | None = "fox",
        collection_type: str | None = None,
        collection_slug: str | None = None,
        eligibility_channel: str | None = None,
    ):
        if collection_type:
            scope = channel or "shared"
            kind = "holidays" if collection_type == "holiday" else "weekly"
            relative = (
                f"{scope}/specials/{kind}/{collection_slug}/{media_type}s/"
                f"{asset_id}.mp4"
            )
        else:
            relative = f"{channel}/{daypart}/{media_type}s/{asset_id}.mp4"
        self.connection.execute(
            """
            INSERT INTO assets(
                id, source_root_id, source_relative_path, original_filename,
                file_size, modified_ns, media_type, display_title, series_title,
                channel_slug, daypart_slug, collection_type, collection_slug,
                holiday_slug, metadata_status, integrity_status,
                review_status, enabled_for_scheduling, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', 'ready',
                      'ready', 1, ?, ?)
            """,
            (
                asset_id,
                self.root_id,
                relative,
                f"{asset_id}.mp4",
                media_type,
                f"Asset {asset_id}",
                series,
                channel,
                None if collection_type else daypart,
                collection_type,
                collection_slug,
                collection_slug if collection_type == "holiday" else None,
                self.now,
                self.now,
            ),
        )
        self.connection.execute(
            "INSERT INTO asset_media_info(asset_id, duration_seconds) VALUES (?, ?)",
            (asset_id, duration),
        )
        self.connection.execute(
            """
            INSERT INTO asset_eligibility(
                asset_id, channel_slug, daypart_slug, eligibility_source
            ) VALUES (?, ?, ?, 'test')
            """,
            (
                asset_id,
                eligibility_channel
                if eligibility_channel is not None
                else (channel or "*"),
                None if collection_type else daypart,
            ),
        )
        if media_type in {"episode", "movie"} and duration > 15 * 60:
            targets = BreakpointAnalyzer(self.connection, self.config).target_offsets(
                round(duration * 1000))
            self.connection.execute(
                """INSERT INTO asset_break_analyses(
                    asset_id,source_signature,status,selected_offset_ms,
                    selected_method,confidence,analyzed_at)
                    VALUES (?, '1:1:breaks-v5','analyzed',?,'midpoint',0.25,?)""",
                (asset_id, targets[0] if targets else None, self.now),
            )
            for index, offset in enumerate(targets, 1):
                self.connection.execute(
                    """INSERT INTO asset_break_candidates(
                        asset_id,offset_ms,method,score,signals_json,selected,created_at)
                        VALUES (?,?,'midpoint',0.25,?,1,?)""",
                    (asset_id, offset, json.dumps({"fallback": True, "window": index,
                     "target_ms": offset}), self.now),
                )
        self.connection.commit()

    def test_cross_midnight_daypart_segments_cover_day(self):
        builder = ScheduleBuilder(self.connection, self.config)
        segments = builder.segments_for_date(date(2026, 7, 27))
        self.assertEqual(
            [(item.slug, item.starts_at.hour, item.ends_at.hour) for item in segments],
            [
                ("night", 0, 6),
                ("morning", 6, 12),
                ("afternoon", 12, 17),
                ("evening", 17, 22),
                ("night", 22, 0),
            ],
        )

    def test_holiday_special_candidates_respect_daypart_and_weekly_variant(self):
        for asset_id in (1, 2, 3):
            self.add_asset(
                asset_id, media_type="episode", daypart="night", duration=1200,
                series=f"Show {asset_id}", collection_type="holiday",
                collection_slug="halloween",
            )
        self.connection.execute(
            "UPDATE assets SET daypart_slug='night' WHERE id=1"
        )
        self.connection.execute(
            "UPDATE asset_eligibility SET daypart_slug='night' WHERE asset_id=1"
        )
        self.connection.execute(
            "UPDATE assets SET holiday_weekly_slug='snick-main' WHERE id=2"
        )
        self.connection.execute(
            "UPDATE assets SET holiday_weekly_slug='snick-finale' WHERE id=3"
        )
        self.connection.commit()
        builder = ScheduleBuilder(self.connection, self.config)
        def candidates(daypart, weekly=None):
            return {item.asset_id for item in builder._special_candidates(
                "fox", daypart, ("episode",), collection_type="holiday",
                collection_slug="halloween", rule_slug="halloween",
                holiday_weekly_slug=weekly,
            )}
        self.assertEqual(candidates("morning"), set())
        self.assertEqual(candidates("night"), {1})
        self.assertEqual(candidates("night", "snick-main"), {2})
        self.assertEqual(candidates("night", "snick-finale"), {3})

    def test_long_daypart_has_evenly_spaced_block_opportunities(self):
        self.config.data["scheduling"]["dayparts"] = [
            {"slug": "morning", "start": "06:00", "end": "18:00"},
            {"slug": "evening", "start": "18:00", "end": "22:00"},
            {"slug": "night", "start": "22:00", "end": "06:00"},
        ]
        self.config.data["blocks"]["maximum_per_daypart"] = 3
        builder = ScheduleBuilder(self.connection, self.config)
        opportunities = builder._block_opportunities_for_date(
            "fox", date(2026, 8, 10)
        )
        morning = sorted(
            item.starts_at
            for item in opportunities.values()
            if item.window_type == "daypart"
            and item.window_slug == "morning"
        )
        self.assertEqual(
            [(item.hour, item.minute) for item in morning],
            [(6, 0), (10, 0), (14, 0)],
        )

    def test_weekly_window_suppresses_surrounding_daypart_midpoint(self):
        self.config.data["scheduling"]["dayparts"] = [
            {"slug": "morning", "start": "06:00", "end": "12:00"},
            {"slug": "afternoon", "start": "12:00", "end": "16:00"},
            {"slug": "evening", "start": "16:00", "end": "20:00"},
            {"slug": "night", "start": "20:00", "end": "06:00"},
        ]
        self.config.data["seasonal"] = {
            "windows": {
                "adult-swim": {
                    "channel": "fox",
                    "weekdays": ["monday"],
                    "start": "00:00",
                    "end": "05:00",
                    "collection": "adult-swim",
                    "guide_label": "Adult Swim",
                    "priority": 300,
                    "enabled": True,
                }
            }
        }
        self.config.data["blocks"]["maximum_per_daypart"] = 3
        builder = ScheduleBuilder(self.connection, self.config)
        opportunities = builder._block_opportunities_for_date(
            "fox", date(2026, 8, 10)
        )
        by_time = {
            (item.starts_at.hour, item.starts_at.minute): item
            for item in opportunities.values()
        }
        self.assertEqual(by_time[(0, 0)].window_type, "weekly")
        self.assertNotIn((1, 0), by_time)

    def test_weekly_special_promos_use_anchored_two_hour_commercial_windows(self):
        self.config.data["seasonal"] = {"windows": {
            "snick": {
                "channel": "fox", "weekdays": ["monday"],
                "start": "20:00", "end": "22:00", "collection": "snick",
                "guide_label": "SNICK", "priority": 300, "enabled": True,
            }
        }}
        builder = ScheduleBuilder(self.connection, self.config)
        base = datetime.fromisoformat("2026-08-04T00:00:00-07:00")
        timeline = []
        for index, stamp in enumerate(("19:59:45", "20:30:00", "22:30:00")):
            starts = datetime.fromisoformat(f"2026-08-04T{stamp}-07:00")
            timeline.append(TimelineItem(
                asset_id=index + 1, starts_at=starts,
                ends_at=starts + timedelta(seconds=30), media_start_ms=0,
                media_end_ms=30_000, entry_type="commercial", title=f"Ad {index}",
                daypart_slug="evening", sequence_group=index, sequence_position=index,
                is_program=False,
            ))
        changed = builder._insert_weekly_special_promos("fox", base.date(), timeline)
        promos = [item for item in changed if item.entry_type == "weekly_special_promo_opportunity"]
        self.assertEqual(len(promos), 2)
        self.assertEqual([item.starts_at.strftime("%H:%M") for item in promos], ["20:30", "22:30"])
        self.assertTrue(all(item.asset_id is None for item in promos))
        self.assertTrue(all((item.ends_at - item.starts_at).total_seconds() == 15 for item in promos))
        self.assertEqual(sum((item.ends_at - item.starts_at).total_seconds()
                             for item in changed), 90)
        self.assertEqual(sum(item.entry_type in {"graphic_interstitial", "continuity"}
                             for item in changed), 2)
        self.assertIn('"special_name": "SNICK"', promos[0].explanation_json)
        self.assertEqual(changed[0].entry_type, "commercial")  # This ad crosses the 20:00 start.

    def test_disabled_weekly_promo_leaves_commercial_unchanged(self):
        self.config.data["seasonal"] = {"windows": {
            "snick": {
                "channel": "fox", "weekdays": ["monday"],
                "start": "20:00", "end": "22:00", "collection": "snick",
                "guide_label": "SNICK", "priority": 300, "enabled": True,
            }
        }}
        builder = ScheduleBuilder(self.connection, self.config)
        channel_id = builder.ensure_channel("fox")
        self.connection.execute(
            """INSERT INTO announcement_weekly_profiles(
                channel_id,collection_slug,identity_name,enabled,script_model,
                speech_model,voice,personality_prompt,voice_instructions,
                generic_script,maximum_words,updated_at,promo_enabled
            ) VALUES (?, 'snick', 'SNICK', 0, 'model', 'tts', 'alloy', '', '', '', 55, 'now', 0)""",
            (channel_id,),
        )
        start = datetime.fromisoformat("2026-08-03T20:30:00-07:00")
        commercial = TimelineItem(
            asset_id=1, starts_at=start, ends_at=start + timedelta(seconds=30),
            media_start_ms=0, media_end_ms=30_000, entry_type="commercial",
            title="Ad", daypart_slug="evening", sequence_group=1,
            sequence_position=0, is_program=False,
        )
        self.assertEqual(
            builder._insert_weekly_special_promos("fox", start.date(), [commercial]),
            [commercial],
        )

    def test_weekly_promo_six_day_boundary_and_long_spot_remainder(self):
        self.config.data["seasonal"] = {"windows": {
            "snick": {"channel": "fox", "weekdays": ["monday"],
                      "start": "20:00", "end": "22:00", "collection": "snick",
                      "guide_label": "SNICK", "priority": 300, "enabled": True}
        }}
        builder = ScheduleBuilder(self.connection, self.config)
        early = datetime.fromisoformat("2026-08-03T20:30:00-07:00")
        spot = TimelineItem(1, early, early + timedelta(seconds=90), 0, 90_000,
                            "commercial", "Long spot", "evening", 0, 0, False)
        self.assertEqual(builder._insert_weekly_special_promos(
            "fox", early.date(), [spot]), [spot])
        starts = early + timedelta(days=1)
        long_spot = replace(spot, starts_at=starts,
                            ends_at=starts + timedelta(seconds=90))
        short_start = starts + timedelta(hours=3)
        short_spot = TimelineItem(2, short_start,
                                  short_start + timedelta(seconds=15), 0, 15_000,
                                  "commercial", "Short spot", "evening", 0, 1, False)
        result = builder._insert_weekly_special_promos(
            "fox", starts.date(), [long_spot, short_spot])
        first = [item for item in result if item.starts_at >= starts
                 and item.ends_at <= starts + timedelta(seconds=90)]
        self.assertEqual(first[0].entry_type, "weekly_special_promo_opportunity")
        self.assertEqual((first[0].ends_at - first[0].starts_at).total_seconds(), 15)
        self.assertEqual(first[1].asset_id, 2)
        self.assertEqual(first[-1].entry_type in {"graphic_interstitial", "continuity"}, True)
        self.assertEqual(first[-1].ends_at, long_spot.ends_at)

    def test_missed_weekly_promo_window_is_not_carried_forward(self):
        self.config.data["seasonal"] = {"windows": {
            "snick": {
                "channel": "fox", "weekdays": ["monday"],
                "start": "20:00", "end": "22:00", "collection": "snick",
                "guide_label": "SNICK", "priority": 300, "enabled": True,
            }
        }}
        builder = ScheduleBuilder(self.connection, self.config)
        starts = datetime.fromisoformat("2026-08-04T22:30:00-07:00")
        timeline = [TimelineItem(
            asset_id=1, starts_at=starts, ends_at=starts.replace(second=30),
            media_start_ms=0, media_end_ms=30_000, entry_type="commercial",
            title="Ad", daypart_slug="night", sequence_group=1,
            sequence_position=0, is_program=False,
        )]
        changed = builder._insert_weekly_special_promos("fox", starts.date(), timeline)
        # The only available ad belongs to the 22:00 cadence window; the 20:00
        # opportunity is not deferred and does not create a second promo.
        self.assertEqual(sum(item.entry_type == "weekly_special_promo_opportunity"
                             for item in changed), 1)
        explanation = json.loads(changed[0].explanation_json)
        self.assertEqual(
            explanation["weekly_special_promo"]["cadence_window_start"],
            "2026-08-04T22:00:00-07:00",
        )

    def test_weekly_promo_schedule_entry_persists_as_generated_screen_opportunity(self):
        builder = ScheduleBuilder(self.connection, self.config)
        channel_id = builder.ensure_channel("fox")
        now = utc_now()
        generation_id = self.connection.execute(
            """INSERT INTO schedule_generations(
                channel_id,schedule_date,seed,configuration_fingerprint,status,
                validation_status,generated_at,activated_at
            ) VALUES (?, '2026-08-03','seed','fingerprint','building','valid',?,?)""",
            (channel_id, now, now),
        ).lastrowid
        explanation = {"weekly_special_promo": {
            "window_slug": "snick", "collection_slug": "snick",
            "special_name": "SNICK", "airing_at": "2026-08-10T20:00:00-07:00",
            "airing_ends_at": "2026-08-10T22:00:00-07:00",
        }}
        self.connection.execute(
            """INSERT INTO schedule_entries(
                generation_id,channel_id,starts_at,ends_at,entry_type,title,
                daypart_slug,sequence_position,is_program,explanation_json
            ) VALUES (?,?,'2026-08-03T20:00:00-07:00','2026-08-03T20:00:30-07:00',
                      'weekly_special_promo_opportunity','Weekly Special Promo: SNICK',
                      'evening',0,0,?)""",
            (generation_id, channel_id, json.dumps(explanation)),
        )
        self.connection.commit()
        builder._persist_announcement_opportunities(generation_id, channel_id, "fox", now)
        row = self.connection.execute(
            "SELECT opportunity_type,weekly_collection_slug,facts_json "
            "FROM announcement_opportunities"
        ).fetchone()
        self.assertEqual(row["opportunity_type"], "weekly_special_promo")
        self.assertEqual(row["weekly_collection_slug"], "snick")
        self.assertEqual(json.loads(row["facts_json"])["weekly_special_promo"]["special_name"], "SNICK")

    def test_late_complete_block_joins_at_elapsed_media_offset(self):
        self.config.data["blocks"].update(
            {
                "selection_probability": 1.0,
                "overlap_policy": "join_in_progress",
                "minimum_join_remaining_minutes": 60,
                "minimum_join_remaining_fraction": 0.50,
            }
        )
        builder = ScheduleBuilder(self.connection, self.config)
        starts_at = datetime.fromisoformat("2026-08-10T10:00:00-07:00")
        opportunity = BlockOpportunity(
            starts_at,
            datetime.fromisoformat("2026-08-10T14:00:00-07:00"),
            "morning",
            "morning",
            "daypart",
        )
        candidate = Candidate(
            100,
            "Five Hour Block",
            "Five Hour Block",
            "complete_block",
            5 * 60 * 60 * 1000,
            "fox/morning/blocks/five-hours.mp4",
            boundary_policy="may_overrun",
        )
        selection = builder._select_block(
            [candidate],
            random.Random(1),
            opportunity,
            datetime.fromisoformat("2026-08-10T11:00:00-07:00"),
            datetime.fromisoformat("2026-08-11T00:00:00-07:00"),
            set(),
        )
        self.assertIsNotNone(selection)
        self.assertEqual(selection.media_start_ms, 60 * 60 * 1000)

    def test_late_must_fit_block_is_skipped(self):
        self.config.data["blocks"]["selection_probability"] = 1.0
        builder = ScheduleBuilder(self.connection, self.config)
        starts_at = datetime.fromisoformat("2026-08-10T10:00:00-07:00")
        opportunity = BlockOpportunity(
            starts_at,
            datetime.fromisoformat("2026-08-10T14:00:00-07:00"),
            "morning",
            "morning",
            "daypart",
        )
        candidate = Candidate(
            100,
            "Strict Block",
            "Strict Block",
            "complete_block",
            3 * 60 * 60 * 1000,
            "fox/morning/blocks/strict.mp4",
            boundary_policy="must_fit",
        )
        self.assertIsNone(
            builder._select_block(
                [candidate],
                random.Random(1),
                opportunity,
                datetime.fromisoformat("2026-08-10T10:30:00-07:00"),
                datetime.fromisoformat("2026-08-11T00:00:00-07:00"),
                set(),
            )
        )

    def test_scheduler_joins_second_long_daypart_block_in_progress(self):
        self.config.data["scheduling"]["dayparts"] = [
            {"slug": "morning", "start": "06:00", "end": "14:00"},
            {"slug": "afternoon", "start": "14:00", "end": "18:00"},
            {"slug": "evening", "start": "18:00", "end": "22:00"},
            {"slug": "night", "start": "22:00", "end": "06:00"},
        ]
        self.config.data["blocks"].update(
            {
                "selection_probability": 1.0,
                "maximum_per_daypart": 2,
                "repeat_cooldown_days": 0,
                "boundary_policy": "may_overrun",
                "overlap_policy": "join_in_progress",
                "minimum_join_remaining_minutes": 60,
                "minimum_join_remaining_fraction": 0.50,
            }
        )
        for asset_id in range(1, 31):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="all-day",
                duration=1800,
                series=f"Series {asset_id}",
            )
        for asset_id in (100, 101):
            self.add_asset(
                asset_id,
                media_type="complete_block",
                daypart="morning",
                duration=5 * 60 * 60,
                series=f"Five Hour Block {asset_id}",
            )

        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 8, 10))
        blocks = [
            row
            for row in builder.entries(result.generation_id)
            if row["entry_type"] == "complete_block"
        ]
        self.assertEqual(result.validation_status, "valid")
        self.assertEqual(len(blocks), 2)
        self.assertEqual(
            datetime.fromisoformat(blocks[0]["starts_at"]).hour,
            6,
        )
        self.assertGreater(blocks[1]["media_start_ms"], 60 * 60 * 1000)
        self.assertEqual(
            datetime.fromisoformat(blocks[1]["ends_at"]),
            datetime.fromisoformat("2026-08-10T15:00:00-07:00"),
        )
        explanation = json.loads(blocks[1]["explanation_json"])
        self.assertEqual(
            explanation["media_slice"]["part"],
            "join_in_progress",
        )

    def test_block_opportunity_makes_only_one_probability_roll(self):
        self.config.data["blocks"]["selection_probability"] = 0.30
        builder = ScheduleBuilder(self.connection, self.config)
        starts_at = datetime.fromisoformat("2026-08-10T10:00:00-07:00")
        opportunity = BlockOpportunity(
            starts_at,
            datetime.fromisoformat("2026-08-10T14:00:00-07:00"),
            "morning",
            "morning",
            "daypart",
        )
        candidates = [
            Candidate(
                asset_id,
                f"Block {asset_id}",
                f"Block {asset_id}",
                "complete_block",
                2 * 60 * 60 * 1000,
                f"fox/morning/blocks/{asset_id}.mp4",
            )
            for asset_id in range(1, 6)
        ]

        class CountingRandom:
            def __init__(self):
                self.rolls = 0

            @staticmethod
            def choice(values):
                return values[0]

            def random(self):
                self.rolls += 1
                return 0.50

        rng = CountingRandom()
        self.assertIsNone(
            builder._select_block(
                candidates,
                rng,
                opportunity,
                starts_at,
                datetime.fromisoformat("2026-08-11T00:00:00-07:00"),
                set(),
            )
        )
        self.assertEqual(rng.rolls, 1)

    def test_block_fallback_bypasses_probability_and_repeat_exclusion(self):
        self.config.data["blocks"].update(
            {
                "selection_probability": 0.0,
                "fallback_when_no_programming": True,
                "fallback_relaxes_repeat_restrictions": True,
            }
        )
        builder = ScheduleBuilder(self.connection, self.config)
        starts_at = datetime.fromisoformat("2026-08-10T06:00:00-07:00")
        opportunity = BlockOpportunity(
            starts_at,
            datetime.fromisoformat("2026-08-10T12:00:00-07:00"),
            "morning",
            "morning",
            "daypart",
        )
        older = Candidate(
            100, "Older Block", "Older Block", "complete_block", 3_600_000,
            "fox/all-day/blocks/older.mp4",
        )
        newer = Candidate(
            101, "Newer Block", "Newer Block", "complete_block", 3_600_000,
            "fox/all-day/blocks/newer.mp4",
        )
        selection = builder._select_block(
            [newer, older],
            random.Random(1),
            opportunity,
            starts_at,
            datetime.fromisoformat("2026-08-11T00:00:00-07:00"),
            {100, 101},
            fallback=True,
            last_used={
                100: "2026-07-01T06:00:00.000-07:00",
                101: "2026-08-01T06:00:00.000-07:00",
            },
        )
        self.assertIsNotNone(selection)
        self.assertEqual(selection.candidate.asset_id, 100)

    def test_only_complete_block_repeats_across_dayparts_before_filler(self):
        self.config.data["blocks"].update(
            {
                "selection_probability": 0.0,
                "maximum_per_daypart": 2,
                "fallback_when_no_programming": True,
                "fallback_relaxes_repeat_restrictions": True,
            }
        )
        self.add_asset(
            100,
            media_type="complete_block",
            daypart="all-day",
            duration=3600,
            series="Only Real Programming",
        )
        self.add_asset(
            200,
            media_type="filler",
            daypart="all-day",
            duration=300,
        )

        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 8, 10))
        rows = builder.entries(result.generation_id)
        blocks = [row for row in rows if row["entry_type"] == "complete_block"]

        self.assertEqual(result.validation_status, "valid")
        self.assertGreaterEqual(len(blocks), 4)
        self.assertEqual({row["asset_id"] for row in blocks}, {100})
        self.assertIn(6, {datetime.fromisoformat(row["starts_at"]).hour for row in blocks})
        self.assertIn(12, {datetime.fromisoformat(row["starts_at"]).hour for row in blocks})

    def test_generation_is_gapless_and_excludes_complete_blocks(self):
        asset_id = 1
        for daypart in ("morning", "afternoon", "evening", "night"):
            for episode in range(3):
                self.add_asset(
                    asset_id,
                    media_type="episode",
                    daypart=daypart,
                    duration=1800,
                    series=f"{daypart} series {episode}",
                )
                asset_id += 1
        self.add_asset(
            asset_id,
            media_type="complete_block",
            daypart="evening",
            duration=3600,
        )
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        self.assertEqual(result.validation_status, "valid")
        rows = builder.entries(result.generation_id)
        self.assertTrue(rows)
        self.assertNotIn("complete_block", {row["entry_type"] for row in rows})
        self.assertEqual(rows[0]["starts_at"][:10], "2026-07-27")
        for left, right in zip(rows, rows[1:]):
            self.assertEqual(left["ends_at"], right["starts_at"])
        self.assertEqual(
            datetime.fromisoformat(rows[-1]["ends_at"]).date(),
            date(2026, 7, 28),
        )

    def test_same_day_regeneration_is_deterministic(self):
        for asset_id in range(1, 9):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="all-day",
                duration=3600,
                series=f"Series {asset_id}",
            )
        builder = ScheduleBuilder(self.connection, self.config)
        first = builder.generate_day("fox", date(2026, 7, 27))
        first_ids = [
            row["asset_id"]
            for row in builder.entries(first.generation_id, programs_only=True)
        ]
        second = builder.generate_day("fox", date(2026, 7, 27))
        second_ids = [
            row["asset_id"]
            for row in builder.entries(second.generation_id, programs_only=True)
        ]
        self.assertEqual(first_ids, second_ids)
        active = builder.active_generation("fox", date(2026, 7, 27))
        self.assertEqual(active["id"], second.generation_id)

    def test_same_day_reroll_uses_new_seed_and_different_valid_schedule(self):
        for asset_id in range(1, 9):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="all-day",
                duration=3600,
                series=f"Series {asset_id}",
            )
        builder = ScheduleBuilder(self.connection, self.config)
        target = date(2026, 7, 27)
        first = builder.generate_day("fox", target)
        first_generation = builder.active_generation("fox", target)
        first_ids = [
            row["asset_id"]
            for row in builder.entries(first.generation_id, programs_only=True)
        ]
        second = builder.generate_day("fox", target, reroll_token="manual-reroll-1")
        second_generation = builder.active_generation("fox", target)
        second_ids = [
            row["asset_id"]
            for row in builder.entries(second.generation_id, programs_only=True)
        ]
        self.assertEqual(second.validation_status, "valid")
        self.assertNotEqual(first_generation["seed"], second_generation["seed"])
        self.assertNotEqual(first_ids, second_ids)
        self.assertTrue(builder.generation_is_current("fox", target))

    def test_next_day_continues_episode_progress(self):
        for asset_id in range(1, 31):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="all-day",
                duration=3600,
                series="One Series",
            )
        builder = ScheduleBuilder(self.connection, self.config)
        first = builder.generate_day("fox", date(2026, 7, 27))
        first_ids = [
            row["asset_id"]
            for row in builder.entries(first.generation_id, programs_only=True)
        ]
        self.assertEqual(len(first_ids), len(set(first_ids)))
        second = builder.generate_day("fox", date(2026, 7, 28))
        second_ids = builder.entries(second.generation_id, programs_only=True)
        self.assertEqual(second_ids[0]["asset_id"], max(first_ids) + 1)

    def test_configured_sequence_inserts_available_interstitials(self):
        self.add_asset(
            1,
            media_type="episode",
            daypart="all-day",
            duration=1600,
            series="Series",
        )
        self.add_asset(
            2,
            media_type="bumper",
            daypart="all-day",
            duration=10,
        )
        self.add_asset(
            3,
            media_type="commercial",
            daypart="all-day",
            duration=30,
        )
        self.add_asset(
            4,
            media_type="bumper",
            daypart="all-day",
            duration=10,
        )
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        self.assertEqual(
            [row["entry_type"] for row in rows[:6]],
            ["bumper", "episode", "bumper", "commercial", "bumper", "episode"],
        )

    def test_long_episode_is_sliced_around_one_midroll(self):
        self.add_asset(1, media_type="episode", daypart="all-day", duration=1380, series="Series")
        self.add_asset(2, media_type="bumper", daypart="all-day", duration=10)
        self.add_asset(3, media_type="commercial", daypart="all-day", duration=30)
        self.add_asset(4, media_type="bumper", daypart="all-day", duration=10)
        self.connection.execute(
            """
            UPDATE asset_break_analyses SET status='manual',selected_offset_ms=600000,
                selected_method='manual',confidence=1.0,analyzed_at=? WHERE asset_id=1
            """,
            (self.now,),
        )
        self.connection.execute(
            "UPDATE asset_break_candidates SET offset_ms=600000,method='manual',score=1.0,signals_json='{\"window\":1,\"manual\":true}' WHERE asset_id=1"
        )
        self.connection.commit()
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        block_id = next(row["programming_block_id"] for row in rows if row["is_program"])
        block = [row for row in rows if row["programming_block_id"] == block_id]
        self.assertEqual(
            [row["entry_type"] for row in block[:6]],
            ["bumper", "episode", "bumper", "commercial", "bumper", "episode"],
        )
        programs = [row for row in block if row["is_program"]]
        self.assertEqual(len(programs), 2)
        self.assertEqual(programs[0]["media_end_ms"], 600000)
        self.assertEqual(programs[1]["media_start_ms"], 600000)
        self.assertEqual(programs[0]["asset_id"], programs[1]["asset_id"])

    def test_named_break_bumpers_only_play_at_matching_midroll_boundaries(self):
        self.add_asset(1, media_type="episode", daypart="all-day",
                       duration=1380, series="The Flintstones")
        for asset_id, name in (
            (2, "Ordinary Fox Kids bumper.mp4"),
            (3, "We'll be right back, so don't go.mp4"),
            (4, "Now, back to the Flintstones!.mp4"),
            (5, "Chip and Dale's Rescue Rangers will return.mp4"),
        ):
            self.add_asset(asset_id, media_type="bumper", daypart="all-day", duration=10)
            self.connection.execute(
                """UPDATE assets SET original_filename=?,source_relative_path=?
                   WHERE id=?""",
                (name, f"shared/all-day/bumpers/{name}", asset_id),
            )
        self.add_asset(6, media_type="commercial", daypart="all-day", duration=30)
        self.connection.commit()

        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        block_id = next(row["programming_block_id"] for row in rows if row["is_program"])
        block = [row for row in rows if row["programming_block_id"] == block_id]
        self.assertEqual([row["asset_id"] for row in block[:6]], [2, 1, 3, 6, 4, 1])
        self.assertNotIn(5, {row["asset_id"] for row in rows})
        # Neither a break cue nor a named return cue belongs after the last program.
        last_program = max(index for index, row in enumerate(block) if row["is_program"])
        self.assertFalse({3, 4, 5} & {row["asset_id"] for row in block[last_program + 1:]})

    def test_general_bumpers_fill_boundaries_when_named_cues_do_not_match(self):
        self.add_asset(1, media_type="episode", daypart="all-day",
                       duration=1380, series="The Simpsons")
        for asset_id, name in (
            (2, "Station bumper.mp4"),
            (3, "Now, back to the Flintstones!.mp4"),
        ):
            self.add_asset(asset_id, media_type="bumper", daypart="all-day", duration=10)
            self.connection.execute(
                "UPDATE assets SET original_filename=?,source_relative_path=? WHERE id=?",
                (name, f"shared/all-day/bumpers/{name}", asset_id),
            )
        self.add_asset(4, media_type="commercial", daypart="all-day", duration=30)
        self.connection.commit()
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        self.assertNotIn(3, {row["asset_id"] for row in rows})
        block_id = next(row["programming_block_id"] for row in rows if row["is_program"])
        block = [row for row in rows if row["programming_block_id"] == block_id]
        self.assertEqual([row["asset_id"] for row in block[:6]], [2, 1, 2, 4, 2, 1])

    def test_short_midroll_uses_general_fallback_at_the_boundary_that_cannot_fit(self):
        builder = ScheduleBuilder(self.connection, self.config)
        rng = random.Random(1)
        cursor = datetime(2026, 7, 27, 8, tzinfo=builder.timezone)
        bumpers = [
            Candidate(2, "Out", "Out", "bumper", 20000,
                      "shared/bumpers/We'll be right back.mp4"),
            Candidate(3, "In", "In", "bumper", 20000,
                      "shared/bumpers/Now back to our program.mp4"),
            Candidate(4, "General", "General", "bumper", 5000,
                      "shared/bumpers/Station identity.mp4"),
        ]
        commercials = [Candidate(5, "Ad", "Ad", "commercial", 30000,
                                 "shared/commercials/Ad.mp4")]
        pod = builder._propose_midroll(
            bumpers, commercials, 55000, None, rng, cursor, {}, set(),
            "The Flintstones", "The Flintstones",
        )
        self.assertEqual([item.asset_id for item in pod], [2, 5, 4])

    def test_saved_show_link_controls_placement_and_invalidates_old_schedule(self):
        self.add_asset(1, media_type="episode", daypart="all-day",
                       duration=1380, series="The Flintstones")
        self.add_asset(2, media_type="bumper", daypart="all-day", duration=10)
        self.add_asset(3, media_type="bumper", daypart="all-day", duration=10)
        self.add_asset(4, media_type="commercial", daypart="all-day", duration=30)
        self.connection.execute(
            """UPDATE assets SET original_filename=?,source_relative_path=?
               WHERE id=3""",
            ("Now back to the Simpsons.mp4",
             "shared/all-day/bumpers/Now back to the Simpsons.mp4"),
        )
        self.connection.commit()
        target = date(2026, 7, 27)
        initial = ScheduleBuilder(self.connection, self.config)
        initial.generate_day("fox", target)
        self.assertTrue(ScheduleBuilder(self.connection, self.config).generation_is_current("fox", target))
        save_bumper_override(self.connection, 3, "break_end", "The Flintstones")
        builder = ScheduleBuilder(self.connection, self.config)
        self.assertFalse(builder.generation_is_current("fox", target))
        result = builder.generate_day("fox", target)
        rows = builder.entries(result.generation_id)
        block_id = next(row["programming_block_id"] for row in rows if row["is_program"])
        block = [row for row in rows if row["programming_block_id"] == block_id]
        self.assertEqual([row["asset_id"] for row in block[:6]], [2, 1, 2, 4, 3, 1])

    def test_hour_slot_episode_is_sliced_around_three_midrolls(self):
        self.add_asset(
            1, media_type="episode", daypart="all-day",
            duration=2640, series="Hour Series",
        )
        self.add_asset(2, media_type="bumper", daypart="all-day", duration=10)
        self.add_asset(3, media_type="commercial", daypart="all-day", duration=30)
        self.add_asset(4, media_type="bumper", daypart="all-day", duration=10)

        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        block_id = next(
            row["programming_block_id"] for row in rows if row["is_program"]
        )
        block = [
            row for row in rows if row["programming_block_id"] == block_id
        ]
        programs = [row for row in block if row["is_program"]]

        self.assertEqual(len(programs), 4)
        self.assertEqual(
            [(row["media_start_ms"], row["media_end_ms"]) for row in programs],
            [(0, 750000), (750000, 1500000), (1500000, 2070000), (2070000, 2640000)],
        )
        self.assertEqual(
            len([
                row for row in block[:block.index(programs[-1])]
                if row["entry_type"] == "commercial"
            ]),
            3,
        )

    def test_hour_episode_spreads_ad_time_across_two_breaks_and_tail(self):
        self.config.data["scheduling"]["commercials_per_break_max"] = 3
        self.add_asset(
            1, media_type="episode", daypart="all-day",
            duration=44 * 60 + 29, series="Hour Series",
        )
        self.add_asset(2, media_type="bumper", daypart="all-day", duration=10)
        self.add_asset(3, media_type="bumper", daypart="all-day", duration=10)
        for asset_id, seconds in ((10, 120), (11, 121), (12, 142), (13, 484)):
            self.add_asset(
                asset_id, media_type="commercial", daypart="all-day",
                duration=seconds,
            )

        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        block_id = next(row["programming_block_id"] for row in rows if row["is_program"])
        block = [row for row in rows if row["programming_block_id"] == block_id]
        programs = [row for row in block if row["is_program"]]
        self.assertEqual(len(programs), 4)
        for earlier, later in zip(programs, programs[1:]):
            ad_pod = block[block.index(earlier) + 1:block.index(later)]
            self.assertIn("commercial", {row["entry_type"] for row in ad_pod})
            pod_seconds = sum(
                (datetime.fromisoformat(row["ends_at"]) -
                 datetime.fromisoformat(row["starts_at"])).total_seconds()
                for row in ad_pod
            )
            self.assertLessEqual(pod_seconds, 5 * 60)
            self.assertNotIn(13, {row["asset_id"] for row in ad_pod})
        second_break_at = datetime.fromisoformat(programs[1]["ends_at"])
        first_started_at = datetime.fromisoformat(block[0]["starts_at"])
        self.assertGreaterEqual(
            (second_break_at - first_started_at).total_seconds(), 27 * 60
        )
        self.assertLessEqual(
            (second_break_at - first_started_at).total_seconds(), 35 * 60
        )
        tail = block[block.index(programs[-1]) + 1:]
        self.assertIn("commercial", {row["entry_type"] for row in tail})
        tail_commercial_seconds = sum(
            (datetime.fromisoformat(row["ends_at"]) -
             datetime.fromisoformat(row["starts_at"])).total_seconds()
            for row in tail if row["entry_type"] == "commercial"
        )
        self.assertLessEqual(tail_commercial_seconds, 5 * 60)
        program_segments = [
            (row["media_end_ms"] or 2649 * 1000) - (row["media_start_ms"] or 0)
            for row in programs
        ]
        self.assertLessEqual(max(program_segments), 15 * 60 * 1000)

    def test_one_bumper_is_reused_for_opening_and_midroll_bookends(self):
        self.add_asset(
            1, media_type="episode", daypart="all-day",
            duration=1380, series="Series",
        )
        self.add_asset(
            2, media_type="bumper", daypart="all-day", duration=10,
        )
        self.add_asset(
            3, media_type="commercial", daypart="all-day", duration=30,
        )

        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        block_id = next(
            row["programming_block_id"] for row in rows if row["is_program"]
        )
        block = [
            row for row in rows if row["programming_block_id"] == block_id
        ]

        self.assertEqual(
            [row["entry_type"] for row in block[:6]],
            ["bumper", "episode", "bumper", "commercial", "bumper", "episode"],
        )
        self.assertEqual(
            [row["asset_id"] for row in block if row["entry_type"] == "bumper"],
            [2, 2, 2],
        )

    def test_two_recent_bumpers_fill_every_opening_and_midroll_position(self):
        self.config.data["scheduling"]["bumper_cooldown_seconds"] = 86400
        self.add_asset(
            1, media_type="episode", daypart="all-day",
            duration=1380, series="Series",
        )
        self.add_asset(
            2, media_type="bumper", daypart="all-day", duration=10,
        )
        self.add_asset(
            3, media_type="bumper", daypart="all-day", duration=10,
        )
        self.add_asset(
            4, media_type="commercial", daypart="all-day", duration=30,
        )

        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        block_id = next(
            row["programming_block_id"] for row in rows if row["is_program"]
        )
        block = [
            row for row in rows if row["programming_block_id"] == block_id
        ]
        bumpers = [row for row in block if row["entry_type"] == "bumper"]

        self.assertEqual(
            [row["entry_type"] for row in block[:6]],
            ["bumper", "episode", "bumper", "commercial", "bumper", "episode"],
        )
        self.assertEqual(len(bumpers), 3)
        self.assertEqual({row["asset_id"] for row in bumpers}, {2, 3})

    def test_long_episode_uses_commercial_only_midroll_without_bumpers(self):
        self.add_asset(
            1, media_type="episode", daypart="all-day",
            duration=1380, series="Series",
        )
        self.add_asset(
            2, media_type="commercial", daypart="all-day", duration=30,
        )
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        block_id = next(
            row["programming_block_id"] for row in rows if row["is_program"]
        )
        block = [row for row in rows if row["programming_block_id"] == block_id]
        programs = [row for row in block if row["is_program"]]
        between = block[block.index(programs[0]) + 1:block.index(programs[1])]
        self.assertEqual(len(programs), 2)
        self.assertTrue(between)
        self.assertEqual({row["entry_type"] for row in between}, {"commercial"})

    def test_long_episode_remains_intact_without_a_fitting_commercial(self):
        self.add_asset(
            1, media_type="episode", daypart="all-day",
            duration=1380, series="Series",
        )
        self.add_asset(
            2, media_type="commercial", daypart="all-day", duration=600,
        )
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        block_id = next(
            row["programming_block_id"] for row in rows if row["is_program"]
        )
        block = [row for row in rows if row["programming_block_id"] == block_id]
        self.assertEqual(len([row for row in block if row["is_program"]]), 1)

    def test_only_promo_can_be_reused_when_it_is_the_only_gap_fit(self):
        self.add_asset(1, media_type="episode", daypart="all-day",
                       duration=1380, series="Series")
        self.add_asset(2, media_type="promo", daypart="all-day", duration=30)
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        block_id = next(row["programming_block_id"] for row in rows if row["is_program"])
        block = [row for row in rows if row["programming_block_id"] == block_id]
        self.assertEqual([row["entry_type"] for row in block[:3]],
                         ["episode", "promo", "episode"])
        self.assertEqual(sum(row["asset_id"] == 2 for row in block), 2)
        promos = [row for row in rows if row["asset_id"] == 2]
        self.assertGreater(len(promos), 1)
        # Cooldown is a preference: the only suitable promo may be reused
        # rather than leaving an otherwise useful opportunity empty.

    def test_promo_can_fill_gap_when_commercial_pod_is_disabled(self):
        self.config.data["scheduling"].update(
            commercials_per_break_min=0, commercials_per_break_max=0,
            midroll_enabled=False,
        )
        self.add_asset(1, media_type="episode", daypart="all-day",
                       duration=1740, series="Series")
        self.add_asset(2, media_type="promo", daypart="all-day", duration=30)
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        self.assertEqual([row["entry_type"] for row in rows[:3]],
                         ["episode", "announcement_opportunity", "promo"])
        promo = rows[2]
        self.assertEqual((datetime.fromisoformat(promo["ends_at"])
                          - datetime.fromisoformat(promo["starts_at"])).total_seconds(), 30)

    def test_promo_pools_combine_shared_and_channel_but_preserve_scope(self):
        for asset_id, channel, daypart in (
            (1, "fox", "night"), (2, "*", "night"),
            (3, "nickelodeon", "night"), (4, "fox", "morning"),
        ):
            self.add_asset(asset_id, media_type="promo", channel=channel,
                           daypart=daypart, duration=30)
        builder = ScheduleBuilder(self.connection, self.config)
        context = builder.rules.context(
            "fox", datetime(2026, 7, 27, 23, tzinfo=builder.timezone))
        programs, bumpers, commercials, filler, _ = builder._pools_for_context(
            "fox", "night", context)
        self.assertFalse(programs or bumpers)
        self.assertEqual({item.asset_id for item in commercials}, {1, 2})
        self.assertEqual(commercials, filler)
        self.assertTrue(all(item.cooldown_seconds == builder.commercial_cooldown
                            for item in filler))

    def test_holiday_promos_share_limit_across_break_and_filler_roles(self):
        self.config.data["seasonal"] = {"holidays": {"halloween": {
            "start": "10-01", "end": "10-31", "maximum_per_break": 1,
            "cooldown_seconds": 0, "weights": {"promo": 2.0},
        }}}
        self.add_asset(1, media_type="episode", daypart="all-day",
                       duration=1380, series="Series")
        for asset_id in (2, 3, 4):
            self.add_asset(asset_id, media_type="promo", daypart="all-day",
                           duration=30, collection_type="holiday",
                           collection_slug="halloween")
        builder = ScheduleBuilder(self.connection, self.config)
        context = builder.rules.context(
            "fox", datetime(2026, 10, 10, 10, tzinfo=builder.timezone))
        _, _, commercials, filler, _ = builder._pools_for_context("fox", "morning", context)
        self.assertEqual(commercials, filler)
        self.assertTrue(all(item.weight == 2.0 and item.cooldown_seconds == 0
                            for item in filler))
        result = builder.generate_day("fox", date(2026, 10, 10))
        rows = builder.entries(result.generation_id)
        block_id = next(row["programming_block_id"] for row in rows if row["is_program"])
        promos = [row for row in rows if row["programming_block_id"] == block_id
                  and row["entry_type"] == "promo"]
        self.assertEqual(len(promos), 1)

    def test_fifteen_minute_episode_has_opening_bumper_but_no_midroll(self):
        self.add_asset(1, media_type="episode", daypart="all-day", duration=900, series="Series")
        self.add_asset(2, media_type="bumper", daypart="all-day", duration=10)
        self.add_asset(3, media_type="commercial", daypart="all-day", duration=30)
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        block_id = next(row["programming_block_id"] for row in rows if row["is_program"])
        block = [row for row in rows if row["programming_block_id"] == block_id]
        programs = [row for row in block if row["is_program"]]
        self.assertEqual(len(programs), 1)
        self.assertEqual(programs[0]["media_start_ms"], 0)
        self.assertEqual(programs[0]["media_end_ms"], 900000)
        self.assertEqual(block[0]["entry_type"], "bumper")

    def test_episode_just_over_fifteen_minutes_is_midroll_eligible(self):
        self.add_asset(1, media_type="episode", daypart="all-day", duration=901, series="Series")
        self.add_asset(2, media_type="bumper", daypart="all-day", duration=10)
        self.add_asset(3, media_type="commercial", daypart="all-day", duration=30)
        self.add_asset(4, media_type="bumper", daypart="all-day", duration=10)
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        block_id = next(row["programming_block_id"] for row in rows if row["is_program"])
        programs = [
            row for row in rows
            if row["programming_block_id"] == block_id and row["is_program"]
        ]
        self.assertEqual(len(programs), 2)
        self.assertEqual(programs[0]["media_end_ms"], 450500)
        self.assertEqual(programs[1]["media_start_ms"], 450500)

    def test_movie_gets_opening_bumper_but_is_not_sliced(self):
        self.add_asset(1, media_type="movie", daypart="all-day", duration=5340, series="Movie")
        self.add_asset(2, media_type="bumper", daypart="all-day", duration=10)
        self.add_asset(3, media_type="commercial", daypart="all-day", duration=30)
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        block_id = next(row["programming_block_id"] for row in rows if row["is_program"])
        block = [row for row in rows if row["programming_block_id"] == block_id]
        self.assertEqual(block[0]["entry_type"], "bumper")
        self.assertEqual(len([row for row in block if row["is_program"]]), 1)

    def test_short_package_gets_one_opening_bumper_and_no_internal_break(self):
        for asset_id in range(1, 4):
            self.add_asset(
                asset_id, media_type="short", daypart="all-day",
                duration=420, series="Cartoon Shorts"
            )
        self.add_asset(10, media_type="bumper", daypart="all-day", duration=10)
        self.add_asset(11, media_type="commercial", daypart="all-day", duration=30)
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        block_id = next(row["programming_block_id"] for row in rows if row["is_program"])
        block = [row for row in rows if row["programming_block_id"] == block_id]
        programs = [row for row in block if row["is_program"]]
        self.assertEqual(len(programs), 3)
        first_program = block.index(programs[0])
        last_program = block.index(programs[-1])
        self.assertEqual(block[0]["entry_type"], "bumper")
        self.assertNotIn(
            "commercial", {row["entry_type"] for row in block[first_program:last_program]}
        )

    def test_bumpers_only_appear_in_explicit_break_bookends(self):
        for asset_id in range(1, 9):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="all-day",
                duration=1700,
                series=f"Series {asset_id}",
            )
        self.add_asset(
            20,
            media_type="bumper",
            daypart="all-day",
            duration=10,
        )
        self.add_asset(
            21,
            media_type="bumper",
            daypart="all-day",
            duration=10,
        )
        self.add_asset(
            30,
            media_type="commercial",
            daypart="all-day",
            duration=30,
        )
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        bumpers = [row for row in rows if row["entry_type"] == "bumper"]
        self.assertTrue(bumpers)
        self.assertTrue(all(row["sequence_group"] is not None for row in bumpers))
        for left, right in zip(rows, rows[1:]):
            self.assertFalse(
                left["entry_type"] == "bumper"
                and right["entry_type"] == "bumper"
            )
        per_group: dict[int, int] = {}
        for row in bumpers:
            per_group[row["sequence_group"]] = (
                per_group.get(row["sequence_group"], 0) + 1
            )
        # One opening station bumper plus two midroll bookends.
        self.assertLessEqual(max(per_group.values()), 3)

    def test_missing_commercial_pool_does_not_create_back_to_back_bumpers(self):
        self.add_asset(
            1,
            media_type="episode",
            daypart="all-day",
            duration=1700,
            series="Series",
        )
        self.add_asset(
            2,
            media_type="bumper",
            daypart="all-day",
            duration=10,
        )
        self.add_asset(
            3,
            media_type="bumper",
            daypart="all-day",
            duration=10,
        )
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        for left, right in zip(rows, rows[1:]):
            self.assertFalse(
                left["entry_type"] == "bumper"
                and right["entry_type"] == "bumper"
            )
        per_group: dict[int, int] = {}
        for row in rows:
            if row["entry_type"] == "bumper":
                per_group[row["sequence_group"]] = (
                    per_group.get(row["sequence_group"], 0) + 1
                )
        self.assertTrue(per_group)
        self.assertLessEqual(max(per_group.values()), 1)

    def test_short_episodes_pair_with_next_episode_from_same_series(self):
        self.add_asset(
            1,
            media_type="episode",
            daypart="all-day",
            duration=660,
            series="Short Series",
        )
        self.add_asset(
            2,
            media_type="episode",
            daypart="all-day",
            duration=660,
            series="Short Series",
        )
        for asset_id in range(10, 14):
            self.add_asset(
                asset_id,
                media_type="commercial",
                daypart="all-day",
                duration=30,
            )

        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        first_half_hour_end = datetime.fromisoformat(
            "2026-07-27T00:30:00-07:00"
        )
        first_package_programs = [
            row
            for row in rows
            if row["is_program"]
            and datetime.fromisoformat(row["starts_at"]) < first_half_hour_end
        ]
        self.assertEqual(
            [row["asset_id"] for row in first_package_programs],
            [1, 2],
        )
        first_index = rows.index(first_package_programs[0])
        second_index = rows.index(first_package_programs[1])
        between = rows[first_index + 1:second_index]
        self.assertIn("commercial", {row["entry_type"] for row in between})
        self.assertNotEqual(
            first_package_programs[0]["sequence_group"],
            first_package_programs[1]["sequence_group"],
        )

    def test_paired_episode_boundary_replaces_internal_midroll(self):
        self.add_asset(
            1, media_type="episode", daypart="all-day",
            duration=960, series="Segmented Series"
        )
        self.add_asset(
            2, media_type="episode", daypart="all-day",
            duration=600, series="Segmented Series"
        )
        self.add_asset(
            10, media_type="commercial", daypart="all-day", duration=30
        )

        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        block_id = next(row["programming_block_id"] for row in rows if row["is_program"])
        block = [row for row in rows if row["programming_block_id"] == block_id]
        programs = [row for row in block if row["is_program"]]
        self.assertEqual([row["asset_id"] for row in programs], [1, 2])
        self.assertEqual(len(programs), 2)
        first_index = block.index(programs[0])
        second_index = block.index(programs[1])
        self.assertIn(
            "commercial",
            {row["entry_type"] for row in block[first_index + 1:second_index]},
        )
        first_package = [
            row for row in rows if row["starts_at"] < "2026-07-27T00:30:00"
        ]
        last_program = max(
            (row for row in first_package if row["is_program"]),
            key=lambda row: row["ends_at"],
        )
        up_next = next(
            row for row in first_package
            if row["entry_type"] == "announcement_opportunity"
        )
        self.assertEqual(up_next["starts_at"], last_program["ends_at"])

    def test_programming_blocks_receive_exact_up_next_opportunities(self):
        for asset_id in range(1, 8):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="all-day",
                duration=1260,
                series=f"Series {asset_id}",
            )
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        programs = [row for row in rows if row["is_program"]]
        opportunities = [
            row for row in rows if row["entry_type"] == "announcement_opportunity"
        ]
        self.assertTrue(programs)
        self.assertTrue(opportunities)
        self.assertTrue(all(row["programming_block_id"] for row in programs))
        self.assertTrue(
            all(
                (
                    datetime.fromisoformat(row["ends_at"])
                    - datetime.fromisoformat(row["starts_at"])
                ).total_seconds() == 30
                for row in opportunities
            )
        )
        persisted = self.connection.execute(
            "SELECT COUNT(*) AS total FROM announcement_opportunities WHERE generation_id=?",
            (result.generation_id,),
        ).fetchone()["total"]
        self.assertEqual(persisted, len(opportunities))

    def test_station_graphic_fills_unresolved_time_after_up_next(self):
        self.add_asset(
            1,
            media_type="episode",
            daypart="all-day",
            duration=1200,
            series="Twenty Minute Show",
        )
        self.connection.execute(
            "INSERT INTO channels(id,slug,display_name,enabled) VALUES (1,'fox','FOX',1)"
        )
        graphic = self.temporary / "media" / "fox" / "graphics" / "interstitials" / "card.png"
        graphic.parent.mkdir(parents=True)
        graphic.write_bytes(b"test graphic")
        self.connection.execute(
            """
            INSERT INTO graphic_assets(
                channel_id,relative_path,purpose,source_kind,file_size,modified_ns,
                validation_status,provenance,first_seen_at,last_seen_at
            ) VALUES (1,'fox/graphics/interstitials/card.png','interstitials',
                      'image',12,1,'valid','user',?,?)
            """,
            (self.now, self.now),
        )
        self.connection.commit()
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        first_package = [
            row for row in rows
            if row["starts_at"] < "2026-07-27T00:30:00"
        ]
        self.assertEqual(
            [row["entry_type"] for row in first_package[-2:]],
            ["announcement_opportunity", "graphic_interstitial"],
        )

    def test_station_graphic_is_last_resort_after_commercial_media(self):
        self.add_asset(
            1, media_type="episode", daypart="all-day", duration=1200,
            series="Twenty Minute Show",
        )
        self.add_asset(
            2, media_type="commercial", daypart="all-day", duration=30,
        )
        self.connection.execute(
            "INSERT INTO channels(id,slug,display_name,enabled) VALUES (1,'fox','FOX',1)"
        )
        graphic = self.temporary / "media" / "fox" / "graphics" / "interstitials" / "card.png"
        graphic.parent.mkdir(parents=True)
        graphic.write_bytes(b"test graphic")
        self.connection.execute(
            """
            INSERT INTO graphic_assets(
                channel_id,relative_path,purpose,source_kind,file_size,modified_ns,
                validation_status,provenance,first_seen_at,last_seen_at
            ) VALUES (1,'fox/graphics/interstitials/card.png','interstitials',
                      'image',12,1,'valid','user',?,?)
            """,
            (self.now, self.now),
        )
        self.connection.commit()
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        first_package = [
            row for row in rows
            if row["starts_at"] < "2026-07-27T00:30:00"
        ]
        graphic_index = next(
            index for index, row in enumerate(first_package)
            if row["entry_type"] == "graphic_interstitial"
        )
        self.assertEqual(
            [row["entry_type"] for row in first_package[graphic_index - 1:graphic_index + 1]],
            ["commercial", "graphic_interstitial"],
        )
        self.assertIn(
            "announcement_opportunity",
            [row["entry_type"] for row in first_package[:graphic_index]],
        )
        self.assertEqual(
            first_package[graphic_index - 1]["ends_at"],
            first_package[graphic_index]["starts_at"],
        )

    def test_large_gap_gets_best_fit_overflow_commercials_before_graphic(self):
        self.config.data["scheduling"]["gap_fill_commercials_max"] = 3
        builder = ScheduleBuilder(self.connection, self.config)
        pool = [
            Candidate(
                asset_id=index,
                title=f"Commercial {index}",
                series=f"Commercial {index}",
                media_type="commercial",
                duration_ms=duration * 1000,
                source_relative_path=f"commercials/{index}.mp4",
            )
            for index, duration in enumerate((30, 46, 60, 72, 80, 121, 142), start=1)
        ]
        selected = builder._gap_commercial_pack(
            pool,
            random.Random(7),
            datetime.fromisoformat("2026-07-27T19:56:48-07:00"),
            {},
            162_000,
            10_000,
            None,
        )
        total = sum(item.duration_ms for item in selected)
        self.assertLessEqual(total, 152_000)
        self.assertLessEqual(162_000 - total, 20_000)
        self.assertLessEqual(len(selected), 3)

    def test_three_seven_minute_episodes_fill_programming_target(self):
        for asset_id in range(1, 4):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="all-day",
                duration=420,
                series="Cartoon Shorts",
            )

        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        first_half_hour_end = datetime.fromisoformat(
            "2026-07-27T00:30:00-07:00"
        )
        programs = [
            row
            for row in rows
            if row["is_program"]
            and datetime.fromisoformat(row["starts_at"]) < first_half_hour_end
        ]
        self.assertEqual([row["asset_id"] for row in programs], [1, 2, 3])
        self.assertEqual(
            sum(
                (
                    datetime.fromisoformat(row["ends_at"])
                    - datetime.fromisoformat(row["starts_at"])
                ).total_seconds()
                for row in programs
            ),
            1260,
        )

    def test_four_five_minute_short_assets_reach_programming_target(self):
        for asset_id in range(1, 5):
            self.add_asset(
                asset_id,
                media_type="short",
                daypart="all-day",
                duration=300,
                series="Music Videos",
            )

        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        programs = [
            row
            for row in rows
            if row["is_program"]
            and row["starts_at"] < "2026-07-27T00:30:00"
        ]
        self.assertEqual([row["asset_id"] for row in programs], [1, 2, 3, 4])

    def test_programming_target_is_a_minimum_and_interstitials_shrink(self):
        self.add_asset(
            1,
            media_type="episode",
            daypart="all-day",
            duration=1080,
            series="Target Series",
        )
        self.add_asset(
            2,
            media_type="episode",
            daypart="all-day",
            duration=540,
            series="Target Series",
        )
        for asset_id in (10, 11):
            self.add_asset(
                asset_id,
                media_type="commercial",
                daypart="all-day",
                duration=120,
            )

        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = [
            row
            for row in builder.entries(result.generation_id)
            if row["starts_at"] < "2026-07-27T00:30:00"
        ]
        program_asset_order = []
        for row in rows:
            if row["is_program"] and (
                not program_asset_order
                or program_asset_order[-1] != row["asset_id"]
            ):
                program_asset_order.append(row["asset_id"])
        self.assertEqual(program_asset_order, [1, 2])
        self.assertEqual(
            len([row for row in rows if row["entry_type"] == "commercial"]),
            1,
        )
        self.assertEqual(rows[-1]["ends_at"], "2026-07-27T00:30:00.000-07:00")

    def test_programming_package_does_not_mix_series(self):
        self.add_asset(
            1,
            media_type="episode",
            daypart="all-day",
            duration=1080,
            series="Series A",
        )
        self.add_asset(
            2,
            media_type="episode",
            daypart="all-day",
            duration=540,
            series="Series B",
        )

        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        programs = [
            row
            for row in rows
            if row["is_program"]
            and row["starts_at"] < "2026-07-27T00:30:00"
        ]
        self.assertEqual(len(programs), 1)

    def test_next_same_series_item_is_not_skipped_when_it_cannot_fit(self):
        for asset_id, duration in ((1, 1080), (2, 780), (3, 60)):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="all-day",
                duration=duration,
                series="Ordered Series",
            )

        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        programs = [
            row
            for row in rows
            if row["is_program"]
            and row["starts_at"] < "2026-07-27T00:30:00"
        ]
        self.assertEqual([row["asset_id"] for row in programs], [1])

    def test_twenty_two_minute_episode_satisfies_target_alone(self):
        for asset_id in (1, 2):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="all-day",
                duration=1320,
                series="Long Series",
            )

        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        programs = [
            row
            for row in rows
            if row["is_program"]
            and row["starts_at"] < "2026-07-27T00:30:00"
        ]
        self.assertEqual([row["asset_id"] for row in programs], [1])

    def test_pairing_can_omit_commercials_to_fit_half_hour(self):
        self.add_asset(
            1,
            media_type="episode",
            daypart="all-day",
            duration=895,
            series="Nearly Full Pair",
        )
        self.add_asset(
            2,
            media_type="episode",
            daypart="all-day",
            duration=895,
            series="Nearly Full Pair",
        )
        self.add_asset(
            10,
            media_type="commercial",
            daypart="all-day",
            duration=30,
        )

        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        first_half_hour_end = datetime.fromisoformat(
            "2026-07-27T00:30:00-07:00"
        )
        first_package = [
            row
            for row in rows
            if datetime.fromisoformat(row["starts_at"]) < first_half_hour_end
        ]
        self.assertEqual(
            [row["asset_id"] for row in first_package if row["is_program"]],
            [1, 2],
        )
        self.assertNotIn(
            "commercial",
            {row["entry_type"] for row in first_package},
        )
        self.assertEqual(
            first_package[-1]["ends_at"],
            "2026-07-27T00:30:00.000-07:00",
        )

    def test_daypart_remainder_uses_commercial_before_continuity(self):
        self.config.data["blocks"].update(
            {
                "selection_probability": 1.0,
                "repeat_cooldown_days": 0,
            }
        )
        self.add_asset(
            1,
            media_type="complete_block",
            daypart="evening",
            duration=3639,
            series="Uneven Block",
        )
        self.add_asset(
            2,
            media_type="episode",
            daypart="evening",
            duration=1800,
            series="Evening Series",
        )
        self.add_asset(
            3,
            media_type="commercial",
            daypart="evening",
            duration=120,
        )

        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 7, 27))
        rows = builder.entries(result.generation_id)
        closing_entries = [
            row
            for row in rows
            if row["daypart_slug"] == "evening"
            and row["starts_at"] >= "2026-07-27T21:30:00"
        ]

        self.assertEqual(
            [row["entry_type"] for row in closing_entries],
            ["announcement_opportunity", "commercial", "continuity"],
        )
        self.assertEqual(closing_entries[1]["asset_id"], 3)
        self.assertEqual(
            closing_entries[0]["starts_at"],
            "2026-07-27T21:33:09.000-07:00",
        )
        self.assertEqual(
            closing_entries[-1]["ends_at"],
            "2026-07-27T22:00:00.000-07:00",
        )
        self.assertEqual(
            (
                datetime.fromisoformat(closing_entries[0]["ends_at"])
                - datetime.fromisoformat(closing_entries[0]["starts_at"])
            ).total_seconds(),
            30,
        )

    def test_halloween_boosts_channel_programs_and_explains_entries(self):
        self.config.data["seasonal"] = {
            "holidays": [
                {
                    "slug": "halloween",
                    "start": "10-01",
                    "end": "10-31",
                    "mode": "boost",
                    "priority": 200,
                    "maximum_per_break": 1,
                    "weights": {"episode": 4.0, "commercial": 3.0},
                }
            ]
        }
        for asset_id in range(1, 20):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="all-day",
                duration=3600,
                series=f"Base {asset_id}",
            )
        self.add_asset(
            100,
            media_type="episode",
            daypart="all-day",
            duration=3600,
            series="Treehouse",
            collection_type="holiday",
            collection_slug="halloween",
        )
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 10, 10))
        programs = builder.entries(result.generation_id, programs_only=True)
        holiday = next(row for row in programs if row["asset_id"] == 100)
        explanation = holiday["explanation_json"]
        self.assertIn('"rule": "halloween"', explanation)
        self.assertIn('"weight_multiplier": 4.0', explanation)
        influences = self.connection.execute(
            """
            SELECT * FROM schedule_entry_influences
            WHERE entry_id = ? AND rule_slug = 'halloween'
            """,
            (holiday["id"],),
        ).fetchone()
        self.assertIsNotNone(influences)

    def test_channel_holiday_program_does_not_leak_to_another_channel(self):
        self.config.data["seasonal"] = {
            "holidays": [
                {
                    "slug": "christmas",
                    "start": "12-01",
                    "end": "12-31",
                    "mode": "replace",
                }
            ]
        }
        for channel, starting_id in (("fox", 1), ("nickelodeon", 30)):
            for offset in range(24):
                self.add_asset(
                    starting_id + offset,
                    media_type="episode",
                    daypart="all-day",
                    duration=3600,
                    series=f"{channel} base {offset}",
                    channel=channel,
                )
        self.add_asset(
            100,
            media_type="episode",
            daypart="all-day",
            duration=3600,
            series="FOX Christmas",
            channel="fox",
            collection_type="holiday",
            collection_slug="christmas",
        )
        builder = ScheduleBuilder(self.connection, self.config)
        fox = builder.generate_day("fox", date(2026, 12, 10))
        nick = builder.generate_day("nickelodeon", date(2026, 12, 10))
        self.assertIn(
            100,
            {row["asset_id"] for row in builder.entries(fox.generation_id, programs_only=True)},
        )
        self.assertNotIn(
            100,
            {row["asset_id"] for row in builder.entries(nick.generation_id, programs_only=True)},
        )

    def test_cross_midnight_window_applies_on_both_dates(self):
        self.config.data["seasonal"] = {
            "windows": [
                {
                    "slug": "adult-swim",
                    "channel": "cartoon-network",
                    "weekdays": ["sunday"],
                    "start": "23:00",
                    "end": "05:00",
                    "collection": "adult-swim",
                    "guide_label": "Adult Swim",
                    "enabled": True,
                }
            ]
        }
        for asset_id in range(1, 30):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="all-day",
                duration=1800,
                series=f"CN Base {asset_id}",
                channel="cartoon-network",
            )
        for asset_id in range(100, 114):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="all-day",
                duration=1800,
                series=f"Adult Swim {asset_id}",
                channel="cartoon-network",
                collection_type="weekly",
                collection_slug="adult-swim",
            )
        builder = ScheduleBuilder(self.connection, self.config)
        sunday = builder.generate_day("cartoon-network", date(2026, 8, 2))
        monday = builder.generate_day("cartoon-network", date(2026, 8, 3))
        sunday_window = [
            row
            for row in builder.entries(sunday.generation_id, programs_only=True)
            if datetime.fromisoformat(row["starts_at"]).hour >= 23
        ]
        monday_window = [
            row
            for row in builder.entries(monday.generation_id, programs_only=True)
            if datetime.fromisoformat(row["starts_at"]).hour < 5
        ]
        self.assertTrue(sunday_window)
        self.assertTrue(monday_window)
        self.assertEqual(
            {row["programming_owner"] for row in sunday_window + monday_window},
            {"adult-swim"},
        )
        self.assertEqual(
            {row["guide_label"] for row in sunday_window + monday_window},
            {"Adult Swim"},
        )

    def test_weekly_block_only_collection_never_falls_back_to_base(self):
        self.config.data["seasonal"] = {
            "windows": [
                {
                    "slug": "snick",
                    "channel": "nickelodeon",
                    "weekdays": ["saturday"],
                    "start": "20:00",
                    "end": "22:00",
                    "collection": "snick",
                    "guide_label": "SNICK",
                    "enabled": True,
                }
            ]
        }
        for asset_id in range(1, 49):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="all-day",
                duration=1800,
                series=f"Nick at Nite {asset_id}",
                channel="nickelodeon",
            )
        self.add_asset(
            100,
            media_type="complete_block",
            daypart="all-day",
            duration=7200,
            series="SNICK Block",
            channel="nickelodeon",
            collection_type="weekly",
            collection_slug="snick",
        )

        builder = ScheduleBuilder(self.connection, self.config)
        first = builder.generate_day("nickelodeon", date(2026, 7, 25))
        second = builder.generate_day("nickelodeon", date(2026, 8, 1))
        first_blocks = [
            row for row in builder.entries(first.generation_id)
            if row["entry_type"] == "complete_block"
        ]
        second_blocks = [
            row for row in builder.entries(second.generation_id)
            if row["entry_type"] == "complete_block"
        ]

        self.assertEqual([row["asset_id"] for row in first_blocks], [100])
        self.assertEqual([row["asset_id"] for row in second_blocks], [100])
        self.assertEqual(second_blocks[0]["starts_at"][11:16], "20:00")
        self.assertEqual(second_blocks[0]["programming_owner"], "snick")
        self.assertEqual(second_blocks[0]["guide_label"], "SNICK")

    def test_weekly_collection_owns_every_media_pool(self):
        self.config.data["seasonal"] = {
            "windows": [
                {
                    "slug": "snick",
                    "channel": "nickelodeon",
                    "weekdays": ["saturday"],
                    "start": "20:00",
                    "end": "22:00",
                    "collection": "snick",
                    "guide_label": "SNICK",
                    "enabled": True,
                }
            ]
        }
        media = (
            ("episode", 1, 101),
            ("bumper", 2, 102),
            ("promo", 3, 103),
            ("commercial", 4, 104),
            ("filler", 5, 105),
            ("station_id", 6, 106),
            ("complete_block", 7, 107),
        )
        for media_type, base_id, weekly_id in media:
            self.add_asset(
                base_id,
                media_type=media_type,
                daypart="all-day",
                duration=1800 if media_type in {"episode", "complete_block"} else 30,
                series="Base",
                channel="nickelodeon",
            )
            self.add_asset(
                weekly_id,
                media_type=media_type,
                daypart="all-day",
                duration=1800 if media_type in {"episode", "complete_block"} else 30,
                series="SNICK",
                channel="nickelodeon",
                collection_type="weekly",
                collection_slug="snick",
            )

        builder = ScheduleBuilder(self.connection, self.config)
        context = builder.rules.context(
            "nickelodeon",
            datetime(2026, 8, 1, 20, 0, tzinfo=builder.timezone),
        )
        programs, bumpers, commercials, filler, _ = builder._pools_for_context(
            "nickelodeon", "evening", context
        )
        blocks = builder._block_pool_for_context(
            "nickelodeon", "evening", context
        )

        self.assertEqual({item.asset_id for item in programs}, {101})
        self.assertEqual({item.asset_id for item in bumpers}, {102})
        self.assertEqual({item.asset_id for item in commercials}, {103, 104})
        self.assertEqual({item.asset_id for item in filler}, {103, 105, 106})
        self.assertEqual({item.asset_id for item in blocks}, {107})

    def test_weekly_block_keeps_probability_when_weekly_programming_exists(self):
        self.config.data["seasonal"] = {
            "windows": [
                {
                    "slug": "snick",
                    "channel": "nickelodeon",
                    "weekdays": ["saturday"],
                    "start": "20:00",
                    "end": "22:00",
                    "collection": "snick",
                    "guide_label": "SNICK",
                    "enabled": True,
                }
            ]
        }
        for asset_id in range(100, 104):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="all-day",
                duration=1800,
                series="SNICK Shows",
                channel="nickelodeon",
                collection_type="weekly",
                collection_slug="snick",
            )
        self.add_asset(
            200,
            media_type="complete_block",
            daypart="all-day",
            duration=7200,
            series="SNICK Block",
            channel="nickelodeon",
            collection_type="weekly",
            collection_slug="snick",
        )
        for asset_id in range(1, 5):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="all-day",
                duration=1800,
                series="Nick at Nite",
                channel="nickelodeon",
            )

        builder = ScheduleBuilder(self.connection, self.config)
        no_block = builder.generate_day("nickelodeon", date(2026, 8, 1))
        window_rows = [
            row for row in builder.entries(no_block.generation_id, programs_only=True)
            if 20 <= datetime.fromisoformat(row["starts_at"]).hour < 22
        ]
        self.assertTrue(window_rows)
        self.assertNotIn(200, {row["asset_id"] for row in window_rows})
        self.assertTrue({row["asset_id"] for row in window_rows} <= set(range(100, 104)))

        self.config.data["blocks"]["selection_probability"] = 1.0
        builder = ScheduleBuilder(self.connection, self.config)
        with_block = builder.generate_day("nickelodeon", date(2026, 8, 8))
        self.assertIn(
            200,
            {row["asset_id"] for row in builder.entries(with_block.generation_id)},
        )

    def test_holiday_block_retains_probability_when_regular_programming_exists(self):
        self.config.data["seasonal"] = {
            "holidays": [
                {
                    "slug": "halloween",
                    "start": "10-01",
                    "end": "10-31",
                    "mode": "boost",
                    "priority": 200,
                }
            ]
        }
        for asset_id in range(1, 49):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="all-day",
                duration=1800,
                series=f"Base {asset_id}",
            )
        self.add_asset(
            100,
            media_type="complete_block",
            daypart="all-day",
            duration=3600,
            series="Halloween Block",
            collection_type="holiday",
            collection_slug="halloween",
        )

        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 10, 10))
        special_blocks = [
            row for row in builder.entries(result.generation_id)
            if row["entry_type"] == "complete_block"
            and row["asset_id"] == 100
        ]

        self.assertFalse(special_blocks)

        self.config.data["blocks"]["selection_probability"] = 1.0
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 10, 11))
        special_blocks = [
            row for row in builder.entries(result.generation_id)
            if row["entry_type"] == "complete_block"
            and row["asset_id"] == 100
        ]
        self.assertTrue(special_blocks)
        self.assertIn('"rule": "halloween"', special_blocks[0]["explanation_json"])

    def test_equal_priority_overlapping_windows_are_rejected(self):
        self.config.data["seasonal"] = {
            "windows": [
                {
                    "slug": "first",
                    "channel": "fox",
                    "weekdays": ["friday"],
                    "start": "20:00",
                    "end": "23:00",
                    "enabled": True,
                    "priority": 300,
                },
                {
                    "slug": "second",
                    "channel": "fox",
                    "weekdays": ["friday"],
                    "start": "21:00",
                    "end": "22:00",
                    "enabled": True,
                    "priority": 300,
                },
            ]
        }
        with self.assertRaisesRegex(ValueError, "Equal-priority"):
            ScheduleBuilder(self.connection, self.config)

    def test_holiday_commercial_maximum_is_enforced_per_break(self):
        self.config.data["scheduling"]["commercials_per_break_min"] = 3
        self.config.data["scheduling"]["commercials_per_break_max"] = 3
        self.config.data["seasonal"] = {
            "holidays": {
                "halloween": {
                    "start": "10-01",
                    "end": "10-31",
                    "maximum_per_break": 1,
                    "weights": {"commercial": 100.0},
                }
            }
        }
        for asset_id in range(1, 25):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="all-day",
                duration=3500,
                series=f"Show {asset_id}",
            )
        for asset_id in range(30, 35):
            self.add_asset(
                asset_id,
                media_type="commercial",
                daypart="all-day",
                duration=20,
                collection_type="holiday",
                collection_slug="halloween",
            )
        for asset_id in range(40, 45):
            self.add_asset(
                asset_id,
                media_type="commercial",
                daypart="all-day",
                duration=20,
            )
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 10, 10))
        rows = builder.entries(result.generation_id)
        per_break: dict[int, int] = {}
        for row in rows:
            if row["sequence_group"] is None or row["entry_type"] != "commercial":
                continue
            selected = json.loads(row["explanation_json"]).get("selection", {})
            if selected.get("rule") != "halloween":
                continue
            per_break[row["sequence_group"]] = (
                per_break.get(row["sequence_group"], 0) + 1
            )
        self.assertTrue(per_break)
        self.assertLessEqual(max(per_break.values()), 1)

    def test_protected_block_may_overrun_daypart_without_overlap(self):
        self.config.data["blocks"].update(
            {
                "selection_probability": 1.0,
                "repeat_cooldown_days": 0,
                "boundary_policy": "may_overrun",
            }
        )
        asset_id = 1
        for daypart in ("morning", "afternoon", "evening", "night"):
            for episode in range(12):
                self.add_asset(
                    asset_id,
                    media_type="episode",
                    daypart=daypart,
                    duration=1800,
                    series=f"{daypart} {episode}",
                )
                asset_id += 1
        self.add_asset(
            100,
            media_type="complete_block",
            daypart="evening",
            duration=21600,
            series="Protected Block",
        )
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 8, 4))
        rows = builder.entries(result.generation_id)
        block = next(row for row in rows if row["asset_id"] == 100)
        self.assertEqual(block["entry_type"], "complete_block")
        self.assertEqual(block["protected_block"], 1)
        self.assertEqual(
            datetime.fromisoformat(block["starts_at"]).hour,
            17,
        )
        self.assertEqual(datetime.fromisoformat(block["ends_at"]).hour, 23)
        following = rows[rows.index(block) + 1]
        self.assertEqual(following["starts_at"], block["ends_at"])
        self.assertEqual(result.validation_status, "valid")

    def test_must_fit_block_is_skipped_at_short_boundary(self):
        self.config.data["blocks"].update(
            {
                "selection_probability": 1.0,
                "repeat_cooldown_days": 0,
            }
        )
        asset_id = 1
        for daypart in ("morning", "afternoon", "evening", "night"):
            for episode in range(12):
                self.add_asset(
                    asset_id,
                    media_type="episode",
                    daypart=daypart,
                    duration=1800,
                    series=f"{daypart} {episode}",
                )
                asset_id += 1
        self.add_asset(
            100,
            media_type="complete_block",
            daypart="evening",
            duration=21600,
        )
        service = BlockService(self.connection)
        service.sync_profiles()
        service.update_profile(100, boundary_policy="must_fit")
        builder = ScheduleBuilder(self.connection, self.config)
        result = builder.generate_day("fox", date(2026, 8, 4))
        self.assertNotIn(
            100,
            {
                row["asset_id"]
                for row in builder.entries(result.generation_id, programs_only=True)
            },
        )

    def test_recurring_schedule_reuses_prior_day_series_slots(self):
        """The first day is random; the following day follows its lineup."""
        asset_id = 1
        for series in ("Alpha", "Beta"):
            for _ in range(10):
                self.add_asset(
                    asset_id,
                    media_type="episode",
                    daypart="morning",
                    duration=20 * 60,
                    series=series,
                    channel="nickelodeon",
                )
                asset_id += 1
        builder = ScheduleBuilder(self.connection, self.config)
        builder.update_schedule_preferences(
            "nickelodeon", recurring_enabled=True, unique_weekends=False
        )
        first = builder.generate_day("nickelodeon", date(2026, 8, 3))
        second = builder.generate_day("nickelodeon", date(2026, 8, 4))

        def morning_slots(generation_id):
            slots = {}
            for row in builder.entries(generation_id, programs_only=True):
                if row["daypart_slug"] != "morning":
                    continue
                starts_at = datetime.fromisoformat(row["starts_at"])
                slot = (starts_at.hour * 60 + starts_at.minute) // 30 * 30
                slots.setdefault(slot, row["asset_id"])
            return slots

        first_slots = morning_slots(first.generation_id)
        second_slots = morning_slots(second.generation_id)
        self.assertTrue(first_slots)
        self.assertTrue(second_slots)
        first_series = {
            slot: self.connection.execute(
                "SELECT series_title FROM assets WHERE id=?", (asset_id,)
            ).fetchone()["series_title"]
            for slot, asset_id in first_slots.items()
        }
        second_series = {
            slot: self.connection.execute(
                "SELECT series_title FROM assets WHERE id=?", (asset_id,)
            ).fetchone()["series_title"]
            for slot, asset_id in second_slots.items()
        }
        self.assertEqual(first_series, second_series)

    def test_recurring_daytime_series_claim_one_of_its_covered_dayparts(self):
        self.config.data["scheduling"]["dayparts"] = [
            {"slug": "morning", "start": "06:00", "end": "07:00",
             "boundary_policy": "must_fit"},
            {"slug": "afternoon", "start": "07:00", "end": "08:00",
             "boundary_policy": "must_fit"},
            {"slug": "evening", "start": "08:00", "end": "09:00",
             "boundary_policy": "must_fit"},
            {"slug": "night", "start": "09:00", "end": "06:00",
             "boundary_policy": "must_fit"},
        ]
        for asset_id in range(1, 7):
            self.add_asset(
                asset_id, media_type="episode", daypart="daytime",
                duration=20 * 60, series=f"Daytime Show {asset_id}",
                channel="nickelodeon",
            )
            self.connection.execute(
                "DELETE FROM asset_eligibility WHERE asset_id=?", (asset_id,)
            )
            self.connection.executemany(
                """INSERT INTO asset_eligibility(
                       asset_id,channel_slug,daypart_slug,eligibility_source)
                   VALUES (?, 'nickelodeon', ?, 'channel_daypart_folder')""",
                [(asset_id, part) for part in ("morning", "afternoon", "evening")],
            )
        self.connection.commit()
        builder = ScheduleBuilder(self.connection, self.config)
        for daypart in ("morning", "afternoon", "evening"):
            self.assertEqual(
                len(builder._candidates("nickelodeon", daypart, ("episode",))), 6
            )
        builder.update_schedule_preferences(
            "nickelodeon", recurring_enabled=True, unique_weekends=False
        )
        generated = builder.generate_day("nickelodeon", date(2026, 9, 29))
        self.assertEqual(generated.validation_status, "valid")
        programs = [
            row for row in builder.entries(generated.generation_id, programs_only=True)
            if row["daypart_slug"] in {"morning", "afternoon", "evening"}
        ]
        counts = Counter(row["series_title"] for row in programs)
        self.assertEqual(len(counts), 6)
        self.assertTrue(all(count == 1 for count in counts.values()))
        snapshot = self.connection.execute(
            "SELECT state_json FROM recurring_lineup_snapshots WHERE generation_id=?",
            (generated.generation_id,),
        ).fetchone()
        self.assertEqual(json.loads(snapshot["state_json"])["version"], 2)

    def test_broad_scope_weekend_lineups_each_get_one_placement(self):
        self.config.data["scheduling"]["dayparts"] = [
            {"slug": "morning", "start": "06:00", "end": "07:00",
             "boundary_policy": "must_fit"},
            {"slug": "afternoon", "start": "07:00", "end": "08:00",
             "boundary_policy": "must_fit"},
            {"slug": "evening", "start": "08:00", "end": "09:00",
             "boundary_policy": "must_fit"},
            {"slug": "night", "start": "09:00", "end": "06:00",
             "boundary_policy": "must_fit"},
        ]
        for series_index in range(1, 13):
            for episode_index in range(2):
                asset_id = (series_index - 1) * 2 + episode_index + 1
                self.add_asset(
                    asset_id, media_type="episode", daypart="daytime",
                    duration=20 * 60, series=f"Broad Show {series_index}",
                    channel="nickelodeon",
                )
                self.connection.execute(
                    "DELETE FROM asset_eligibility WHERE asset_id=?", (asset_id,)
                )
                self.connection.executemany(
                    """INSERT INTO asset_eligibility(
                           asset_id,channel_slug,daypart_slug,eligibility_source)
                       VALUES (?, 'nickelodeon', ?, 'channel_daypart_folder')""",
                    [(asset_id, part) for part in ("morning", "afternoon", "evening")],
                )
        self.connection.commit()
        builder = ScheduleBuilder(self.connection, self.config)
        builder.update_schedule_preferences(
            "nickelodeon", recurring_enabled=True, unique_weekends=True
        )
        lineups = []
        for target in (date(2026, 9, 28), date(2026, 10, 3), date(2026, 10, 4)):
            generated = builder.generate_day("nickelodeon", target)
            names = [
                row["series_title"]
                for row in builder.entries(generated.generation_id, programs_only=True)
                if row["daypart_slug"] in {"morning", "afternoon", "evening"}
            ]
            self.assertEqual(len(names), 6)
            self.assertEqual(len(set(names)), 6)
            lineups.append(set(names))
        self.assertFalse(lineups[0].intersection(lineups[1]))

    def test_broad_scope_rebuild_discards_old_slots_but_preserves_progress(self):
        self.add_asset(
            1, media_type="episode", daypart="morning", duration=20 * 60,
            series="Moved Show", channel="nickelodeon",
        )
        builder = ScheduleBuilder(self.connection, self.config)
        builder.update_schedule_preferences(
            "nickelodeon", recurring_enabled=True, unique_weekends=False
        )
        old = builder.generate_day("nickelodeon", date(2026, 9, 28))
        old_state = json.loads(self.connection.execute(
            "SELECT state_json FROM recurring_lineup_snapshots WHERE generation_id=?",
            (old.generation_id,),
        ).fetchone()["state_json"])
        self.assertEqual(old_state["version"], 1)
        self.assertTrue(old_state["slots"])
        self.connection.execute(
            "UPDATE assets SET daypart_slug='all-day' WHERE id=1"
        )
        self.connection.execute(
            "UPDATE asset_eligibility SET daypart_slug='all-day' WHERE asset_id=1"
        )
        self.connection.commit()
        rebuilt, _hash = ScheduleBuilder(self.connection, self.config)._recurring_state_before(
            builder.ensure_channel("nickelodeon"), "nickelodeon", date(2026, 9, 29),
            unique_weekends=False,
        )
        self.assertEqual(rebuilt["version"], 2)
        self.assertEqual(rebuilt["slots"], {})
        self.assertEqual(rebuilt["queues"], {})
        self.assertEqual(rebuilt["progress"], old_state["progress"])

    def test_recurring_unique_weekends_use_saturday_not_latest_weekday(self):
        """Saturday's recurring template cannot be replaced by Friday or Sunday."""
        asset_id = 1
        first_ids = {}
        for series in ("Friday Show", "Saturday Show", "Sunday Show"):
            first_ids[series] = asset_id
            for _ in range(100):
                self.add_asset(
                    asset_id,
                    media_type="episode",
                    daypart="morning",
                    duration=20 * 60,
                    series=series,
                    channel="nickelodeon",
                )
                asset_id += 1
        self.connection.execute(
            "UPDATE assets SET enabled_for_scheduling=0 WHERE channel_slug='nickelodeon'"
        )
        builder = ScheduleBuilder(self.connection, self.config)
        builder.update_schedule_preferences(
            "nickelodeon", recurring_enabled=True, unique_weekends=True
        )

        def generate_with(series, target):
            self.connection.execute(
                "UPDATE assets SET enabled_for_scheduling=(series_title=?) WHERE channel_slug='nickelodeon'",
                (series,),
            )
            self.connection.commit()
            return builder.generate_day("nickelodeon", target)

        generate_with("Friday Show", date(2026, 8, 7))  # Friday
        generate_with("Saturday Show", date(2026, 8, 8))  # Saturday template
        generate_with("Sunday Show", date(2026, 8, 9))  # Sunday template
        self.connection.execute(
            "UPDATE assets SET enabled_for_scheduling=1 WHERE channel_slug='nickelodeon'"
        )
        self.connection.commit()
        target = builder.generate_day("nickelodeon", date(2026, 8, 15))
        first_program = next(
            row
            for row in builder.entries(target.generation_id, programs_only=True)
            if row["daypart_slug"] == "morning"
        )
        self.assertEqual(first_program["series_title"], "Saturday Show")

    def test_reroll_replaces_recurring_base_and_refreshes_matching_future_days(self):
        asset_id = 1
        for series in (
            "Alpha", "Beta", "Gamma", "Delta", "Echo", "Foxtrot", "Golf", "Hotel"
        ):
            for _ in range(14):
                self.add_asset(
                    asset_id,
                    media_type="episode",
                    daypart="morning",
                    duration=20 * 60,
                    series=series,
                    channel="nickelodeon",
                )
                asset_id += 1
        builder = ScheduleBuilder(self.connection, self.config)
        builder.update_schedule_preferences(
            "nickelodeon", recurring_enabled=True, unique_weekends=True
        )
        base_date = date(2026, 8, 8)
        sunday_date = date(2026, 8, 9)
        future_saturday = date(2026, 8, 15)
        original_base = builder.generate_day("nickelodeon", base_date)
        original_sunday = builder.generate_day("nickelodeon", sunday_date)
        original_future = builder.generate_day("nickelodeon", future_saturday)

        def morning_series(generation_id):
            return [
                self.connection.execute(
                    "SELECT series_title FROM assets WHERE id=?", (row["asset_id"],)
                ).fetchone()["series_title"]
                for row in builder.entries(generation_id, programs_only=True)
                if row["daypart_slug"] == "morning"
            ]

        original_pattern = morning_series(original_base.generation_id)
        result, refreshed, locked = builder.reroll_day(
            "nickelodeon",
            base_date,
            reroll_token="fresh-saturday-pattern",
        )
        new_pattern = morning_series(result.generation_id)
        active_future = builder.active_generation("nickelodeon", future_saturday)
        active_sunday = builder.active_generation("nickelodeon", sunday_date)

        self.assertNotEqual(original_pattern, new_pattern)
        self.assertEqual(refreshed, [sunday_date, future_saturday])
        self.assertEqual(locked, [])
        self.assertNotEqual(active_future["id"], original_future.generation_id)
        self.assertNotEqual(active_sunday["id"], original_sunday.generation_id)
        self.assertEqual(morning_series(active_future["id"]), new_pattern)

    def test_recurring_schedule_promotes_new_series_into_duplicate_slot(self):
        """A newly imported show cannot be permanently excluded by a full grid."""
        asset_id = 1
        for series in ("Alpha", "Beta"):
            for _ in range(12):
                self.add_asset(
                    asset_id,
                    media_type="episode",
                    daypart="morning",
                    duration=20 * 60,
                    series=series,
                    channel="nickelodeon",
                )
                asset_id += 1
        builder = ScheduleBuilder(self.connection, self.config)
        builder.update_schedule_preferences(
            "nickelodeon", recurring_enabled=True, unique_weekends=False
        )
        builder.generate_day("nickelodeon", date(2026, 8, 3))
        for _ in range(12):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="morning",
                duration=20 * 60,
                series="Gamma",
                channel="nickelodeon",
            )
            asset_id += 1
        second = builder.generate_day("nickelodeon", date(2026, 8, 4))
        series = {
            self.connection.execute(
                "SELECT series_title FROM assets WHERE id=?", (row["asset_id"],)
            ).fetchone()["series_title"]
            for row in builder.entries(second.generation_id, programs_only=True)
            if row["daypart_slug"] == "morning"
        }
        self.assertIn("Gamma", series)

    def test_recurring_overflow_uses_open_unique_weekend_slots_before_waiting(self):
        asset_id = 1
        for series in ("Alpha", "Beta"):
            for _ in range(100):
                self.add_asset(
                    asset_id,
                    media_type="episode",
                    daypart="morning",
                    duration=20 * 60,
                    series=series,
                    channel="nickelodeon",
                )
                asset_id += 1
        builder = ScheduleBuilder(self.connection, self.config)
        builder.update_schedule_preferences(
            "nickelodeon", recurring_enabled=True, unique_weekends=True
        )
        builder.generate_day("nickelodeon", date(2026, 8, 3))
        for _ in range(100):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="morning",
                duration=20 * 60,
                series="Gamma",
                channel="nickelodeon",
            )
            asset_id += 1

        weekday = builder.generate_day("nickelodeon", date(2026, 8, 4))
        weekday_series = {
            row["series_title"]
            for row in builder.entries(weekday.generation_id, programs_only=True)
            if row["daypart_slug"] == "morning"
        }
        self.assertNotIn("Gamma", weekday_series)
        lineup = builder.recurring_lineup("nickelodeon")
        self.assertIn("Gamma", lineup["waiting"]["morning"])

        saturday = builder.generate_day("nickelodeon", date(2026, 8, 8))
        saturday_series = {
            row["series_title"]
            for row in builder.entries(saturday.generation_id, programs_only=True)
            if row["daypart_slug"] == "morning"
        }
        self.assertIn("Gamma", saturday_series)

    def test_recurring_later_overflow_uses_weekend_fallback_slot(self):
        asset_id = 1
        for series in ("Alpha", "Beta"):
            for _ in range(100):
                self.add_asset(
                    asset_id, media_type="episode", daypart="morning",
                    duration=20 * 60, series=series, channel="nickelodeon",
                )
                asset_id += 1
        builder = ScheduleBuilder(self.connection, self.config)
        builder.update_schedule_preferences(
            "nickelodeon", recurring_enabled=True, unique_weekends=True
        )
        builder.generate_day("nickelodeon", date(2026, 8, 3))
        builder.generate_day("nickelodeon", date(2026, 8, 8))
        builder.generate_day("nickelodeon", date(2026, 8, 9))
        for _ in range(100):
            self.add_asset(
                asset_id, media_type="episode", daypart="morning",
                duration=20 * 60, series="Gamma", channel="nickelodeon",
            )
            asset_id += 1

        weekday = builder.generate_day("nickelodeon", date(2026, 8, 10))
        weekday_series = {
            row["series_title"] for row in builder.entries(
                weekday.generation_id, programs_only=True
            ) if row["daypart_slug"] == "morning"
        }
        self.assertNotIn("Gamma", weekday_series)
        saturday = builder.generate_day("nickelodeon", date(2026, 8, 15))
        saturday_series = {
            row["series_title"] for row in builder.entries(
                saturday.generation_id, programs_only=True
            ) if row["daypart_slug"] == "morning"
        }
        self.assertIn("Gamma", saturday_series)

    def test_weekly_special_slots_do_not_absorb_recurring_overflow(self):
        self.config.data["seasonal"] = {"windows": [{
            "slug": "snick", "channel": "nickelodeon", "weekdays": ["saturday"],
            "start": "20:00", "end": "22:00", "collection": "snick",
            "guide_label": "SNICK", "enabled": True,
        }]}
        builder = ScheduleBuilder(self.connection, self.config)
        available = builder._ordinary_recurring_clock_slots(
            "nickelodeon", date(2026, 8, 8)
        )["evening"]
        self.assertIn("1170", available)  # 7:30 PM
        self.assertNotIn("1200", available)  # 8:00 PM starts SNICK
        weekday_slots = {str(minute): "Alpha" for minute in range(1020, 1320, 30)}
        state = {
            "version": 1,
            "slots": {"weekday:evening": weekday_slots},
            "queues": {"evening": [
                {"series": f"Overflow {index}", "enqueued_at": "2026-08-01"}
                for index in range(10)
            ]},
            "progress": {},
        }
        groups = {
            f"Overflow {index}": [Candidate(
                index + 1, f"Overflow {index}", f"Overflow {index}",
                "episode", 1_200_000, f"overflow-{index}.mp4"
            )]
            for index in range(10)
        }
        groups["Alpha"] = [Candidate(100, "Alpha", "Alpha", "episode", 1_200_000, "alpha.mp4")]
        builder._recurring_prepare_slots(
            state, "saturday", "evening", groups, date(2026, 8, 8),
            unique_weekends=True,
            all_eligible_assets={name: {items[0].asset_id} for name, items in groups.items()},
            available_clock_slots=available,
        )
        assigned = state["slots"]["saturday:evening"]
        self.assertEqual(set(assigned), {str(minute) for minute in range(1020, 1200, 30)})
        self.assertEqual(len(state["queues"]["evening"]), 4)

    def test_recurring_reroll_does_not_leave_assigned_series_waiting(self):
        state = {
            "version": 1,
            "slots": {"weekday:morning": {"360": "Gamma"}},
            "queues": {"morning": [
                {"series": "Gamma", "enqueued_at": "2026-08-01"}
            ]},
            "progress": {"Gamma": {"last_asset_id": 1, "seen_asset_ids": [1]}},
        }
        builder = ScheduleBuilder(self.connection, self.config)
        builder._recurring_add_waiting(
            state, "morning", {"Gamma"}, "2026-08-03", {"Gamma": {1, 2}}
        )
        self.assertEqual(state["queues"]["morning"], [])

        state["progress"]["Gamma"]["seen_asset_ids"] = [1, 2]
        state["queues"]["morning"].append(
            {"series": "Gamma", "enqueued_at": "2026-08-03"}
        )
        builder._recurring_add_waiting(
            state, "morning", {"Gamma"}, "2026-08-04", {"Gamma": {1, 2}}
        )
        self.assertEqual(len(state["queues"]["morning"]), 1)

    def test_weekend_first_generation_seeds_weekday_baseline(self):
        self.add_asset(
            1, media_type="episode", daypart="morning", duration=20 * 60,
            series="Alpha", channel="nickelodeon",
        )
        builder = ScheduleBuilder(self.connection, self.config)
        start = datetime(2026, 8, 8, 6, tzinfo=builder.timezone)
        state = builder._empty_recurring_state()
        builder._record_recurring_state(
            state,
            [TimelineItem(1, start, start + timedelta(minutes=20), 0, 1_200_000,
                          "episode", "Alpha", "morning", 1, 0, True)],
            "nickelodeon", date(2026, 8, 8), unique_weekends=True,
            all_eligible_assets={"Alpha": {1}},
        )
        self.assertEqual(state["slots"]["weekday:morning"], {"360": "Alpha"})
        self.assertEqual(state["borrowed_weekend_slots"]["saturday:morning"], ["360"])

    def test_recurring_completed_series_moves_behind_waiting_queue_and_reruns(self):
        for asset_id, series in ((1, "Alpha"), (2, "Beta")):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="morning",
                duration=20 * 60,
                series=series,
                channel="nickelodeon",
            )
        builder = ScheduleBuilder(self.connection, self.config)
        start = datetime.combine(
            date(2026, 8, 3), datetime.min.time(), builder.timezone
        ) + timedelta(hours=6)
        state = {
            "version": 1,
            "slots": {"weekday:morning": {"360": "Alpha"}},
            "queues": {"morning": [{"series": "Beta", "enqueued_at": "2026-08-01"}]},
            "progress": {"Alpha": {"last_asset_id": None, "seen_asset_ids": []}},
        }
        builder._record_recurring_state(
            state,
            [TimelineItem(1, start, start + timedelta(minutes=20), 0, 1_200_000,
                          "episode", "Alpha", "morning", 1, 0, True)],
            "nickelodeon",
            date(2026, 8, 3),
            unique_weekends=True,
            all_eligible_assets={"Alpha": {1}, "Beta": {2}},
        )
        groups = {
            "Alpha": [Candidate(1, "Alpha", "Alpha", "episode", 1_200_000, "a.mp4")],
            "Beta": [Candidate(2, "Beta", "Beta", "episode", 1_200_000, "b.mp4")],
        }
        builder._recurring_prepare_slots(
            state,
            "weekday",
            "morning",
            groups,
            date(2026, 8, 4),
            unique_weekends=False,
            all_eligible_assets={"Alpha": {1}, "Beta": {2}},
        )
        assignments = state["slots"]["weekday:morning"]
        self.assertEqual(assignments["360"], "Alpha")
        selected = builder._recurring_series_for_slot(
            state,
            "weekday",
            "morning",
            start,
            date(2026, 8, 4),
            groups,
            all_eligible_assets={"Alpha": {1}, "Beta": {2}},
        )
        self.assertEqual(selected, "Beta")
        self.assertEqual(assignments["360"], "Beta")
        state["progress"]["Beta"] = {"last_asset_id": 2, "seen_asset_ids": [2]}
        state["queues"]["morning"].append({"series": "Beta", "enqueued_at": "2026-08-04"})
        selected = builder._recurring_series_for_slot(
            state,
            "weekday",
            "morning",
            start,
            date(2026, 8, 5),
            groups,
            all_eligible_assets={"Alpha": {1}, "Beta": {2}},
        )
        self.assertEqual(selected, "Alpha")
        self.assertEqual(state["progress"]["Alpha"]["seen_asset_ids"], [])

    def test_returning_series_plays_new_episode_before_rerun(self):
        progress = {"Alpha": {"last_asset_id": 1, "seen_asset_ids": [1]}}
        groups = {
            "Alpha": [
                Candidate(1, "Alpha 1", "Alpha", "episode", 1000, "S01E01.mp4"),
                Candidate(2, "Alpha 2", "Alpha", "episode", 1000, "S01E02.mp4"),
            ]
        }
        queue = [{"series": "Alpha", "enqueued_at": "2026-08-01"}]
        selected = ScheduleBuilder._recurring_pop_waiting(
            queue, groups, progress, {"Alpha": {1, 2}}
        )
        self.assertEqual(selected, "Alpha")
        self.assertEqual(progress["Alpha"]["seen_asset_ids"], [1])
        queue.append({"series": "Alpha", "enqueued_at": "2026-08-02"})
        selected = ScheduleBuilder._recurring_pop_waiting(
            queue, groups, progress, {"Alpha": {1}}
        )
        self.assertEqual(selected, "Alpha")
        self.assertEqual(progress["Alpha"]["seen_asset_ids"], [])

    def test_recurring_schedule_keeps_new_series_unplaced_when_grid_is_unique(self):
        """A new show does not silently displace a one-slot-per-series lineup."""
        for asset_id in range(1, 13):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="morning",
                duration=20 * 60,
                series=f"Core {asset_id}",
                channel="nickelodeon",
            )
        builder = ScheduleBuilder(self.connection, self.config)
        builder.update_schedule_preferences(
            "nickelodeon", recurring_enabled=True, unique_weekends=False
        )
        builder.generate_day("nickelodeon", date(2026, 8, 3))
        for asset_id in range(13, 25):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="morning",
                duration=20 * 60,
                series="Gamma",
                channel="nickelodeon",
            )
        second = builder.generate_day("nickelodeon", date(2026, 8, 4))
        series = {
            self.connection.execute(
                "SELECT series_title FROM assets WHERE id=?", (row["asset_id"],)
            ).fetchone()["series_title"]
            for row in builder.entries(second.generation_id, programs_only=True)
            if row["daypart_slug"] == "morning"
        }
        self.assertNotIn("Gamma", series)

    def test_recurring_template_excludes_complete_block_overrides(self):
        """A chance-selected block must not become tomorrow's core lineup."""
        self.config.data["blocks"]["selection_probability"] = 1.0
        self.add_asset(
            1,
            media_type="complete_block",
            daypart="morning",
            duration=60 * 60,
            series="Saturday Morning Block",
            channel="nickelodeon",
        )
        builder = ScheduleBuilder(self.connection, self.config)
        builder.update_schedule_preferences(
            "nickelodeon", recurring_enabled=True, unique_weekends=False
        )
        first = builder.generate_day("nickelodeon", date(2026, 8, 3))
        self.assertIn(
            1,
            {
                row["asset_id"]
                for row in builder.entries(first.generation_id, programs_only=True)
            },
        )
        channel_id = builder.ensure_channel("nickelodeon")
        grid = builder._recurring_grid_for_date(
            channel_id, date(2026, 8, 4), unique_weekends=False
        )
        self.assertEqual(grid, {})

    def test_content_only_channel_runs_programs_back_to_back_without_continuity(self):
        for asset_id, series in ((1, "Flying Toasters"), (2, "Mystify")):
            self.add_asset(
                asset_id,
                media_type="episode",
                daypart="all-day",
                duration=12 * 60,
                series=series,
                channel="screensavers",
            )
        builder = ScheduleBuilder(self.connection, self.config)
        builder.update_schedule_preferences(
            "screensavers",
            recurring_enabled=False,
            unique_weekends=False,
            presentation_format="content_only",
        )
        result = builder.generate_day("screensavers", date(2026, 8, 3))
        entries = builder.entries(result.generation_id)
        self.assertTrue(entries)
        self.assertTrue(all(row["is_program"] for row in entries))
        self.assertNotIn("continuity", {row["entry_type"] for row in entries})
        for earlier, later in zip(entries, entries[1:]):
            self.assertEqual(earlier["ends_at"], later["starts_at"])
        first_start = datetime.fromisoformat(entries[0]["starts_at"])
        second_start = datetime.fromisoformat(entries[1]["starts_at"])
        self.assertEqual((second_start - first_start).total_seconds(), 12 * 60)


if __name__ == "__main__":
    unittest.main()
