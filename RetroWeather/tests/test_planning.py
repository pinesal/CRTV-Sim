from __future__ import annotations

import copy
import json
from pathlib import Path

from _support import WorkspaceTestCase
from retroweather.config import DEFAULT_SETTINGS
from retroweather.models import NormalizedWeatherSnapshot
from retroweather.planning import PlanError, build_forecast_plan


FIXTURE = Path(__file__).parent / "fixtures" / "normalized-weather.json"


class SegmentPlanningTests(WorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.snapshot = NormalizedWeatherSnapshot.from_dict(
            json.loads(FIXTURE.read_text(encoding="utf-8"))
        )

    def test_default_plan_is_contiguous_and_exactly_two_minutes(self) -> None:
        plan = build_forecast_plan(self.snapshot, DEFAULT_SETTINGS)

        self.assertEqual(plan.duration_seconds, 120)
        self.assertEqual(len(plan.segments), 7)
        self.assertEqual(plan.segments[0].start_seconds, 0)
        self.assertEqual(plan.segments[-1].end_seconds, 120)
        self.assertEqual(
            [segment.start_seconds for segment in plan.segments],
            [0, 5, 17.5, 37.5, 62.5, 80, 107.5],
        )
        self.assertEqual(plan.segments[3].title, "Hourly Outlook")

    def test_legacy_settings_without_segments_use_defaults(self) -> None:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["channel"].pop("segments")

        plan = build_forecast_plan(self.snapshot, settings)

        self.assertEqual(plan.segments[-1].end_seconds, 120)

    def test_duration_mismatch_is_rejected(self) -> None:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["channel"]["duration_seconds"] = 119

        with self.assertRaisesRegex(PlanError, "timeline is 120.0 seconds"):
            build_forecast_plan(self.snapshot, settings)

    def test_duplicate_segment_ids_are_rejected(self) -> None:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["channel"]["segments"][1]["id"] = "station-id"

        with self.assertRaisesRegex(PlanError, "Duplicate segment id"):
            build_forecast_plan(self.snapshot, settings)
