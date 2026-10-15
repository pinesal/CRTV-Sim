import shutil
import unittest
import uuid
import hashlib
import json
import wave
import math
import struct
import subprocess
from unittest.mock import Mock
from datetime import datetime
from pathlib import Path

from retrotv.announcements import UpNextRenderer, UpNextService
from retrotv.ai_announcements import AnnouncementProfileService
from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database, ensure_storage_root, utc_now
from retrotv.graphics import GraphicsService


class FakeSpeechProvider:
    name = "test-tts"

    def __init__(self, error=None):
        self.text = None
        self.error = error

    def synthesize(self, text, output, *, voice, rate, instructions=""):
        self.text = text
        if self.error:
            raise RuntimeError(self.error)
        output.write_bytes(b"speech")


class ToneGenericProvider:
    def __init__(self, frequency=440):
        self.frequency = frequency

    def synthesize(self, text, output, **kwargs):
        with wave.open(str(output), "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
            wav.writeframes(b"".join(struct.pack("<h", int(10000 * math.sin(2 * math.pi * self.frequency * n / 16000)))
                                     for n in range(16000)))


class UpNextTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(__file__).resolve().parents[1]
        self.root = self.project / ".test-tmp" / f"announcements-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.config = AppConfig(
            self.root / "test.toml",
            self.project,
            {
                "application": {
                    "database_path": str(self.root / "retrotv.sqlite3"),
                    "timezone": "America/Los_Angeles",
                },
                "storage_roots": {"media": str(self.root / "media")},
                "tools": {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"},
                "scheduling": {
                    "timezone": "America/Los_Angeles",
                    "dayparts": [
                        {"slug": "morning", "start": "06:00", "end": "12:00"},
                        {"slug": "afternoon", "start": "12:00", "end": "16:00"},
                        {"slug": "evening", "start": "16:00", "end": "20:00"},
                        {"slug": "night", "start": "20:00", "end": "06:00"},
                    ],
                },
                "guide": {"slot_minutes": 30},
                "announcements": {
                    "output_dir": str(self.root / "generated"),
                    "fonts_dir": "assets/fonts",
                    "listing_count": 3,
                    "minimum_duration_seconds": 4,
                    "tts_provider": "none",
                    "channels": {
                        "nickelodeon": {
                            "background_color": "#F58220",
                            "tagline": "The only network for kids!",
                            "spoken_tagline": "The only network for kids, Nickelodeon!",
                        }
                    },
                },
                "daypart_branding": {
                    "nickelodeon": {
                        "night": {
                            "display_name": "Nick at Nite",
                            "background_color": "0x20345B",
                            "tagline": "Hello out there from TV Land!",
                            "spoken_tagline": "You're watching Nick at Nite!",
                        }
                    }
                },
                "seasonal": {
                    "windows": {
                        "snick": {
                            "channel": "nickelodeon",
                            "weekdays": ["monday"],
                            "start": "20:00",
                            "end": "22:00",
                            "collection": "snick",
                            "guide_label": "SNICK",
                            "priority": 300,
                            "enabled": True,
                        }
                    }
                },
            },
        )
        self.connection = connect_database(self.config.database_path)
        apply_migrations(self.connection, self.project / "migrations")
        storage = ensure_storage_root(self.connection, "media")
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO channels(id, slug, display_name, channel_number, enabled)
            VALUES (1, 'nickelodeon', 'Nickelodeon', 33, 1)
            """
        )
        self.connection.execute(
            """
            INSERT INTO schedule_generations(
                id, channel_id, schedule_date, seed, configuration_fingerprint,
                status, validation_status, generated_at, activated_at
            ) VALUES (1, 1, '2026-08-03', 'seed', 'fingerprint',
                      'active', 'valid', ?, ?)
            """,
            (now, now),
        )
        shows = (
            (1, "Doug", "2026-08-03T14:30:00-07:00", "2026-08-03T15:00:00-07:00"),
            (2, "Ren and Stimpy", "2026-08-03T15:00:00-07:00", "2026-08-03T15:30:00-07:00"),
            (3, "Rugrats", "2026-08-03T15:30:00-07:00", "2026-08-03T16:00:00-07:00"),
        )
        for position, (asset_id, series, starts_at, ends_at) in enumerate(shows):
            self.connection.execute(
                """
                INSERT INTO assets(
                    id, source_root_id, source_relative_path, original_filename,
                    file_size, modified_ns, media_type, display_title, series_title,
                    channel_slug, daypart_slug, metadata_status, integrity_status,
                    review_status, enabled_for_scheduling, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, 1, 1, 'episode', ?, ?, 'nickelodeon',
                          'afternoon', 'ready', 'ready', 'ready', 1, ?, ?)
                """,
                (
                    asset_id,
                    storage,
                    f"nickelodeon/afternoon/episodes/{series}/{asset_id}.mp4",
                    f"{asset_id}.mp4",
                    series,
                    series,
                    now,
                    now,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO schedule_entries(
                    generation_id, channel_id, asset_id, starts_at, ends_at,
                    media_start_ms, media_end_ms, entry_type, title, daypart_slug,
                    sequence_group, sequence_position, is_program
                ) VALUES (1, 1, ?, ?, ?, 0, 1800000, 'episode', ?,
                          'afternoon', ?, ?, 1)
                """,
                (asset_id, starts_at, ends_at, series, position + 1, position),
            )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_plan_uses_next_three_half_hour_schedule_facts(self):
        plan = UpNextService(self.connection, self.config).build_plan(
            "nickelodeon", datetime.fromisoformat("2026-08-03T14:10:00-07:00")
        )
        self.assertEqual(plan.channel_name, "Nickelodeon")
        self.assertEqual(plan.background_color, "0xF58220")
        self.assertEqual(plan.tagline, "The only network for kids!")
        self.assertEqual([line.title for line in plan.lines], [
            "Doug",
            "Ren and Stimpy",
            "Rugrats",
        ])
        self.assertEqual(
            [UpNextService._display_time(line.starts_at) for line in plan.lines],
            ["2:30 PM", "3:00 PM", "3:30 PM"],
        )
        self.assertEqual(
            plan.spoken_text,
            "Up next on Nickelodeon... Doug. Followed by Ren and Stimpy. "
            "Then, later today, Rugrats. "
            "The only network for kids, Nickelodeon!",
        )

    def test_ass_screen_contains_schedule_and_tagline(self):
        renderer = UpNextRenderer(self.connection, self.config)
        plan = renderer.service.build_plan(
            "nickelodeon", datetime.fromisoformat("2026-08-03T14:10:00-07:00")
        )
        ass = renderer._ass(plan, 12)
        self.assertIn("COMING UP NEXT ON NICKELODEON...", ass)
        self.assertIn("Doug  -  2:30 PM", ass)
        self.assertIn("Ren and Stimpy  -  3:00 PM", ass)
        self.assertIn("The only network for kids!", ass)
        self.assertIn(r"\pos(320,112)", ass)
        self.assertIn(r"\pos(320,188)", ass)
        self.assertIn(r"\pos(320,390)", ass)
        self.assertIn("5,64,64,20,1", ass)

    def test_program_start_is_presented_at_nearest_guide_half_hour(self):
        service = UpNextService(self.connection, self.config)
        self.assertEqual(
            service._nearest_guide_slot(
                datetime.fromisoformat("2026-08-03T18:01:18-07:00")
            ),
            datetime.fromisoformat("2026-08-03T18:00:00-07:00"),
        )
        self.assertEqual(
            service._nearest_guide_slot(
                datetime.fromisoformat("2026-08-03T18:29:40-07:00")
            ),
            datetime.fromisoformat("2026-08-03T18:30:00-07:00"),
        )

    def test_up_next_uses_promoted_program_daypart_identity_at_boundary(self):
        self.connection.execute(
            """
            INSERT INTO schedule_entries(
                generation_id,channel_id,starts_at,ends_at,entry_type,title,
                daypart_slug,sequence_position,is_program
            ) VALUES (
                1,1,'2026-08-03T20:00:00-07:00','2026-08-03T20:30:00-07:00',
                'episode','Mister Ed','night',50,1
            )
            """
        )
        self.connection.commit()
        plan = UpNextService(self.connection, self.config).build_plan(
            "nickelodeon", datetime.fromisoformat("2026-08-03T19:40:00-07:00")
        )
        self.assertEqual(plan.daypart_slug, "night")
        self.assertEqual(plan.channel_name, "Nick at Nite")
        self.assertEqual(plan.background_color, "0x20345B")
        self.assertEqual(plan.lines[0].title, "Mister Ed")
        self.assertEqual(plan.weekly_collection_slug, "snick")

    def test_persisted_up_next_uses_the_upcoming_weekly_special_scope(self):
        self.connection.execute(
            """
            INSERT INTO schedule_entries(
                generation_id,channel_id,starts_at,ends_at,entry_type,title,daypart_slug,
                sequence_position,is_program,programming_block_id
            ) VALUES (1,1,'2026-08-03T20:00:00-07:00','2026-08-03T20:30:00-07:00',
                      'episode','Roundhouse','night',50,1,50)
            """
        )
        entry_id = self.connection.execute(
            """
            INSERT INTO schedule_entries(
                generation_id,channel_id,starts_at,ends_at,entry_type,title,daypart_slug,
                sequence_position,is_program,programming_block_id
            ) VALUES (1,1,'2026-08-03T19:59:30-07:00','2026-08-03T20:00:00-07:00',
                      'announcement_opportunity','Up Next','evening',49,0,50)
            """
        ).lastrowid
        now = utc_now()
        facts = {"channel": "nickelodeon", "station_identification": False, "listings": []}
        self.connection.execute(
            """
            INSERT INTO announcement_opportunities(
                schedule_entry_id,generation_id,channel_id,programming_block_id,
                starts_at,ends_at,facts_json,cache_key,render_status,created_at,updated_at
            ) VALUES (?,1,1,50,'2026-08-03T19:59:30-07:00','2026-08-03T20:00:00-07:00',
                      ?,?,'pending',?,?)
            """,
            (entry_id, json.dumps(facts), hashlib.sha256(b"snick").hexdigest(), now, now),
        )
        self.connection.commit()
        opportunity_id = self.connection.execute(
            "SELECT id FROM announcement_opportunities WHERE schedule_entry_id=?", (entry_id,)
        ).fetchone()["id"]

        plan = UpNextService(self.connection, self.config).build_opportunity_plan(opportunity_id)
        self.assertEqual(plan.daypart_slug, "night")
        self.assertEqual(plan.weekly_collection_slug, "snick")

    def test_weekly_special_promo_plan_refreshes_and_displays_known_lineup(self):
        self.connection.execute(
            "UPDATE schedule_entries SET programming_owner='snick' WHERE generation_id=1 AND is_program=1"
        )
        entry_id = self.connection.execute(
            """INSERT INTO schedule_entries(
                generation_id,channel_id,starts_at,ends_at,entry_type,title,daypart_slug,
                sequence_position,is_program,programming_block_id
            ) VALUES (1,1,'2026-08-03T14:29:30-07:00','2026-08-03T14:30:00-07:00',
                      'weekly_special_promo_opportunity','Weekly Special Promo: SNICK',
                      'afternoon',48,0,50)"""
        ).lastrowid
        details = {
            "window_slug": "snick", "collection_slug": "snick",
            "special_name": "SNICK", "airing_at": "2026-08-03T14:30:00-07:00",
            "airing_ends_at": "2026-08-03T16:00:00-07:00",
        }
        now = utc_now()
        self.connection.execute(
            """INSERT INTO announcement_opportunities(
                schedule_entry_id,generation_id,channel_id,starts_at,ends_at,
                facts_json,cache_key,weekly_collection_slug,opportunity_type,
                render_status,created_at,updated_at
            ) VALUES (?,1,1,'2026-08-03T14:29:30-07:00','2026-08-03T14:30:00-07:00',
                      ?,?,'snick','weekly_special_promo','pending',?,?)""",
            (entry_id, json.dumps({"channel": "nickelodeon",
                                   "weekly_special_promo": details,
                                   "lineup": []}), hashlib.sha256(b"promo").hexdigest(), now, now),
        )
        self.connection.commit()
        opportunity_id = self.connection.execute(
            "SELECT id FROM announcement_opportunities WHERE schedule_entry_id=?", (entry_id,)
        ).fetchone()["id"]
        plan = UpNextService(self.connection, self.config).build_opportunity_plan(opportunity_id)
        self.assertTrue(plan.weekly_special_promo)
        self.assertEqual(plan.special_name, "SNICK")
        self.assertEqual(plan.weekly_collection_slug, "snick")
        self.assertEqual(len(plan.lines), 2)
        self.assertTrue(plan.promo_lineup_more)
        self.assertTrue({line.title for line in plan.lines}.issubset(
            {"Doug", "Ren and Stimpy", "Rugrats"}
        ))
        ass = UpNextRenderer(self.connection, self.config)._ass(plan, 30)
        self.assertIn("TONIGHT, JOIN US FOR SNICK!", ass)
        self.assertIn("SNICK", ass)
        self.assertIn("Monday, August 3", ass)
        self.assertIn("2:30 PM", ass)
        self.assertIn("…AND MORE", ass)

    def test_weekly_special_promo_falls_back_to_name_and_time_without_lineup(self):
        plan = UpNextService(self.connection, self.config)._weekly_special_promo_plan(
            {"channel_slug": "nickelodeon", "channel_name": "Nickelodeon",
             "starts_at": "2026-08-03T19:00:00-07:00"},
            {"weekly_special_promo": {
                "collection_slug": "snick", "special_name": "SNICK",
                "airing_at": "2026-08-03T20:00:00-07:00",
            }, "selected_lineup": [], "lineup_more": False},
        )
        self.assertEqual(plan.lines, ())
        self.assertIn("SNICK", plan.tagline)
        self.assertIn("8:00 PM", plan.tagline)
        ass = UpNextRenderer(self.connection, self.config)._ass(plan, 30)
        self.assertIn("Monday, August 3", ass)
        self.assertNotIn("TBD", ass)

    def test_weekly_promo_narration_uses_improvised_dialogue_and_keeps_silent_fallback(self):
        self.connection.execute(
            "UPDATE schedule_entries SET programming_owner='snick' WHERE generation_id=1 AND is_program=1"
        )
        entry_id = self.connection.execute(
            """INSERT INTO schedule_entries(
                generation_id,channel_id,starts_at,ends_at,entry_type,title,daypart_slug,
                sequence_position,is_program,programming_block_id
            ) VALUES (1,1,'2026-08-03T14:29:30-07:00','2026-08-03T14:30:00-07:00',
                      'weekly_special_promo_opportunity','Weekly Special Promo: SNICK',
                      'afternoon',48,0,50)"""
        ).lastrowid
        details = {"window_slug": "snick", "collection_slug": "snick",
                   "special_name": "SNICK", "airing_at": "2026-08-03T14:30:00-07:00",
                   "airing_ends_at": "2026-08-03T16:00:00-07:00"}
        now = utc_now()
        self.connection.execute(
            """INSERT INTO announcement_opportunities(
                schedule_entry_id,generation_id,channel_id,starts_at,ends_at,
                facts_json,cache_key,weekly_collection_slug,opportunity_type,
                render_status,created_at,updated_at
            ) VALUES (?,1,1,'2026-08-03T14:29:30-07:00','2026-08-03T14:30:00-07:00',
                      ?,?,'snick','weekly_special_promo','pending',?,?)""",
            (entry_id, json.dumps({"channel": "nickelodeon",
                                   "weekly_special_promo": details, "lineup": []}),
             hashlib.sha256(b"promo-narration").hexdigest(), now, now),
        )
        self.connection.commit()
        opportunity_id = self.connection.execute(
            "SELECT id FROM announcement_opportunities WHERE schedule_entry_id=?", (entry_id,)
        ).fetchone()["id"]
        AnnouncementProfileService(self.connection, self.config).update(
            "nickelodeon", {"override_enabled": True, "enabled": True,
                            "identity_name": "SNICK"}, weekly_collection_slug="snick"
        )

        class Client:
            def request_json(self, path, payload):
                self.payload = payload
                return {"output_text": "Tune in Saturday for SNICK!"}

        renderer = UpNextRenderer(
            self.connection, self.config, tts_provider=FakeSpeechProvider(),
            ai_client=Client(),
        )
        renderer._encode = lambda work, ass, audio, output, color, duration, **kwargs: output.write_bytes(b"video")
        renderer._validate = lambda path, expected_duration=None: float(expected_duration or 30)
        renderer._audio_duration = lambda path: 1.0
        silent = renderer.render_opportunity(opportunity_id, use_tts=False)
        fallback = self.connection.execute(
            "SELECT fallback_path FROM announcement_opportunities WHERE id=?",
            (opportunity_id,),
        ).fetchone()["fallback_path"]
        self.assertEqual(Path(fallback), silent.output)
        narrated = renderer.render_opportunity(opportunity_id, use_tts=True, activate=False)
        self.assertEqual(renderer._tts_provider.text, "Tune in Saturday for SNICK!")
        self.assertTrue(narrated.output.is_file())

        renderer._promo_script_provider = type("BrokenScript", (), {
            "generate": lambda *_args: (_ for _ in ()).throw(RuntimeError("LLM offline"))
        })()
        with self.assertRaisesRegex(RuntimeError, "LLM offline"):
            renderer.render_opportunity(opportunity_id, use_tts=True, force=True)
        self.assertTrue(Path(fallback).is_file())
        renderer._promo_script_provider = None
        renderer._tts_provider = FakeSpeechProvider(error="TTS offline")
        with self.assertRaisesRegex(RuntimeError, "TTS offline"):
            renderer.render_opportunity(opportunity_id, use_tts=True, force=True)
        self.assertTrue(Path(fallback).is_file())
        AnnouncementProfileService(self.connection, self.config).update(
            "nickelodeon", {"promo_narration_enabled": False},
            weekly_collection_slug="snick"
        )
        with self.assertRaisesRegex(RuntimeError, "AI promo narration is disabled"):
            renderer.render_opportunity(opportunity_id, use_tts=True, force=True)
        self.assertTrue(Path(fallback).is_file())

    def test_plan_carries_active_weekly_collection_for_graphics(self):
        plan = UpNextService(self.connection, self.config).build_plan(
            "nickelodeon", datetime.fromisoformat("2026-08-03T20:10:00-07:00")
        )
        self.assertEqual(plan.weekly_collection_slug, "snick")

    def test_enabled_weekly_profile_overrides_identity_and_taglines(self):
        AnnouncementProfileService(self.connection, self.config).update(
            "nickelodeon",
            {
                "override_enabled": True,
                "identity_name": "SNICK",
                "tagline": "Saturday night is {channel}!",
                "spoken_tagline": "You're watching SNICK!",
            },
            weekly_collection_slug="snick",
        )
        plan = UpNextService(self.connection, self.config).build_plan(
            "nickelodeon", datetime.fromisoformat("2026-08-03T20:10:00-07:00")
        )
        self.assertEqual(plan.channel_name, "SNICK")
        self.assertEqual(plan.tagline, "Saturday night is SNICK!")
        self.assertTrue(plan.spoken_text.endswith("You're watching SNICK!"))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_silent_render_is_valid_and_cached(self):
        renderer = UpNextRenderer(self.connection, self.config)
        when = datetime.fromisoformat("2026-08-03T14:10:00-07:00")
        first = renderer.render("nickelodeon", when, use_tts=False)
        second = renderer.render("nickelodeon", when, use_tts=False)
        self.assertTrue(first.output.is_file())
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(first.output, second.output)
        self.assertEqual(first.tts_provider, "none")

    def test_persisted_opportunity_renders_exact_thirty_second_fallback(self):
        now = utc_now()
        cursor = self.connection.execute(
            """
            INSERT INTO schedule_entries(
                generation_id, channel_id, starts_at, ends_at, entry_type,
                title, daypart_slug, sequence_position, is_program
            ) VALUES (1,1,'2026-08-03T14:00:00-07:00',
                      '2026-08-03T14:00:30-07:00','announcement_opportunity',
                      'Up Next','afternoon',99,0)
            """
        )
        entry_id = int(cursor.lastrowid)
        facts = {"channel": "nickelodeon", "station_identification": False, "listings": []}
        self.connection.execute(
            """
            INSERT INTO announcement_opportunities(
                schedule_entry_id,generation_id,channel_id,starts_at,ends_at,
                facts_json,cache_key,created_at,updated_at
            ) VALUES (?,1,1,'2026-08-03T14:00:00-07:00',
                      '2026-08-03T14:00:30-07:00',?,?,?,?)
            """,
            (entry_id, json.dumps(facts), hashlib.sha256(b"test").hexdigest(), now, now),
        )
        self.connection.commit()
        opportunity_id = self.connection.execute(
            "SELECT id FROM announcement_opportunities WHERE schedule_entry_id=?",
            (entry_id,),
        ).fetchone()["id"]
        result = UpNextRenderer(self.connection, self.config).render_opportunity(
            opportunity_id, use_tts=False
        )
        self.assertAlmostEqual(result.duration_seconds, 30, delta=0.25)
        saved = self.connection.execute(
            "SELECT fallback_path FROM announcement_opportunities WHERE id=?",
            (opportunity_id,),
        ).fetchone()
        self.assertEqual(Path(saved["fallback_path"]), result.output)

    def _fallback_fixture(self):
        now = utc_now()
        cursor = self.connection.execute(
            "INSERT INTO schedule_entries(generation_id,channel_id,starts_at,ends_at,entry_type,title,daypart_slug,sequence_position,is_program) "
            "VALUES (1,1,'2026-08-03T14:00:00-07:00','2026-08-03T14:00:30-07:00','announcement_opportunity','Up Next','afternoon',99,0)"
        )
        facts = json.dumps({"channel": "nickelodeon", "station_identification": False, "listings": []})
        cursor = self.connection.execute(
            "INSERT INTO announcement_opportunities(schedule_entry_id,generation_id,channel_id,starts_at,ends_at,facts_json,cache_key,created_at,updated_at) "
            "VALUES (?,1,1,'2026-08-03T14:00:00-07:00','2026-08-03T14:00:30-07:00',?,?,?,?)",
            (cursor.lastrowid, facts, hashlib.sha256(facts.encode()).hexdigest(), now, now),
        )
        self.connection.commit()
        renderer = UpNextRenderer(self.connection, self.config, tts_provider=FakeSpeechProvider())
        renderer._encode = lambda work, ass, audio, output, color, duration, **kwargs: output.write_bytes(b"video")
        renderer._validate = lambda path, expected_duration=None: float(expected_duration or 30)
        renderer._audio_duration = lambda path: 1
        return renderer, cursor.lastrowid

    def test_cached_silent_screen_upgrades_and_replacement_changes_cache(self):
        renderer, opportunity = self._fallback_fixture()
        first = renderer.render_opportunity(opportunity, use_tts=False)
        self.assertEqual(first.audio_mode, "silent")
        self.assertTrue(renderer.fallback_is_current(opportunity))
        profiles = AnnouncementProfileService(self.connection, self.config)
        profiles.generate_generic("nickelodeon", ToneGenericProvider())
        self.assertFalse(renderer.fallback_is_current(opportunity))
        spoken = renderer.render_opportunity(opportunity, use_tts=False)
        self.assertEqual(spoken.audio_mode, "generic")
        self.assertNotEqual(first.output, spoken.output)
        self.assertTrue(renderer.fallback_is_current(opportunity))
        profiles.update("nickelodeon", {"generic_script": "New recording"})
        # Profile changes alone keep the currently usable recording and video.
        self.assertTrue(renderer.fallback_is_current(opportunity))
        profiles.generate_generic("nickelodeon", ToneGenericProvider(880))
        self.assertFalse(renderer.fallback_is_current(opportunity))
        replacement = renderer.render_opportunity(opportunity, use_tts=False)
        self.assertNotEqual(spoken.output, replacement.output)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM schedule_generations").fetchone()[0], 1)

    def test_ai_failures_leave_generic_ready_without_another_cloud_request(self):
        renderer, opportunity = self._fallback_fixture()
        profiles = AnnouncementProfileService(self.connection, self.config)
        profiles.generate_generic("nickelodeon", ToneGenericProvider())
        profiles.update("nickelodeon", {"enabled": True})
        renderer._script_provider = Mock()
        for error in ("LLM unavailable", "Timeout", "Missing credentials"):
            renderer._script_provider.generate.side_effect = RuntimeError(error)
            with self.assertRaisesRegex(RuntimeError, error):
                renderer.render_opportunity(opportunity, use_tts=True)
            row = self.connection.execute("SELECT fallback_mode,fallback_path,fallback_reason FROM announcement_opportunities WHERE id=?", (opportunity,)).fetchone()
            self.assertEqual(row["fallback_mode"], "generic")
            self.assertTrue(Path(row["fallback_path"]).is_file())
            self.assertIn(error, row["fallback_reason"])
        renderer._script_provider.generate.side_effect = None
        renderer._script_provider.generate.return_value = "Factual dialogue"
        renderer._tts_provider = FakeSpeechProvider(error="TTS failed")
        with self.assertRaisesRegex(RuntimeError, "TTS failed"):
            renderer.render_opportunity(opportunity, use_tts=True)
        renderer._tts_provider = FakeSpeechProvider()
        renderer._audio_duration = lambda path: 31 if path.name == "speech.wav" else 1
        with self.assertRaisesRegex(RuntimeError, "does not fit"):
            renderer.render_opportunity(opportunity, use_tts=True)
        self.assertTrue(renderer.fallback_is_current(opportunity))
        renderer._audio_duration = lambda path: 1
        result = renderer.render_opportunity(opportunity, use_tts=True)
        self.assertEqual(result.audio_mode, "ai")
        self.assertEqual(renderer._tts_provider.text, "Factual dialogue")

    def test_generic_incorporation_failure_is_visible_and_silence_is_last(self):
        renderer, opportunity = self._fallback_fixture()
        profiles = AnnouncementProfileService(self.connection, self.config)
        profiles.generate_generic("nickelodeon", ToneGenericProvider())
        calls = []
        def encode(work, ass, audio, output, color, duration, **kwargs):
            calls.append(audio)
            if audio:
                raise RuntimeError("audio encoding failed")
            output.write_bytes(b"silent video")
        renderer._encode = encode
        result = renderer.render_opportunity(opportunity, use_tts=False)
        self.assertEqual(result.audio_mode, "silent")
        self.assertIsNotNone(calls[0])
        self.assertIsNone(calls[1])
        self.assertIn("audio encoding failed", result.fallback_reason)
        self.assertTrue(renderer.fallback_is_current(opportunity))

    def test_fallback_preserves_recorded_opportunity_duration(self):
        renderer, opportunity = self._fallback_fixture()
        AnnouncementProfileService(self.connection, self.config).generate_generic("nickelodeon", ToneGenericProvider())
        self.connection.execute("UPDATE announcement_opportunities SET ends_at='2026-08-03T14:00:05-07:00' WHERE id=?", (opportunity,))
        self.connection.commit()
        result = renderer.render_opportunity(opportunity, use_tts=False)
        self.assertEqual(result.duration_seconds, 5)
        self.assertEqual(result.audio_mode, "generic")

    def test_damaged_generic_video_cache_is_repaired_before_silence(self):
        renderer, opportunity = self._fallback_fixture()
        AnnouncementProfileService(self.connection, self.config).generate_generic("nickelodeon", ToneGenericProvider())
        first = renderer.render_opportunity(opportunity, use_tts=False)
        first.output.write_bytes(b"broken")
        def validate(path, expected_duration=None):
            if path.read_bytes() == b"broken":
                raise RuntimeError("Broken cached video")
            return float(expected_duration or 30)
        renderer._validate = validate
        self.assertFalse(renderer.fallback_is_current(opportunity))
        repaired = renderer.render_opportunity(opportunity, use_tts=False)
        self.assertEqual(repaired.audio_mode, "generic")
        self.assertFalse(repaired.cached)
        self.assertTrue(renderer.fallback_is_current(opportunity))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_generic_audio_survives_real_video_music_mix(self):
        profiles = AnnouncementProfileService(self.connection, self.config)
        profiles.generate_generic("nickelodeon", ToneGenericProvider())
        music = self.root / "media/nickelodeon/graphics/up-next/music.wav"
        music.parent.mkdir(parents=True)
        with wave.open(str(music), "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
            wav.writeframes(b"".join(struct.pack("<h", int(5000 * math.sin(2 * math.pi * 120 * n / 16000)))
                                     for n in range(16000 * 4)))
        result = UpNextRenderer(self.connection, self.config).render(
            "nickelodeon", datetime.fromisoformat("2026-08-03T14:10:00-07:00"), use_tts=False, exact_duration=4
        )
        self.assertEqual(result.audio_mode, "generic")
        def rms(start):
            decoded = subprocess.run([self.config.ffmpeg, "-v", "error", "-ss", str(start), "-i", str(result.output),
                                      "-t", "0.6", "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                                     capture_output=True, check=True, timeout=30).stdout
            values = struct.unpack("<" + "h" * (len(decoded) // 2), decoded)
            self.assertGreater(len(values), 8000)
            return math.sqrt(sum(v*v for v in values) / len(values))
        # Foreground audio occupies the first second; the rest is only quiet music.
        self.assertGreater(rms(0.1), rms(2) * 5)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_up_next_music_is_mixed_into_silent_visual(self):
        music = self.root / "media" / "nickelodeon" / "graphics" / "up-next" / "music.wav"
        music.parent.mkdir(parents=True)
        with wave.open(str(music), "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(b"\0\0\0\0" * 48000)
        GraphicsService(self.connection, self.config).scan_catalog()
        result = UpNextRenderer(self.connection, self.config).render(
            "nickelodeon",
            datetime.fromisoformat("2026-08-03T14:10:00-07:00"),
            use_tts=False,
            force=True,
        )
        self.assertTrue(result.output.is_file())
        self.assertGreater(result.duration_seconds, 0)


if __name__ == "__main__":
    unittest.main()
