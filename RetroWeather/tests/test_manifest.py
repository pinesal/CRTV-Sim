from __future__ import annotations

import copy
import json
from pathlib import Path

from _support import WorkspaceTestCase
from retroweather.models import NormalizedWeatherSnapshot
from retroweather.publication import ManifestValidationError, validate_version_manifest


FIXTURE = Path(__file__).parent / "fixtures" / "normalized-weather.json"


class ManifestValidationTests(WorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.version = self.temporary / "version"
        self.version.mkdir()
        snapshot = NormalizedWeatherSnapshot.from_dict(
            json.loads(FIXTURE.read_text(encoding="utf-8"))
        )
        snapshot.save(self.version / "normalized-weather.json")
        (self.version / "weather-channel.mp4").write_bytes(b"fixture video")
        self.manifest = {
            "schema_version": 1,
            "channel": {"slug": "weather", "display_name": "RetroWeather"},
            "generation_id": "fixture-generation",
            "generated_at": "2026-08-06T15:00:00-07:00",
            "valid_until": "2026-08-06T15:30:00-07:00",
            "timezone": "America/Los_Angeles",
            "video": "weather-channel.mp4",
            "duration_seconds": 10.021,
            "attribution": ["Weather data by fixture"],
            "segments": [
                {
                    "id": "first", "start_seconds": 0, "end_seconds": 4,
                    "title": "First", "condition_theme": "default/day", "background": None,
                },
                {
                    "id": "second", "start_seconds": 4, "end_seconds": 10,
                    "title": "Second", "condition_theme": "sunny/day", "background": "sky.jpg",
                },
            ],
        }

    def _save(self, payload=None) -> None:
        (self.version / "channel.json").write_text(
            json.dumps(payload or self.manifest), encoding="utf-8"
        )

    def test_complete_portable_manifest_is_accepted(self) -> None:
        self._save()

        validated = validate_version_manifest(
            self.version, expected_generation_id="fixture-generation"
        )

        self.assertEqual(validated.video_path.name, "weather-channel.mp4")
        self.assertEqual(validated.timeline_duration_seconds, 10)

    def test_absolute_and_traversing_video_paths_are_rejected(self) -> None:
        for unsafe in (str((self.temporary / "outside.mp4").resolve()), "../outside.mp4"):
            payload = copy.deepcopy(self.manifest)
            payload["video"] = unsafe
            self._save(payload)
            with self.assertRaisesRegex(ManifestValidationError, "safe relative path"):
                validate_version_manifest(self.version)

    def test_missing_reference_and_unknown_manifest_field_are_rejected(self) -> None:
        payload = copy.deepcopy(self.manifest)
        payload["video"] = "missing.mp4"
        self._save(payload)
        with self.assertRaisesRegex(ManifestValidationError, "missing file"):
            validate_version_manifest(self.version)

        payload = copy.deepcopy(self.manifest)
        payload["unexpected"] = True
        self._save(payload)
        with self.assertRaisesRegex(ManifestValidationError, "unknown fields"):
            validate_version_manifest(self.version)

    def test_timeline_gap_and_out_of_bounds_segment_are_rejected(self) -> None:
        payload = copy.deepcopy(self.manifest)
        payload["segments"][1]["start_seconds"] = 5
        self._save(payload)
        with self.assertRaisesRegex(ManifestValidationError, "previous end"):
            validate_version_manifest(self.version)

        payload = copy.deepcopy(self.manifest)
        payload["segments"][1]["end_seconds"] = 11
        self._save(payload)
        with self.assertRaisesRegex(ManifestValidationError, "beyond the video"):
            validate_version_manifest(self.version)
