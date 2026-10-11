from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from _support import WorkspaceTestCase
from retroweather.config import AppPaths, initialize
from retroweather.locations import save_selected_location
from retroweather.models import NormalizedWeatherSnapshot
from retroweather.providers.open_meteo import LocationSearchResult, ProviderRequestError
from retroweather.rendering import RenderError, render_and_publish
from retroweather.rendering.static import _render_lock
from retroweather.weather import build_weather_status, fetch_and_store_weather
from retroweather.web.jobs import ChannelJobManager
from retroweather.web.server import build_status


FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOT_PAYLOAD = json.loads(
    (FIXTURES / "normalized-weather.json").read_text(encoding="utf-8")
)
LOCATION_PAYLOAD = json.loads(
    (FIXTURES / "open-meteo-geocoding-get.json").read_text(encoding="utf-8")
)


class FailingClient:
    last_raw_response = {"error": True, "reason": "fixture provider outage"}

    def fetch(self, location, units, expires_minutes=15):
        raise ProviderRequestError("fixture provider outage")


class RecoveryTests(WorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.paths = AppPaths.discover(self.temporary)
        initialize(self.paths)
        selection = LocationSearchResult.from_api(LOCATION_PAYLOAD).to_selection()
        save_selected_location(self.paths, selection)
        self.snapshot = NormalizedWeatherSnapshot.from_dict(SNAPSHOT_PAYLOAD)
        self.snapshot.save(self.paths.latest_snapshot)
        self.pointer = self.paths.output / "current.json"
        self.active = self.paths.versions / "previous-valid"
        self.active.mkdir()
        (self.active / "keep.txt").write_text("active", encoding="utf-8")
        self.pointer.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation_id": "previous-valid",
                    "manifest": "versions/previous-valid/channel.json",
                }
            ),
            encoding="utf-8",
        )

    def test_dead_render_process_lock_is_recovered_and_build_removed(self):
        lock = self.paths.state / "render.lock"
        lock.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "pid": 2_147_483_647,
                    "started_at": "2026-08-06T12:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        abandoned = self.paths.versions / ".interrupted.building"
        abandoned.mkdir()
        (abandoned / "partial.mp4").write_bytes(b"partial")

        with _render_lock(self.paths):
            self.assertTrue(lock.is_file())
            self.assertFalse(abandoned.exists())

        self.assertFalse(lock.exists())

    def test_live_render_process_lock_is_not_stolen(self):
        lock = self.paths.state / "render.lock"
        lock.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "pid": os.getpid(),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RenderError, "already in progress"):
            with _render_lock(self.paths):
                pass

    def test_old_partial_lock_write_is_recovered(self):
        lock = self.paths.state / "render.lock"
        lock.write_text("{partial", encoding="utf-8")
        old = datetime.now(timezone.utc).timestamp() - 10
        os.utime(lock, (old, old))

        with _render_lock(self.paths):
            owner = json.loads(lock.read_text(encoding="utf-8"))
            self.assertEqual(owner["pid"], os.getpid())

        self.assertFalse(lock.exists())

    def test_missing_ffmpeg_preserves_active_publication(self):
        before = self.pointer.read_bytes()

        def missing(command, **kwargs):
            raise FileNotFoundError(command[0])

        with self.assertRaisesRegex(RenderError, "ffmpeg is required"):
            render_and_publish(
                self.paths,
                runner=missing,
                now=datetime.fromisoformat("2026-08-06T15:00:00-07:00"),
            )

        self.assertEqual(self.pointer.read_bytes(), before)
        self.assertTrue((self.active / "keep.txt").is_file())
        self.assertFalse(any(item.name.endswith(".building") for item in self.paths.versions.iterdir()))

    def test_full_output_during_pointer_swap_removes_orphan_and_keeps_active(self):
        before = self.pointer.read_bytes()
        valid_probe = {
            "streams": [
                {
                    "codec_type": "video", "codec_name": "h264", "width": 640,
                    "height": 480, "pix_fmt": "yuv420p", "avg_frame_rate": "30/1",
                },
                {
                    "codec_type": "audio", "codec_name": "aac",
                    "sample_rate": "48000", "channels": 2,
                },
            ],
            "format": {"duration": "120.021"},
        }

        def fake_segment(output: Path, *args, **kwargs):
            output.write_bytes(b"segment")

        def fake_concat(segments, output: Path, *args, **kwargs):
            output.write_bytes(b"video")

        with (
            patch("retroweather.rendering.static._render_segment", side_effect=fake_segment),
            patch("retroweather.rendering.static._concatenate_segments", side_effect=fake_concat),
            patch("retroweather.rendering.static._probe_video", return_value=valid_probe),
            patch("retroweather.rendering.static._validate_seeking"),
            patch("retroweather.rendering.static.validate_version_directory", return_value=120.021),
            patch(
                "retroweather.rendering.static._write_json_atomic",
                side_effect=OSError("No space left on device"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "No space left"):
                render_and_publish(
                    self.paths,
                    now=datetime.fromisoformat("2026-08-06T15:00:00-07:00"),
                )

        self.assertEqual(self.pointer.read_bytes(), before)
        self.assertEqual(
            [item.name for item in self.paths.versions.iterdir()],
            ["previous-valid"],
        )

    def test_interrupted_snapshot_replace_keeps_previous_cache(self):
        original = self.paths.latest_snapshot.read_bytes()
        changed = json.loads(json.dumps(SNAPSHOT_PAYLOAD))
        changed["current"]["temperature"] = 99.0
        replacement = NormalizedWeatherSnapshot.from_dict(changed)

        with patch("retroweather.models.os.replace", side_effect=OSError("interrupted")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                replacement.save(self.paths.latest_snapshot)

        self.assertEqual(self.paths.latest_snapshot.read_bytes(), original)
        self.assertFalse(any(self.paths.normalized_cache.glob(".*.tmp")))

    def test_corrupt_cache_plus_provider_failure_still_serves_active_channel(self):
        self.paths.latest_snapshot.write_text("{broken", encoding="utf-8")
        before = self.pointer.read_bytes()
        failed_at = datetime.now(timezone.utc) + timedelta(hours=2)

        with self.assertRaisesRegex(ProviderRequestError, "provider outage"):
            fetch_and_store_weather(
                self.paths,
                FailingClient(),
                clock=lambda: failed_at,
            )

        weather = build_weather_status(self.paths, now=failed_at)
        application = build_status(self.paths)
        self.assertFalse(weather.available)
        self.assertIn("invalid JSON", weather.snapshot_error)
        self.assertEqual(weather.state, "failed")
        self.assertTrue(application["published_channel"])
        self.assertEqual(self.pointer.read_bytes(), before)

    def test_running_job_state_becomes_interrupted_failure_after_restart(self):
        self.paths.render_status.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "state": "running",
                    "action": "fetch-render",
                    "stage": "Joining forecast segments",
                    "progress": 68,
                    "started_at": "2026-08-06T15:00:00+00:00",
                    "finished_at": None,
                    "generation_id": None,
                    "validation_result": None,
                    "error": None,
                    "last_successful_publication": None,
                    "last_successful_generation": None,
                    "weather_changed": True,
                }
            ),
            encoding="utf-8",
        )

        restored = ChannelJobManager(self.paths, weather_client=object()).status()

        self.assertEqual(restored["state"], "failed")
        self.assertEqual(restored["stage"], "Interrupted by application restart")
        self.assertIn("ended when RetroWeather stopped", restored["error"])
