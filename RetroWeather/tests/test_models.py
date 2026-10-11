from __future__ import annotations

import copy
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from _support import WorkspaceTestCase
from retroweather.cli import main
from retroweather.config import AppPaths, initialize
from retroweather.models import (
    MODEL_SCHEMA_VERSION,
    NormalizedWeatherSnapshot,
    SnapshotValidationError,
    UnsupportedSnapshotVersion,
)


FIXTURE = Path(__file__).parent / "fixtures" / "normalized-weather.json"


class NormalizedWeatherTests(WorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.raw = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_fixture_loads_and_exposes_typed_values(self) -> None:
        snapshot = NormalizedWeatherSnapshot.from_dict(self.raw)

        self.assertEqual(snapshot.schema_version, MODEL_SCHEMA_VERSION)
        self.assertEqual(snapshot.location.timezone, "America/Los_Angeles")
        self.assertEqual(snapshot.current.temperature, 78.4)
        self.assertTrue(snapshot.current.is_day)
        self.assertIsNone(snapshot.current.wind_gust)
        self.assertEqual(len(snapshot.hourly), 2)
        self.assertEqual(len(snapshot.daily), 2)
        self.assertEqual(len(snapshot.alerts), 1)
        self.assertIsNotNone(snapshot.current.observed_at.utcoffset())

    def test_fixture_round_trips_through_atomic_file_save(self) -> None:
        original = NormalizedWeatherSnapshot.load(FIXTURE)
        target = self.temporary / "normalized-weather.json"

        original.save(target)
        reloaded = NormalizedWeatherSnapshot.load(target)

        self.assertEqual(reloaded, original)
        self.assertEqual(reloaded.to_dict(), original.to_dict())
        self.assertFalse(any(self.temporary.glob("*.tmp")))

    def test_rejects_unsupported_schema_version(self) -> None:
        self.raw["schema_version"] = 99
        with self.assertRaisesRegex(UnsupportedSnapshotVersion, "expected 1"):
            NormalizedWeatherSnapshot.from_dict(self.raw)

    def test_rejects_naive_timestamp_with_field_path(self) -> None:
        self.raw["current"]["observed_at"] = "2026-08-06T14:15:00"
        with self.assertRaisesRegex(
            SnapshotValidationError, "current.observed_at.*UTC offset"
        ):
            NormalizedWeatherSnapshot.from_dict(self.raw)

    def test_rejects_out_of_range_and_boolean_numbers(self) -> None:
        invalid_latitude = copy.deepcopy(self.raw)
        invalid_latitude["location"]["latitude"] = 91
        with self.assertRaisesRegex(SnapshotValidationError, "location.latitude"):
            NormalizedWeatherSnapshot.from_dict(invalid_latitude)

        self.raw["current"]["temperature"] = True
        with self.assertRaisesRegex(SnapshotValidationError, "current.temperature"):
            NormalizedWeatherSnapshot.from_dict(self.raw)

    def test_rejects_unknown_fields_and_unordered_hours(self) -> None:
        unknown = copy.deepcopy(self.raw)
        unknown["provider_payload"] = {}
        with self.assertRaisesRegex(SnapshotValidationError, "unknown field"):
            NormalizedWeatherSnapshot.from_dict(unknown)

        self.raw["hourly"].reverse()
        with self.assertRaisesRegex(SnapshotValidationError, "strictly ordered"):
            NormalizedWeatherSnapshot.from_dict(self.raw)

    def test_show_weather_validates_and_displays_snapshot(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        snapshot = NormalizedWeatherSnapshot.load(FIXTURE)
        snapshot.save(paths.latest_snapshot)
        output = io.StringIO()

        with redirect_stdout(output):
            result = main(["--home", str(self.temporary), "show-weather"])

        self.assertEqual(result, 0)
        displayed = json.loads(output.getvalue())
        self.assertEqual(displayed["location"]["display_name"], "Los Angeles, California")
        self.assertEqual(displayed["metadata"]["provider"], "open-meteo-fixture")

    def test_show_weather_rejects_malformed_cached_snapshot(self) -> None:
        paths = AppPaths.discover(self.temporary)
        initialize(paths)
        paths.latest_snapshot.write_text("{not-json", encoding="utf-8")
        errors = io.StringIO()

        with redirect_stderr(errors):
            result = main(["--home", str(self.temporary), "show-weather"])

        self.assertEqual(result, 2)
        self.assertIn("invalid JSON", errors.getvalue())
