from __future__ import annotations

import io
import json
import shutil
import unittest
import uuid
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from retrotv.cli import main
from retrotv.synthetic import SyntheticChannelReader


class SyntheticChannelReaderTests(unittest.TestCase):
    def setUp(self):
        project = Path(__file__).resolve().parents[1]
        test_root = project / ".test-tmp"
        test_root.mkdir(exist_ok=True)
        self.temporary = test_root / f"synthetic-{uuid.uuid4().hex}"
        self.temporary.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temporary, ignore_errors=True)

    def publish(
        self,
        generation_id: str,
        *,
        generated_at: str = "2026-08-09T10:00:00-07:00",
        valid_until: str = "2026-08-09T10:30:00-07:00",
    ) -> Path:
        version = self.temporary / "versions" / generation_id
        version.mkdir(parents=True)
        (version / "weather-channel.mp4").write_bytes(b"synthetic-video")
        manifest = {
            "schema_version": 1,
            "channel": {"slug": "weather", "display_name": "RetroWeather"},
            "generation_id": generation_id,
            "generated_at": generated_at,
            "valid_until": valid_until,
            "timezone": "America/Los_Angeles",
            "video": "weather-channel.mp4",
            "duration_seconds": 240.0,
            "attribution": ["Weather data by Open-Meteo"],
            "segments": [
                {
                    "id": "forecast",
                    "start_seconds": 0,
                    "end_seconds": 240,
                    "title": "Local Forecast",
                    "condition_theme": "sunny/day",
                    "background": None,
                }
            ],
        }
        (version / "channel.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        (self.temporary / "current.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generation_id": generation_id,
                    "manifest": f"versions/{generation_id}/channel.json",
                }
            ),
            encoding="utf-8",
        )
        return version

    def test_discovers_valid_channel_and_reports_freshness(self):
        version = self.publish("generation-1")
        status = SyntheticChannelReader(
            self.temporary, expected_slug="weather"
        ).refresh(now=datetime(2026, 8, 9, 17, 10, tzinfo=timezone.utc))

        self.assertTrue(status.ready)
        self.assertEqual(status.generation_id, "generation-1")
        self.assertEqual(status.schema_version, 1)
        self.assertEqual(status.age_seconds, 600.0)
        self.assertFalse(status.stale)
        self.assertFalse(status.using_last_known_good)
        self.assertEqual(status.video_path, (version / "weather-channel.mp4").resolve())

    def test_stale_is_based_on_manifest_valid_until(self):
        self.publish("generation-1")
        status = SyntheticChannelReader(self.temporary).refresh(
            now=datetime(2026, 8, 9, 17, 31, tzinfo=timezone.utc)
        )
        self.assertTrue(status.ready)
        self.assertTrue(status.stale)

    def test_two_readers_share_wall_clock_playback_position(self):
        self.publish("generation-1")
        when = datetime(2026, 8, 9, 17, 10, 17, 250000, tzinfo=timezone.utc)
        first = SyntheticChannelReader(
            self.temporary, expected_slug="weather"
        ).playback_position(now=when)
        second = SyntheticChannelReader(
            self.temporary, expected_slug="weather"
        ).playback_position(now=when)

        self.assertAlmostEqual(first.offset_seconds, when.timestamp() % 240.0)
        self.assertAlmostEqual(first.offset_seconds, second.offset_seconds)
        self.assertEqual(first.segment_id, second.segment_id)
        self.assertEqual(first.manifest.generation_id, "generation-1")

    def test_rejects_absolute_and_traversing_pointer_paths(self):
        self.publish("generation-1")
        pointer_path = self.temporary / "current.json"
        for unsafe in (
            str((self.temporary / "versions/generation-1/channel.json").resolve()),
            "../outside/channel.json",
            r"..\outside\channel.json",
            r"C:\outside\channel.json",
            r"C:outside\channel.json",
        ):
            pointer_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "generation_id": "generation-1",
                        "manifest": unsafe,
                    }
                ),
                encoding="utf-8",
            )
            status = SyntheticChannelReader(self.temporary).refresh()
            self.assertFalse(status.ready, unsafe)
            self.assertIn("safe relative path", status.error)

    def test_deleted_last_good_video_is_not_reported_as_playable(self):
        version = self.publish("generation-1")
        reader = SyntheticChannelReader(self.temporary, expected_slug="weather")
        self.assertTrue(reader.refresh().ready)
        (version / "weather-channel.mp4").unlink()
        self.assertFalse(reader.refresh().ready)
        self.assertIsNone(reader.last_good)
        self.publish("generation-2")
        self.assertTrue(reader.refresh().ready)

    def test_rejects_unsafe_video_path(self):
        version = self.publish("generation-1")
        manifest_path = version / "channel.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["video"] = "../../outside.mp4"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        status = SyntheticChannelReader(self.temporary).refresh()
        self.assertFalse(status.ready)
        self.assertIn("safe relative path", status.error)

    def test_retains_last_good_manifest_when_replacement_is_invalid(self):
        self.publish("generation-1")
        reader = SyntheticChannelReader(self.temporary, expected_slug="weather")
        accepted = reader.refresh(
            now=datetime(2026, 8, 9, 17, 10, tzinfo=timezone.utc)
        )
        self.assertTrue(accepted.ready)

        self.publish("generation-2")
        manifest = self.temporary / "versions/generation-2/channel.json"
        manifest.write_text("{not-json", encoding="utf-8")
        fallback = reader.refresh(
            now=datetime(2026, 8, 9, 17, 11, tzinfo=timezone.utc)
        )

        self.assertTrue(fallback.ready)
        self.assertEqual(fallback.generation_id, "generation-1")
        self.assertTrue(fallback.using_last_known_good)
        self.assertIn("invalid JSON", fallback.error)

    def test_generation_mismatch_and_invalid_timeline_are_rejected(self):
        version = self.publish("generation-1")
        manifest_path = version / "channel.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["generation_id"] = "other-generation"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        mismatch = SyntheticChannelReader(self.temporary).refresh()
        self.assertFalse(mismatch.ready)
        self.assertIn("expected 'generation-1'", mismatch.error)

        manifest["generation_id"] = "generation-1"
        manifest["segments"][0]["end_seconds"] = 200
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        timeline = SyntheticChannelReader(self.temporary).refresh()
        self.assertFalse(timeline.ready)
        self.assertIn("timeline", timeline.error)


class SyntheticChannelCliTests(unittest.TestCase):
    def test_weather_status_does_not_open_the_media_database(self):
        project = Path(__file__).resolve().parents[1]
        profile = project / "config" / "development.windows.local.toml"
        output = io.StringIO()
        with patch(
            "retrotv.cli.connect_database",
            side_effect=AssertionError("weather reader must bypass the database"),
        ):
            with redirect_stdout(output):
                result = main(
                    [
                        "--config",
                        str(profile),
                        "weather-status",
                        "--root",
                        str(
                            project
                            / "RetroWeather"
                            / "synthetic-channels"
                            / "weather"
                        ),
                    ]
                )
        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertTrue(payload["ready"])
        self.assertTrue(payload["video_path"].endswith("weather-channel.mp4"))


if __name__ == "__main__":
    unittest.main()
