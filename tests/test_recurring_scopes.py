import unittest
from collections import Counter

from retrotv.recurring_scopes import ScopePlacement, allocate_recurring_scopes


class RecurringScopeAllocationTests(unittest.TestCase):
    def allocate(self, placements, slots, previous=None, queues=None, progress=None,
                 other_template_series=None):
        return allocate_recurring_scopes(
            placements, slots, previous or {}, queues or {}, progress or {},
            {item.series: {index + 1} for index, item in enumerate(placements)},
            "weekday", "2026-09-29", other_template_series,
        )

    def test_daytime_uses_each_series_once_when_pool_fills_slots(self):
        placements = [
            ScopePlacement(f"Show {index}", "daytime",
                           frozenset({"morning", "afternoon", "evening"}))
            for index in range(6)
        ]
        plan, waiting = self.allocate(
            placements,
            {part: {"0", "30"} for part in ("morning", "afternoon", "evening")},
        )
        names = [series for assignments in plan.values() for series in assignments.values()]
        self.assertEqual(len(names), 6)
        self.assertEqual(len(set(names)), 6)
        self.assertFalse(any(waiting.values()))

    def test_all_day_shortage_balances_repeats_across_custom_daypart(self):
        placements = [
            ScopePlacement(name, "all-day", frozenset({"morning", "over-night"}))
            for name in ("Alpha", "Beta")
        ]
        plan, waiting = self.allocate(
            placements,
            {"morning": {"360", "390", "420"},
             "over-night": {"0", "30", "60"}},
        )
        counts = Counter(name for part in plan.values() for name in part.values())
        self.assertEqual(sum(counts.values()), 6)
        self.assertEqual(set(counts), {"Alpha", "Beta"})
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)
        self.assertFalse(any(waiting.values()))

    def test_explicit_folder_gets_its_own_daypart_without_double_claim(self):
        placements = [
            ScopePlacement("Shared Show", "evening", frozenset({"evening"})),
            ScopePlacement("Shared Show", "daytime",
                           frozenset({"morning", "afternoon", "evening"})),
            ScopePlacement("Morning Show", "morning", frozenset({"morning"})),
            ScopePlacement("Afternoon Show", "afternoon", frozenset({"afternoon"})),
        ]
        plan, _waiting = self.allocate(
            placements,
            {"morning": {"360", "390"},
             "afternoon": {"720"}, "evening": {"1020"}},
        )
        self.assertEqual(list(plan["evening"].values()), ["Shared Show"])
        self.assertEqual(
            sum(name == "Shared Show" for part in plan.values() for name in part.values()),
            2,
        )

    def test_new_exact_placement_takes_priority_over_broad_incumbent(self):
        broad = ScopePlacement("Broad Show", "daytime",
                               frozenset({"morning", "afternoon"}))
        exact = ScopePlacement("Morning Show", "morning", frozenset({"morning"}))
        plan, waiting = self.allocate(
            [broad, exact], {"morning": {"360"}, "afternoon": {"720"}},
            {"morning": {"360": "Broad Show"}},
        )
        self.assertEqual(plan["morning"], {"360": "Morning Show"})
        self.assertEqual(plan["afternoon"], {"720": "Broad Show"})
        self.assertFalse(any(waiting.values()))

    def test_overflow_waits_and_completed_show_yields_its_slot(self):
        placements = [
            ScopePlacement(name, "morning", frozenset({"morning"}))
            for name in ("Alpha", "Beta", "Gamma")
        ]
        first, waiting = self.allocate(placements, {"morning": {"360", "390"}})
        self.assertEqual(len(waiting["morning"]), 1)
        completed = next(iter(first["morning"].values()))
        progress = {completed: {"last_asset_id": 1, "seen_asset_ids": [
            next(index + 1 for index, item in enumerate(placements)
                 if item.series == completed)
        ]}}
        second, next_waiting = self.allocate(
            placements, {"morning": {"360", "390"}}, first, waiting, progress,
        )
        self.assertIn(waiting["morning"][0]["series"], second["morning"].values())
        self.assertIn(completed, [item["series"] for item in next_waiting["morning"]])

    def test_new_shows_replace_shortage_repeats(self):
        coverage = frozenset({"morning", "afternoon"})
        initial = [
            ScopePlacement(name, "daytime", coverage) for name in ("Alpha", "Beta")
        ]
        slots = {"morning": {"360", "390"}, "afternoon": {"720", "750"}}
        old, _ = self.allocate(initial, slots)
        self.assertEqual(len({name for part in old.values() for name in part.values()}), 2)
        expanded = initial + [
            ScopePlacement(name, "daytime", coverage) for name in ("Gamma", "Delta")
        ]
        new, waiting = self.allocate(expanded, slots, old)
        self.assertEqual(
            Counter(name for part in new.values() for name in part.values()),
            {"Alpha": 1, "Beta": 1, "Gamma": 1, "Delta": 1},
        )
        self.assertFalse(any(waiting.values()))

    def test_weekend_uses_unplaced_weekday_shows_before_repeating_lineup(self):
        placements = [
            ScopePlacement(name, "daytime", frozenset({"morning", "afternoon"}))
            for name in ("Alpha", "Beta", "Gamma", "Delta")
        ]
        slots = {"morning": {"360"}, "afternoon": {"720"}}
        weekday, _ = self.allocate(placements, slots)
        weekday_names = {name for part in weekday.values() for name in part.values()}
        saturday, _ = self.allocate(
            placements, slots, other_template_series=weekday_names,
        )
        saturday_names = {name for part in saturday.values() for name in part.values()}
        self.assertFalse(weekday_names.intersection(saturday_names))
        self.assertEqual(len(saturday_names), 2)

    def test_broad_overflow_appears_once_in_waiting(self):
        placements = [
            ScopePlacement(f"Show {index}", "daytime",
                           frozenset({"morning", "afternoon"}))
            for index in range(5)
        ]
        plan, waiting = self.allocate(
            placements, {"morning": {"360"}, "afternoon": {"720"}}
        )
        aired = {name for part in plan.values() for name in part.values()}
        queued = [entry["series"] for part in waiting.values() for entry in part]
        self.assertEqual(len(aired), 2)
        self.assertEqual(len(queued), 3)
        self.assertEqual(len(set(queued)), 3)
        self.assertFalse(aired.intersection(queued))

    def test_multiple_unplaced_scopes_show_one_waiting_row_per_series(self):
        placements = [
            ScopePlacement("Alpha", "morning", frozenset({"morning"})),
            ScopePlacement("Alpha", "daytime", frozenset({"morning", "afternoon"})),
        ]
        _plan, waiting = self.allocate(placements, {"morning": set(), "afternoon": set()})
        self.assertEqual(
            [row["series"] for rows in waiting.values() for row in rows],
            ["Alpha"],
        )


if __name__ == "__main__":
    unittest.main()
