"""Allocate recurring series across the dayparts covered by their folders.

The scanner expands ``daytime`` and ``all-day`` eligibility for ordinary
scheduling. Recurring lineups instead need to remember the folder that granted
that eligibility so one broad placement does not claim every daypart.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScopePlacement:
    series: str
    scope: str
    dayparts: frozenset[str]

    @property
    def broad(self) -> bool:
        return self.scope in {"daytime", "all-day"}


def _tie_key(template: str, daypart: str, series: str) -> str:
    return hashlib.sha256(f"{template}:{daypart}:{series}".encode()).hexdigest()


def allocate_recurring_scopes(
    placements: list[ScopePlacement],
    clock_slots: dict[str, set[str]],
    previous_slots: dict[str, dict[str, str]],
    previous_queues: dict[str, list[dict[str, Any]]],
    progress: dict[str, object],
    eligible_assets: dict[str, set[int]],
    template: str,
    schedule_date: str,
    other_template_series: set[str] | None = None,
    *,
    fill_repeats: bool = True,
    slot_spans: dict[str, int] | None = None,
    clock_slot_minutes: int = 30,
    reset_completed_runs: bool = True,
) -> tuple[dict[str, dict[str, str]], dict[str, list[dict[str, object]]]]:
    """Place each folder scope once, then use balanced repeats for shortages.

    Every exact daypart folder is a separate placement. A daytime or all-day
    folder may claim one compatible daypart. A second folder for the same show
    may claim another, but never a second primary position in the same daypart.
    """
    available = {
        daypart: sorted(keys, key=int)
        for daypart, keys in clock_slots.items()
    }
    other_template_series = other_template_series or set()
    plan: dict[str, dict[str, str]] = {daypart: {} for daypart in available}
    queue_order: dict[tuple[str, str], int] = {}
    for queue in previous_queues.values():
        for entry in queue:
            key = (str(entry.get("series", "")), str(entry.get("scope", "")))
            queue_order.setdefault(key, len(queue_order))

    def is_complete(series: str) -> bool:
        row = progress.get(series)
        assets = eligible_assets.get(series, set())
        return bool(
            assets and isinstance(row, dict)
            and assets.issubset({int(value) for value in row.get("seen_asset_ids", [])})
        )

    def had_slot(placement: ScopePlacement) -> bool:
        return any(
            placement.series in previous_slots.get(daypart, {}).values()
            for daypart in placement.dayparts
        )

    # Keep unfinished incumbents before admitting waiting shows. Completed
    # series go last so a waiting show can take their slot; if room remains,
    # they immediately return for a rerun.
    ordered = sorted(
        placements,
        key=lambda item: (
            is_complete(item.series),
            item.broad,
            not had_slot(item),
            item.series in other_template_series,
            len(item.dayparts),
            queue_order.get((item.series, item.scope), 1_000_000),
            item.series.casefold(),
            item.scope,
        ),
    )
    primary: set[tuple[str, str]] = set()
    occupied: dict[str, set[str]] = {part: set() for part in available}
    unplaced: list[ScopePlacement] = []
    for placement in ordered:
        choices: list[tuple[int, int, int, str]] = []
        for daypart in placement.dayparts:
            if daypart not in available or (placement.series, daypart) in primary:
                continue
            span = (slot_spans or {}).get(placement.series, 1)
            free = [key for key in available[daypart] if all(
                str(int(key) + index * clock_slot_minutes) in available[daypart]
                and str(int(key) + index * clock_slot_minutes) not in occupied[daypart]
                for index in range(span)
            )]
            if not free:
                continue
            incumbent = int(any(
                previous_slots.get(daypart, {}).get(key) == placement.series
                for key in free
            ))
            choices.append((incumbent, len(free), -len(plan[daypart]), daypart))
        if not choices:
            unplaced.append(placement)
            continue
        daypart = max(choices)[-1]
        span = (slot_spans or {}).get(placement.series, 1)
        free = [key for key in available[daypart] if all(
            str(int(key) + index * clock_slot_minutes) in available[daypart]
            and str(int(key) + index * clock_slot_minutes) not in occupied[daypart]
            for index in range(span)
        )]
        same = [
            key for key in free
            if previous_slots.get(daypart, {}).get(key) == placement.series
        ]
        empty = [key for key in free if key not in previous_slots.get(daypart, {})]
        key = (same or empty or free)[0]
        plan[daypart][key] = placement.series
        occupied[daypart].update(str(int(key) + index * clock_slot_minutes) for index in range(span))
        primary.add((placement.series, daypart))

    eligible: dict[str, set[str]] = {daypart: set() for daypart in available}
    for placement in placements:
        for daypart in placement.dayparts:
            if daypart in eligible:
                eligible[daypart].add(placement.series)
    counts = Counter(series for assignments in plan.values() for series in assignments.values())
    for daypart in available:
        for key in available[daypart]:
            if not fill_repeats or key in occupied[daypart] or not eligible[daypart]:
                continue
            series = min(
                eligible[daypart],
                key=lambda name: (
                    counts[name],
                    list(plan[daypart].values()).count(name),
                    _tie_key(template, daypart, name),
                ),
            )
            plan[daypart][key] = series
            counts[series] += 1

    waiting: dict[str, list[dict[str, object]]] = {part: [] for part in available}
    queued_series: set[str] = set()
    for placement in unplaced:
        if placement.series in queued_series:
            continue
        choices = [part for part in placement.dayparts if part in waiting]
        if not choices:
            continue
        daypart = min(choices, key=lambda part: (len(waiting[part]), part))
        old_entry = next(
            (
                entry for entry in previous_queues.get(daypart, [])
                if entry.get("series") == placement.series
                and entry.get("scope") == placement.scope
            ),
            None,
        )
        waiting[daypart].append({
            "series": placement.series,
            "scope": placement.scope,
            "enqueued_at": (
                str(old_entry.get("enqueued_at")) if old_entry else schedule_date
            ),
        })
        queued_series.add(placement.series)

    # A completed show with an assigned rerun slot must start a new episode
    # run. A completed show still waiting keeps its progress until promoted.
    assigned = {series for assignments in plan.values() for series in assignments.values()}
    for series in assigned:
        if reset_completed_runs and is_complete(series):
            row = progress.get(series)
            if isinstance(row, dict):
                row["seen_asset_ids"] = []
    return plan, waiting
