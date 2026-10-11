import hashlib
import io
import json
import shutil
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch, sentinel

from retrotv.config import AppConfig
from retrotv.channel_management import ChannelDeletionService
from retrotv.db import apply_migrations, connect_database, ensure_storage_root
from retrotv.guide import GuideService
from retrotv.internet_media import (
    InternetCatalogClient,
    InternetMediaService,
    PreparationService,
    WatchProgressService,
    parse_episode_date,
)
from retrotv.normalization import AnalysisResult
from retrotv.playback import PlaybackController, PlaybackError, Player, ScheduleResolver
from retrotv.probe import DecodeResult, ProbeResult
from retrotv.restore_point import RestorePointService
from retrotv.scanner import LibraryScanner


class FixtureCatalog:
    def __init__(self, items):
        self.items = items

    def preview(self, url, show_title):
        episodes = []
        for item in self.items[url]:
            copy = dict(item)
            copy.setdefault("display_title", f"{show_title} — {copy.get('episode_date') or copy['remote_filename']}")
            copy.setdefault("date_source", "filename" if copy.get("episode_date") else None)
            copy.setdefault("duration_seconds", 3600.0)
            copy.setdefault("size_bytes", 4)
            copy.setdefault("checksum_kind", None)
            copy.setdefault("checksum_value", None)
            copy.setdefault("metadata_status", "ready" if copy.get("episode_date") else "review")
            episodes.append(copy)
        return {
            "source_type": "direct_video",
            "identifier": None,
            "source_url": url,
            "title": show_title,
            "episodes": episodes,
        }


class FakeResponse(io.BytesIO):
    def __init__(self, value, *, status=200, headers=None):
        super().__init__(value)
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class ProgressPlayer(Player):
    def __init__(self):
        self.loaded = []
        self.running = False
        self.position = 0.0
        self.completed = False

    def start(self):
        self.running = True

    def load(self, target):
        self.loaded.append(target)
        self.position = target.media_offset_seconds
        self.running = True

    def is_running(self):
        return self.running

    def stop(self):
        self.running = False

    def playback_position(self):
        return self.position

    def poll_completed(self):
        value = self.completed
        self.completed = False
        return value

    @property
    def supports_progress(self):
        return True


class BasicPlayer(ProgressPlayer):
    @property
    def supports_progress(self):
        return False


def episode(key, date_value, filename=None):
    return {
        "stable_key": key,
        "remote_url": f"https://video.example/{filename or key + '.mp4'}",
        "remote_filename": filename or key + ".mp4",
        "episode_date": date_value,
    }


class InternetMediaTests(unittest.TestCase):
    def setUp(self):
        project = Path(__file__).resolve().parents[1]
        base = project / ".test-tmp"
        base.mkdir(exist_ok=True)
        self.temporary = base / f"internet-media-{uuid.uuid4().hex}"
        self.media = self.temporary / "media"
        self.state = self.temporary / "state"
        self.media.mkdir(parents=True)
        self.state.mkdir()
        self.profile = self.temporary / "test.toml"
        self.profile.write_text("[application]\n", encoding="utf-8")
        self.config = AppConfig(
            profile_path=self.profile,
            project_root=project,
            data={
                "application": {
                    "database_path": str(self.temporary / "retrotv.sqlite3"),
                    "timezone": "America/Los_Angeles",
                },
                "storage_roots": {
                    "media": str(self.media),
                    "normalized": str(self.temporary / "normalized"),
                    "previews": str(self.temporary / "previews"),
                    "state": str(self.state),
                    "originals_backup": str(self.temporary / "backup"),
                },
                "tools": {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"},
                "library": {"dayparts": ["morning", "afternoon", "evening", "night"]},
                "internet_media": {
                    "cache_directory": str(self.temporary / "cache"),
                    "cache_limit_gib": 50,
                    "minimum_free_gib": 0,
                    "maximum_retries": 0,
                },
            },
        )
        self.connection = connect_database(self.config.database_path)
        apply_migrations(self.connection, project / "migrations")

    def tearDown(self):
        self.connection.close()
        shutil.rmtree(self.temporary, ignore_errors=True)

    def service(self, catalog):
        return InternetMediaService(self.connection, self.config, client=FixtureCatalog(catalog))

    def create_channel(self, service, *, storage="save_space"):
        return service.configure_channel(
            {
                "display_name": "Monday Night Wars",
                "channel_number": 98,
                "playback_mode": "watch_at_your_pace",
                "storage_mode": storage,
                "prepare_ahead": 1,
                "same_date_order": ["Raw", "Nitro", "Thunder"],
            }
        )

    def add(self, service, url, show, episodes):
        return service.add_source(
            {
                "channel": "monday-night-wars",
                "show_title": show,
                "url": url,
                "selected_keys": [item["stable_key"] for item in episodes],
            }
        )

    def make_ready(self, episode_id, *, aspect_mode="native_4_3"):
        path = self.temporary / "cache" / f"{episode_id}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ready")
        self.connection.execute(
            "UPDATE remote_episodes SET local_path=?,aspect_mode=? WHERE id=?",
            (str(path), aspect_mode, episode_id),
        )
        self.connection.execute(
            """INSERT INTO internet_preparation_jobs(episode_id,state,priority,created_at,updated_at,completed_at)
               VALUES (?,'ready',0,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')
               ON CONFLICT(episode_id) DO UPDATE SET state='ready'""",
            (episode_id,),
        )
        self.connection.commit()
        return path

    def test_date_parser_rejects_conflicting_and_invalid_dates(self):
        self.assertEqual(parse_episode_date("Raw 1998-01-05.mp4"), ("1998-01-05", "filename"))
        self.assertEqual(parse_episode_date("Raw 01-05-1998.mp4"), ("1998-01-05", "filename"))
        self.assertEqual(parse_episode_date("Raw 1998-02-31.mp4"), (None, None))
        self.assertEqual(parse_episode_date("1998-01-05 copy 1998-01-12.mp4"), (None, None))

    def test_archive_derivatives_collapse_and_file_metadata_date_is_used(self):
        client = InternetCatalogClient()
        client._open_json = lambda _url: {
            "metadata": {"title": "Raw 1998", "date": "1998-01-01"},
            "files": [
                {"name": "odd-original.mpg", "format": "MPEG", "size": "50"},
                {
                    "name": "odd-original.ia.mp4",
                    "original": "odd-original.mpg",
                    "format": "h.264 IA",
                    "size": "20",
                    "title": "Raw 1998-01-05",
                },
            ],
        }
        result = client.preview("https://archive.org/details/example", "Raw")
        self.assertEqual(len(result["episodes"]), 1)
        self.assertEqual(result["episodes"][0]["remote_filename"], "odd-original.ia.mp4")
        self.assertEqual(result["episodes"][0]["episode_date"], "1998-01-05")
        self.assertEqual(result["episodes"][0]["date_source"], "file_metadata_title")

    def test_catalog_requests_use_the_supplied_verified_tls_context(self):
        client = InternetCatalogClient(ssl_context=sentinel.tls_context)
        response = FakeResponse(json.dumps({"metadata": {}, "files": []}).encode("utf-8"))
        with patch(
            "retrotv.internet_media.urllib.request.urlopen", return_value=response
        ) as open_url:
            self.assertEqual(client._open_json("https://archive.org/metadata/example"), {"metadata": {}, "files": []})
        self.assertIs(open_url.call_args.kwargs["context"], sentinel.tls_context)

    def test_playlist_merges_shows_by_date_priority_then_manual_order(self):
        raw = [episode("raw-1", "1998-01-05")]
        nitro = [episode("nitro-1", "1998-01-05"), episode("nitro-2", "1998-01-12")]
        thunder = [episode("thunder-1", "1998-01-08")]
        catalog = {"https://video.example/raw.mp4": raw, "https://video.example/nitro.mp4": nitro, "https://video.example/thunder.mp4": thunder}
        service = self.service(catalog)
        self.create_channel(service)
        self.add(service, "https://video.example/raw.mp4", "Raw", raw)
        self.add(service, "https://video.example/nitro.mp4", "Nitro", nitro)
        self.add(service, "https://video.example/thunder.mp4", "Thunder", thunder)
        playlist = WatchProgressService(self.connection, self.config).playlist("monday-night-wars")
        self.assertEqual([item["stable_key"] for item in playlist], ["raw-1", "nitro-1", "thunder-1", "nitro-2"])

    def test_start_date_checkpoint_completion_and_caught_up(self):
        items = [episode("one", "1998-01-05"), episode("two", "1998-01-12")]
        url = "https://video.example/raw.mp4"
        service = self.service({url: items})
        self.create_channel(service)
        detail = self.add(service, url, "Raw", items)
        result = service.set_watching_position("monday-night-wars", start_date="1998-01-06", position_seconds=75)
        self.assertEqual(result["current"]["stable_key"], "two")
        self.assertEqual(result["position_seconds"], 75)
        progress = WatchProgressService(self.connection, self.config)
        progress.save_position("monday-night-wars", int(result["current"]["id"]), 90)
        self.assertEqual(progress.status("monday-night-wars")["position_seconds"], 90)
        completed = progress.complete("monday-night-wars", int(result["current"]["id"]))
        self.assertEqual(completed["state"], "caught_up")
        self.assertIsNone(completed["current"])
        watched = self.connection.execute("SELECT completed_at FROM remote_episodes WHERE id=?", (detail["episodes"][1]["id"],)).fetchone()[0]
        self.assertIsNotNone(watched)

    def test_watch_resolver_resumes_saved_position_and_controller_checkpoints(self):
        item = episode("one", "1998-01-05")
        url = "https://video.example/raw.mp4"
        service = self.service({url: [item]})
        self.create_channel(service)
        detail = self.add(service, url, "Raw", [item])
        episode_id = int(detail["episodes"][0]["id"])
        self.make_ready(episode_id, aspect_mode="stretched_4_3")
        service.set_watching_position("monday-night-wars", episode_id, 45)
        resolver = ScheduleResolver(self.connection, self.config)
        target = resolver.resolve("monday-night-wars", datetime.now(resolver.scheduler.timezone))
        self.assertEqual(target.entry_type, "watch_episode")
        self.assertEqual(target.media_offset_seconds, 45)
        self.assertEqual(target.video_filter, "lavfi=[setsar=1,setdar=4/3]")
        player = ProgressPlayer()
        controller = PlaybackController(resolver, player)
        controller.tune_once("monday-night-wars")
        player.position = 79
        controller._checkpoint_watch()
        self.assertEqual(WatchProgressService(self.connection, self.config).status("monday-night-wars")["position_seconds"], 79)
        guide = GuideService(self.connection, self.config).snapshot()
        listing = next(channel for channel in guide["channels"] if channel["slug"] == "monday-night-wars")
        self.assertEqual(listing["playback_mode"], "watch_at_your_pace")
        self.assertEqual(listing["progress_seconds"], 79)
        self.assertEqual(listing["now"]["title"], target.title)
        self.assertEqual(listing["now"]["starts_at"], "")

    def test_watch_channel_requires_progress_capable_player(self):
        item = episode("one", "1998-01-05")
        url = "https://video.example/raw.mp4"
        service = self.service({url: [item]})
        self.create_channel(service)
        detail = self.add(service, url, "Raw", [item])
        episode_id = int(detail["episodes"][0]["id"])
        self.make_ready(episode_id)
        service.set_watching_position("monday-night-wars", episode_id)
        resolver = ScheduleResolver(self.connection, self.config)
        with self.assertRaisesRegex(PlaybackError, "require the mpv"):
            PlaybackController(resolver, BasicPlayer()).tune_once("monday-night-wars")

    def test_completed_playlist_resolves_to_non_looping_caught_up_slate(self):
        item = episode("one", "1998-01-05")
        url = "https://video.example/raw.mp4"
        service = self.service({url: [item]})
        self.create_channel(service)
        detail = self.add(service, url, "Raw", [item])
        episode_id = int(detail["episodes"][0]["id"])
        self.make_ready(episode_id)
        service.set_watching_position("monday-night-wars", episode_id)
        progress = WatchProgressService(self.connection, self.config)
        progress.complete("monday-night-wars", episode_id)
        slate = self.temporary / "caught-up.mp4"
        slate.write_bytes(b"slate")
        resolver = ScheduleResolver(self.connection, self.config)
        with patch("retrotv.playback.ensure_continuity_slate", return_value=slate):
            target = resolver.resolve("monday-night-wars", datetime.now(resolver.scheduler.timezone))
        self.assertEqual(target.entry_type, "watch_caught_up")
        self.assertEqual(target.title, "You're caught up")

    def test_refresh_preserves_id_and_progress_for_rename_and_flags_replacement(self):
        old = episode("old-name", "1998-01-05", "Raw.1998-01-05.mp4")
        old["checksum_kind"] = "md5"
        old["checksum_value"] = "a" * 32
        url = "https://video.example/raw.mp4"
        catalog = {url: [old]}
        fixture = FixtureCatalog(catalog)
        service = InternetMediaService(self.connection, self.config, client=fixture)
        self.create_channel(service)
        detail = self.add(service, url, "Raw", [old])
        episode_id = int(detail["episodes"][0]["id"])
        local = self.temporary / "cache" / "local.mp4"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"old")
        self.connection.execute("UPDATE remote_episodes SET local_path=? WHERE id=?", (str(local), episode_id))
        self.connection.commit()
        renamed = episode("new-name", "1998-01-05", "Raw 01-05-1998.mp4")
        renamed["checksum_kind"] = "md5"
        renamed["checksum_value"] = "b" * 32
        catalog[url] = [renamed]
        refreshed = service.refresh_source(int(detail["id"]))
        self.assertEqual(refreshed["episodes"][0]["id"], episode_id)
        self.assertEqual(refreshed["episodes"][0]["stable_key"], "new-name")
        self.assertEqual(refreshed["episodes"][0]["availability_status"], "changed")

    def test_older_refresh_item_enters_catchup_review_without_rewind(self):
        current = episode("current", "1998-01-12")
        url = "https://video.example/raw.mp4"
        catalog = {url: [current]}
        service = self.service(catalog)
        self.create_channel(service)
        detail = self.add(service, url, "Raw", [current])
        service.set_watching_position("monday-night-wars", int(detail["episodes"][0]["id"]))
        catalog[url].append(episode("older", "1998-01-05"))
        refreshed = service.refresh_source(int(detail["id"]))
        older = next(item for item in refreshed["episodes"] if item["stable_key"] == "older")
        self.assertEqual(older["catchup_status"], "review")
        self.assertEqual(older["selected"], 0)
        self.assertEqual(WatchProgressService(self.connection, self.config).status("monday-night-wars")["current"]["stable_key"], "current")

    def test_manual_exclusion_and_source_disable_survive_refresh_and_sidecar_sync(self):
        uncertain = episode("uncertain", None, "odd-name.mp4")
        url = "https://video.example/raw.mp4"
        service = self.service({url: [uncertain]})
        self.create_channel(service)
        detail = self.add(service, url, "Raw", [uncertain])
        episode_id = int(detail["episodes"][0]["id"])
        service.update_episode(episode_id, {"selected": False, "excluded": True})
        refreshed = service.refresh_source(int(detail["id"]))
        self.assertEqual(refreshed["episodes"][0]["metadata_status"], "excluded")
        service.disable_source(int(detail["id"]))
        sidecar = self.media / Path(detail["source_relative_path"])
        self.assertFalse(json.loads(sidecar.read_text(encoding="utf-8"))["enabled"])
        service.sync_source_files()
        self.assertEqual(self.connection.execute("SELECT enabled FROM internet_sources WHERE id=?", (detail["id"],)).fetchone()[0], 0)

    def test_save_space_cleanup_only_deletes_completed_managed_cache(self):
        item = episode("one", "1998-01-05")
        url = "https://video.example/raw.mp4"
        service = self.service({url: [item]})
        self.create_channel(service)
        detail = self.add(service, url, "Raw", [item])
        episode_id = int(detail["episodes"][0]["id"])
        cache_file = Path(self.config.data["internet_media"]["cache_directory"]) / "channel" / "one.mp4"
        cache_file.parent.mkdir(parents=True)
        cache_file.write_bytes(b"data")
        self.connection.execute("UPDATE remote_episodes SET local_path=? WHERE id=?", (str(cache_file), episode_id))
        self.connection.commit()
        preparation = PreparationService(self.connection, self.config)
        self.assertFalse(preparation.delete_completed_cache(episode_id))
        self.connection.execute("UPDATE remote_episodes SET completed_at='2026-01-01T00:00:00+00:00' WHERE id=?", (episode_id,))
        self.connection.commit()
        self.assertTrue(preparation.delete_completed_cache(episode_id))
        self.assertFalse(cache_file.exists())

    def test_empty_cache_previews_deletes_and_protects_active_file(self):
        item = episode("one", "1998-01-05")
        service = self.service({"https://video.example/raw.mp4": [item]})
        self.create_channel(service)
        detail = self.add(service, "https://video.example/raw.mp4", "Raw", [item])
        episode_id = int(detail["episodes"][0]["id"])
        cache = Path(self.config.data["internet_media"]["cache_directory"])
        active = cache / "active.mp4"
        removable = cache / "removable.mp4"
        incomplete = cache / "resume.partial"
        active.parent.mkdir(parents=True, exist_ok=True)
        active.write_bytes(b"active")
        removable.write_bytes(b"remove me")
        incomplete.write_bytes(b"partial")
        self.connection.execute(
            "UPDATE remote_episodes SET local_path=? WHERE id=?",
            (str(removable), episode_id),
        )
        self.connection.execute(
            """INSERT INTO internet_preparation_jobs(
                   episode_id,state,temporary_path,created_at,updated_at
               ) VALUES (?,'retry',?,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')""",
            (episode_id, str(incomplete)),
        )
        self.connection.commit()
        preparation = PreparationService(
            self.connection, self.config, ssl_context=sentinel.download_tls_context
        )
        preview = preparation.preview_empty_cache(active_path=active)
        self.assertEqual(preview["files"], 1)
        self.assertEqual(preview["bytes"], len(b"remove me"))
        result = preparation.empty_cache(active_path=active)
        self.assertEqual(result["files_removed"], 1)
        self.assertTrue(active.exists())
        self.assertTrue(incomplete.exists())
        self.assertFalse(removable.exists())
        self.assertIsNone(
            self.connection.execute(
                "SELECT local_path FROM remote_episodes WHERE id=?", (episode_id,)
            ).fetchone()[0]
        )

    def test_save_space_download_validates_analyzes_and_becomes_ready_without_normalization(self):
        item = episode("one", "1998-01-05")
        url = "https://video.example/raw.mp4"
        service = self.service({url: [item]})
        self.create_channel(service)
        detail = self.add(service, url, "Raw", [item])
        service.set_watching_position(
            "monday-night-wars", int(detail["episodes"][0]["id"])
        )
        preparation = PreparationService(self.connection, self.config)
        preparation.queue_channel("monday-night-wars")
        response = FakeResponse(b"test", headers={"Content-Length": "4", "ETag": '"one"'})
        analysis = AnalysisResult("analyzed", "native_4_3", "native_4_3", 1.0, None, [], "ok")
        with (
            patch("retrotv.internet_media.urllib.request.urlopen", return_value=response),
            patch("retrotv.internet_media.probe_media", return_value=ProbeResult(True, {"duration_seconds": 3600.0, "width": 640, "height": 480})),
            patch("retrotv.internet_media.decode_sample", return_value=DecodeResult(True)),
            patch("retrotv.internet_media.NormalizationAnalyzer.analyze", return_value=analysis),
            patch.object(PreparationService, "_compatibility_convert") as conversion,
        ):
            result = preparation.run_next()
        self.assertEqual(result["state"], "ready")
        self.assertTrue(Path(result["path"]).is_file())
        conversion.assert_not_called()
        row = self.connection.execute("SELECT retained,aspect_mode FROM remote_episodes").fetchone()
        self.assertEqual((row["retained"], row["aspect_mode"]), (0, "native_4_3"))

    def test_disabled_normalization_download_skips_aspect_analysis_and_is_ready(self):
        self.config.data["normalization"] = {"enabled": False}
        item = episode("one", "1998-01-05")
        url = "https://video.example/raw.mp4"
        service = self.service({url: [item]})
        self.create_channel(service)
        detail = self.add(service, url, "Raw", [item])
        service.set_watching_position("monday-night-wars", int(detail["episodes"][0]["id"]))
        preparation = PreparationService(self.connection, self.config)
        preparation.queue_channel("monday-night-wars")
        with (
            patch("retrotv.internet_media.urllib.request.urlopen", return_value=FakeResponse(b"test", headers={"Content-Length":"4"})),
            patch("retrotv.internet_media.probe_media", return_value=ProbeResult(True, {"duration_seconds":3600.,"width":1920,"height":1080})),
            patch("retrotv.internet_media.decode_sample", return_value=DecodeResult(True)),
            patch("retrotv.internet_media.NormalizationAnalyzer.analyze") as analyzer,
        ):
            result = preparation.run_next()
        self.assertEqual(result["state"], "ready")
        analyzer.assert_not_called()
        self.assertTrue(Path(result["path"]).is_file())

    def test_watch_channel_does_not_prepare_or_play_before_start_is_confirmed(self):
        item = episode("one", "1998-01-05")
        url = "https://video.example/raw.mp4"
        service = self.service({url: [item]})
        self.create_channel(service)
        detail = self.add(service, url, "Raw", [item])
        preparation = PreparationService(self.connection, self.config)

        preparation.queue_channel("monday-night-wars")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM internet_preparation_jobs WHERE state='queued'"
            ).fetchone()[0],
            0,
        )
        slate = self.temporary / "choose-position.mp4"
        slate.write_bytes(b"slate")
        resolver = ScheduleResolver(self.connection, self.config)
        with patch("retrotv.playback.ensure_continuity_slate", return_value=slate):
            target = resolver.resolve(
                "monday-night-wars", datetime.now(resolver.scheduler.timezone)
            )
        self.assertEqual(target.entry_type, "watch_waiting_slate")
        self.assertIn("Choose a starting position", target.title)

        service.set_watching_position(
            "monday-night-wars", int(detail["episodes"][0]["id"])
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM internet_preparation_jobs WHERE state='queued'"
            ).fetchone()[0],
            1,
        )

    def test_scheduled_channel_prepares_two_and_advances_only_after_airtime(self):
        items = [
            episode("one", "1998-01-05"),
            episode("two", "1998-01-12"),
            episode("three", "1998-01-19"),
        ]
        url = "https://video.example/raw.mp4"
        service = self.service({url: items})
        service.configure_channel(
            {
                "display_name": "Monday Night Wars",
                "channel_number": 98,
                "playback_mode": "scheduled",
                "storage_mode": "keep",
                "prepare_ahead": 1,
            }
        )
        detail = self.add(service, url, "Raw", items)
        preparation = PreparationService(self.connection, self.config)
        preparation.queue_channel("monday-night-wars")
        states = {
            row["stable_key"]: row["state"]
            for row in self.connection.execute(
                """SELECT e.stable_key,COALESCE(j.state,'cataloged') state
                   FROM remote_episodes e LEFT JOIN internet_preparation_jobs j ON j.episode_id=e.id"""
            )
        }
        self.assertEqual(states, {"one": "queued", "two": "queued", "three": "cataloged"})

        first_id = int(detail["episodes"][0]["id"])
        root_id = ensure_storage_root(self.connection, "media")
        now = datetime.now(UTC)
        asset = self.connection.execute(
            """INSERT INTO assets(
                   source_root_id,source_relative_path,original_filename,file_size,modified_ns,
                   channel_slug,first_seen_at,last_seen_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (root_id, "monday-night-wars/all-day/episodes/Raw/one.mp4", "one.mp4", 4, 1,
             "monday-night-wars", now.isoformat(), now.isoformat()),
        )
        channel_id = self.connection.execute(
            "SELECT id FROM channels WHERE slug='monday-night-wars'"
        ).fetchone()[0]
        generation = self.connection.execute(
            """INSERT INTO schedule_generations(
                   channel_id,schedule_date,seed,configuration_fingerprint,status,
                   validation_status,generated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (channel_id, now.date().isoformat(), "test", "test", "active", "valid", now.isoformat()),
        )
        self.connection.execute(
            """INSERT INTO schedule_entries(
                   generation_id,channel_id,asset_id,starts_at,ends_at,media_start_ms,
                   media_end_ms,entry_type,title,daypart_slug,sequence_position,is_program)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1)""",
            (
                generation.lastrowid, channel_id, asset.lastrowid,
                (now - timedelta(hours=2)).isoformat(), (now - timedelta(hours=1)).isoformat(),
                0, 3_600_000, "program", "One", "all-day", 0,
            ),
        )
        self.connection.execute(
            "UPDATE remote_episodes SET asset_id=? WHERE id=?", (asset.lastrowid, first_id)
        )
        self.connection.commit()

        preparation.queue_channel("monday-night-wars")
        third = self.connection.execute(
            """SELECT j.state FROM internet_preparation_jobs j
               JOIN remote_episodes e ON e.id=j.episode_id WHERE e.stable_key='three'"""
        ).fetchone()
        self.assertEqual(third["state"], "queued")

    def test_switching_scheduled_channel_to_watch_mode_cancels_stale_queue(self):
        items = [episode("one", "1998-01-05"), episode("two", "1998-01-12")]
        url = "https://video.example/raw.mp4"
        service = self.service({url: items})
        service.configure_channel(
            {
                "display_name": "Monday Night Wars",
                "playback_mode": "scheduled",
                "storage_mode": "keep",
                "prepare_ahead": 1,
            }
        )
        detail = self.add(service, url, "Raw", items)
        PreparationService(self.connection, self.config).queue_channel(
            "monday-night-wars"
        )
        first_id = int(detail["episodes"][0]["id"])
        partial = self.media / "monday-night-wars/all-day/episodes/Raw/one.mp4.partial"
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(b"partial")
        self.connection.execute(
            "UPDATE internet_preparation_jobs SET temporary_path=? WHERE episode_id=?",
            (str(partial), first_id),
        )
        self.connection.commit()

        service.configure_channel(
            {
                "display_name": "Monday Night Wars",
                "playback_mode": "watch_at_your_pace",
                "storage_mode": "save_space",
                "prepare_ahead": 1,
            }
        )
        jobs = self.connection.execute(
            "SELECT state,cancel_requested FROM internet_preparation_jobs ORDER BY episode_id"
        ).fetchall()
        self.assertEqual(
            [(row["state"], row["cancel_requested"]) for row in jobs],
            [("cataloged", 0), ("cataloged", 0)],
        )
        self.assertFalse(partial.exists())
        self.assertFalse(
            WatchProgressService(self.connection, self.config)
            .status("monday-night-wars")["position_confirmed"]
        )

    def test_active_download_observes_cancellation_and_removes_partial(self):
        item = episode("one", "1998-01-05")
        url = "https://video.example/raw.mp4"
        service = self.service({url: [item]})
        self.create_channel(service)
        detail = self.add(service, url, "Raw", [item])
        service.set_watching_position(
            "monday-night-wars", int(detail["episodes"][0]["id"])
        )
        preparation = PreparationService(
            self.connection, self.config, ssl_context=sentinel.download_tls_context
        )

        class CancellingResponse(FakeResponse):
            def __init__(inner_self):
                super().__init__(b"test", headers={"Content-Length": "4"})
                inner_self.cancelled = False

            def read(inner_self, size=-1):
                data = super(CancellingResponse, inner_self).read(size)
                if data and not inner_self.cancelled:
                    inner_self.cancelled = True
                    self.connection.execute(
                        "UPDATE internet_preparation_jobs SET cancel_requested=1"
                    )
                    self.connection.commit()
                return data

        with patch(
            "retrotv.internet_media.urllib.request.urlopen",
            return_value=CancellingResponse(),
        ) as open_url:
            result = preparation.run_next()
        self.assertTrue(result["cancelled"])
        self.assertIs(open_url.call_args.kwargs["context"], sentinel.download_tls_context)
        job = self.connection.execute(
            "SELECT state,temporary_path FROM internet_preparation_jobs"
        ).fetchone()
        self.assertEqual((job["state"], job["temporary_path"]), ("cataloged", None))
        self.assertEqual(list(Path(self.config.data["internet_media"]["cache_directory"]).rglob("*.partial")), [])

    def test_channel_delete_removes_records_and_partials_but_preserves_media(self):
        item = episode("one", "1998-01-05")
        url = "https://video.example/raw.mp4"
        service = self.service({url: [item]})
        self.create_channel(service)
        detail = self.add(service, url, "Raw", [item])
        episode_id = int(detail["episodes"][0]["id"])
        finished = self.make_ready(episode_id)
        partial = finished.with_name("unfinished.mp4.partial")
        partial.write_bytes(b"partial")
        self.connection.execute(
            """UPDATE internet_preparation_jobs SET state='failed',temporary_path=?
               WHERE episode_id=?""",
            (str(partial), episode_id),
        )
        self.connection.commit()
        source_file = self.media / Path(detail["source_relative_path"])
        manager = ChannelDeletionService(self.connection, self.config)
        preview = manager.preview("monday-night-wars")
        self.assertEqual(preview["confirmation_text"], "DELETE Monday Night Wars")
        with self.assertRaisesRegex(ValueError, "Type"):
            manager.delete(
                "monday-night-wars", confirmation_text="delete", acknowledge_rescan=True
            )
        result = manager.delete(
            "monday-night-wars",
            confirmation_text="DELETE Monday Night Wars",
            acknowledge_rescan=True,
        )
        self.assertTrue(result["scanner_will_recreate"])
        self.assertFalse(partial.exists())
        self.assertFalse(source_file.exists())
        self.assertTrue(finished.exists())
        self.assertIsNone(
            self.connection.execute(
                "SELECT id FROM channels WHERE slug='monday-night-wars'"
            ).fetchone()
        )
        LibraryScanner(self.connection, self.config)._sync_channels_from_folders(self.media)
        recreated = self.connection.execute(
            "SELECT id FROM channels WHERE slug='monday-night-wars'"
        ).fetchone()
        self.assertIsNotNone(recreated)

    def test_restore_point_is_one_time_verified_and_restores_database(self):
        self.connection.execute("INSERT INTO channels(slug,display_name,enabled) VALUES ('before','Before',1)")
        self.connection.commit()
        service = RestorePointService(self.config)
        first = service.create(self.connection)
        self.assertTrue(first["created"])
        self.assertFalse(service.create(self.connection)["created"])
        self.connection.execute("INSERT INTO channels(slug,display_name,enabled) VALUES ('after','After',1)")
        self.connection.commit()
        self.connection.close()
        result = service.restore()
        self.connection = connect_database(self.config.database_path)
        slugs = [row[0] for row in self.connection.execute("SELECT slug FROM channels ORDER BY slug")]
        self.assertEqual(slugs, ["before"])
        self.assertTrue(Path(result.post_upgrade_database).is_file())
        manifest = json.loads(service.manifest_path.read_text(encoding="utf-8"))
        database_record = next(item for item in manifest["files"] if item["path"] == "database.sqlite3")
        self.assertEqual(database_record["sha256"], hashlib.sha256((service.root / "database.sqlite3").read_bytes()).hexdigest())

    def test_restore_point_does_not_record_current_build_as_previous_build(self):
        portable_root = self.temporary / "portable"
        portable_root.mkdir()
        (portable_root / "RetroTV.exe").write_bytes(b"current-build")
        (portable_root / "previous-build.path").write_text(".\n", encoding="utf-8")
        service = RestorePointService(self.config)
        with patch.dict(
            "os.environ",
            {"RETROTV_PORTABLE_ROOT": str(portable_root), "RETROTV_PREVIOUS_BUILD": ""},
        ):
            service.create(self.connection)
        manifest = json.loads(service.manifest_path.read_text(encoding="utf-8"))
        self.assertIsNone(manifest["previous_build"])


if __name__ == "__main__":
    unittest.main()
