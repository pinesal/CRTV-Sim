from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from threading import Event
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from retrotv.asset_paths import resolve_asset_path, is_external
from retrotv.db import utc_now
from retrotv.process_control import ProcessCancelled, run_cancellable


@dataclass(frozen=True)
class BreakpointResult:
    asset_id: int
    status: str
    offset_ms: int
    method: str
    confidence: float
    candidate_count: int


class BreakpointAnalyzer:
    """Find and record every intended act break across a program."""

    ANALYSIS_VERSION = 5

    def __init__(self, connection, config, *, cancel_event: Event | None = None):
        self.connection = connection
        self.config = config
        self.cancel_event = cancel_event
        settings = config.data.get("scheduling", {})
        self.minimum_duration_ms = round(
            float(settings.get("midroll_min_media_minutes", 15)) * 60_000
        )
        self.search_window_ms = round(
            float(settings.get("midroll_search_window_minutes", 4)) * 60_000
        )
        self.minimum_segment_ms = round(
            float(settings.get("midroll_min_segment_minutes", 5)) * 60_000
        )
        self.clock_slot_ms = max(
            60_000, round(float(settings.get("clock_slot_minutes", 30)) * 60_000)
        )

    def pending_assets(self, *, limit: int | None = None) -> list:
        timezone_name = str(self.config.data.get("application", {}).get("timezone", "UTC"))
        local_now = datetime.now(ZoneInfo(timezone_name))
        now = local_now.isoformat(timespec="milliseconds")
        earliest_date = (local_now.date() - timedelta(days=1)).isoformat()
        maximum = None if limit is None else max(0, int(limit))
        if maximum == 0:
            return []
        pending_where = """
            a.media_type IN ('episode','movie')
            AND a.enabled_for_scheduling=1 AND a.review_status='ready'
            AND a.metadata_status='ready'
            AND (a.source_root_id IN (SELECT id FROM storage_roots WHERE source_kind='managed') OR
                 a.source_root_id IN (SELECT s.root_id FROM external_sources s JOIN storage_roots r ON r.id=s.root_id WHERE r.enabled=1 AND s.availability='available'))
            AND a.integrity_status IN ('ready', 'not_scanned')
            AND m.duration_seconds > ?
            AND (b.asset_id IS NULL OR b.status IN ('pending', 'error') OR
                 b.source_signature != CAST(a.file_size AS TEXT) || ':' ||
                 CAST(a.modified_ns AS TEXT) || ':breaks-v5')
        """
        dates = self.connection.execute(
            """SELECT DISTINCT schedule_date FROM schedule_generations
               WHERE status='active' AND schedule_date>=? ORDER BY schedule_date""",
            (earliest_date,),
        ).fetchall()
        selected: list = []
        seen: set[int] = set()
        for date_row in dates:
            remaining = None if maximum is None else maximum - len(selected)
            if remaining == 0:
                break
            scheduled_limit = "" if remaining is None else "LIMIT ?"
            values: list[object] = [date_row["schedule_date"], now,
                                    self.minimum_duration_ms / 1000]
            if remaining is not None:
                values.append(remaining + len(seen))
            rows = self.connection.execute(
                f"""WITH scheduled AS (
                    SELECT se.asset_id, MIN(se.starts_at) next_air
                    FROM schedule_generations sg CROSS JOIN schedule_entries se
                    WHERE sg.status='active' AND sg.schedule_date=?
                      AND se.generation_id=sg.id AND se.is_program=1
                      AND se.asset_id IS NOT NULL AND se.ends_at>?
                    GROUP BY se.asset_id
                )
                SELECT a.id,a.source_relative_path,a.file_size,a.modified_ns,
                       m.duration_seconds
                FROM scheduled s CROSS JOIN assets a
                CROSS JOIN asset_media_info m
                LEFT JOIN asset_break_analyses b ON b.asset_id=a.id
                WHERE a.id=s.asset_id AND m.asset_id=a.id AND {pending_where}
                ORDER BY s.next_air,a.id {scheduled_limit}""",
                values,
            ).fetchall()
            for row in rows:
                if row["id"] not in seen:
                    selected.append(row)
                    seen.add(int(row["id"]))
                    if maximum is not None and len(selected) >= maximum:
                        return selected

        remaining = None if maximum is None else maximum - len(selected)
        fallback_limit = "" if remaining is None else "LIMIT ?"
        values = [self.minimum_duration_ms / 1000]
        if remaining is not None:
            values.append(remaining + len(seen))
        rows = self.connection.execute(
            f"""SELECT a.id,a.source_relative_path,a.file_size,a.modified_ns,
                       m.duration_seconds
                FROM assets a JOIN asset_media_info m ON m.asset_id=a.id
                LEFT JOIN asset_break_analyses b ON b.asset_id=a.id
                WHERE {pending_where} ORDER BY a.id {fallback_limit}""",
            values,
        ).fetchall()
        for row in rows:
            if row["id"] not in seen:
                selected.append(row)
                if maximum is not None and len(selected) >= maximum:
                    break
        return selected

    def analyze_pending(self, *, limit: int | None = None) -> list[BreakpointResult]:
        return [self.analyze_asset(int(row["id"])) for row in self.pending_assets(limit=limit)]

    def selected_offset(self, asset_id: int) -> int | None:
        offsets = self.selected_offsets(asset_id)
        return offsets[0] if offsets else None

    def selected_offsets(self, asset_id: int) -> list[int]:
        row = self.connection.execute(
            """
            SELECT b.source_signature, b.status, b.selected_offset_ms,
                   a.file_size, a.modified_ns, m.duration_seconds
            FROM asset_break_analyses b JOIN assets a ON a.id=b.asset_id
            JOIN asset_media_info m ON m.asset_id=a.id
            WHERE b.asset_id=? AND b.status IN ('analyzed', 'manual')
            """,
            (asset_id,),
        ).fetchone()
        if row is None:
            return []
        base_signature = f"{row['file_size']}:{row['modified_ns']}"
        signature = f"{base_signature}:breaks-v{self.ANALYSIS_VERSION}"
        if row["source_signature"] != signature:
            return []
        selected = self.connection.execute(
            """
            SELECT offset_ms,method FROM asset_break_candidates
            WHERE asset_id=? AND selected=1 ORDER BY offset_ms
            """,
            (asset_id,),
        ).fetchall()
        allow_fallback = str(self.config.data.get("scheduling", {}).get(
            "midroll_fallback", "midpoint")).casefold() == "midpoint"
        offsets = [int(item["offset_ms"]) for item in selected
                   if allow_fallback or item["method"] != "midpoint"]
        if not selected and row["selected_offset_ms"] is not None and (
            allow_fallback or row["status"] == "manual"
        ):
            offsets.append(int(row["selected_offset_ms"]))
        return offsets

    def target_offsets(self, duration_ms: int) -> list[int]:
        """Place targets about twelve content minutes apart across the whole file."""
        count = max(1, round(duration_ms / (12 * 60_000)) - 1)
        targets = [duration_ms * index // (count + 1) for index in range(1, count + 1)]
        if count == 3 and 40 * 60_000 <= duration_ms <= 50 * 60_000:
            # An hour package's middle break is preferably near the first
            # half-hour boundary after allowing for the opening ad pod.
            targets[1] = min(duration_ms - 2 * self.minimum_segment_ms,
                             self.clock_slot_ms - 5 * 60_000)
            targets[2] = (targets[1] + duration_ms) // 2
        return [
            target for target in targets
            if self.minimum_segment_ms <= target
            <= duration_ms - self.minimum_segment_ms
        ]

    def set_manual(self, asset_id: int, offset_ms: int, target_index: int = 1) -> BreakpointResult:
        row = self._asset(asset_id)
        duration_ms = round(float(row["duration_seconds"]) * 1000)
        self._validate_offset(offset_ms, duration_ms)
        targets = self.target_offsets(duration_ms)
        if not 1 <= target_index <= len(targets):
            raise ValueError("Unknown act-break target")
        for existing in self.connection.execute(
            "SELECT target_index,offset_ms FROM asset_break_overrides WHERE asset_id=? AND target_index!=?",
            (asset_id, target_index),
        ):
            if (existing["target_index"] < target_index and
                    offset_ms - existing["offset_ms"] < self.minimum_segment_ms) or (
                    existing["target_index"] > target_index and
                    existing["offset_ms"] - offset_ms < self.minimum_segment_ms):
                raise ValueError("Manual breaks must remain ordered and at least five minutes apart")
        self.connection.execute(
            """INSERT INTO asset_break_overrides(asset_id,target_index,offset_ms,updated_at)
               VALUES (?,?,?,?) ON CONFLICT(asset_id,target_index) DO UPDATE SET
               offset_ms=excluded.offset_ms,updated_at=excluded.updated_at""",
            (asset_id, target_index, offset_ms, utc_now()),
        )
        self.connection.commit()
        return self.analyze_asset(asset_id)

    def reset_manual(self, asset_id: int, target_index: int) -> BreakpointResult:
        self.connection.execute(
            "DELETE FROM asset_break_overrides WHERE asset_id=? AND target_index=?",
            (asset_id, target_index),
        )
        self.connection.commit()
        return self.analyze_asset(asset_id)

    def analyze_asset(self, asset_id: int) -> BreakpointResult:
        row = self._asset(asset_id)
        duration_ms = round(float(row["duration_seconds"]) * 1000)
        if row["media_type"] not in {"episode", "movie"} or duration_ms <= self.minimum_duration_ms:
            raise ValueError(f"Asset {asset_id} is not an eligible program over the threshold")
        signature = self._signature(row)
        targets = self.target_offsets(duration_ms)
        if not targets:
            raise ValueError(f"Asset {asset_id} has no safe act-break window")
        source = self._playback_path(row)
        chapters = self._chapter_starts(row, source)
        candidates: list[dict] = []
        selected_breaks: list[dict] = []
        errors: list[str] = []
        overrides = {int(item["target_index"]): int(item["offset_ms"])
                     for item in self.connection.execute(
                         "SELECT target_index,offset_ms FROM asset_break_overrides WHERE asset_id=?",
                         (asset_id,),
                     )}
        allow_fallback = str(self.config.data.get("scheduling", {}).get(
            "midroll_fallback", "midpoint")).casefold() == "midpoint"
        for index, target_ms in enumerate(targets, start=1):
            start_ms = max(
                self.minimum_segment_ms, target_ms - self.search_window_ms
            )
            end_ms = min(
                duration_ms - self.minimum_segment_ms,
                target_ms + self.search_window_ms,
            )
            if index in overrides:
                self._validate_offset(overrides[index], duration_ms)
                selected = {"offset_ms": overrides[index], "method": "manual",
                            "score": 1.0, "signals": {"manual": True}}
                window_candidates = [selected]
            else:
                try:
                    stderr = self._run_detection(source, start_ms, end_ms)
                    window_candidates = self._parse_candidates(
                        stderr, start_ms, target_ms, duration_ms
                    )
                    self._apply_chapter_hints(window_candidates, chapters, start_ms, end_ms)
                    window_candidates = [item for item in window_candidates
                                         if item["method"] == "fade" or
                                         item["signals"].get("silence") or
                                         item["signals"].get("chapter_supported")]
                    if not window_candidates and allow_fallback:
                        selected = {
                            "offset_ms": target_ms,
                            "method": "midpoint",
                            "score": 0.25,
                            "signals": {"fallback": True, "reason": "no natural cue"},
                        }
                        window_candidates = [selected]
                    elif window_candidates:
                        selected = max(window_candidates, key=lambda item: (
                            item["score"] + self._boundary_bonus(item["offset_ms"], duration_ms),
                            -abs(item["offset_ms"] - target_ms),
                        ))
                    else:
                        selected = None
                except ProcessCancelled:
                    raise
                except Exception as exc:
                    errors.append(f"window {index}: {exc}")
                    selected = None
                    window_candidates = []
            for candidate in window_candidates:
                candidate["signals"] = {
                    **candidate["signals"], "window": index, "target_ms": target_ms
                }
            candidates.extend(window_candidates)
            if selected is not None:
                if selected_breaks and selected["offset_ms"] - selected_breaks[-1]["offset_ms"] < self.minimum_segment_ms:
                    if selected["method"] == "manual" and selected_breaks[-1]["method"] != "manual":
                        selected_breaks.pop()
                    else:
                        continue
                selected_breaks.append(selected)

        confidence = min((float(item["score"]) for item in selected_breaks), default=0.0)
        methods = [str(item["method"]) for item in selected_breaks]
        method = (
            "midpoint"
            if methods and all(value == "midpoint" for value in methods)
            else methods[0] if len(methods) == 1 else "multiple" if methods else "none"
        )
        self._store(
            asset_id, signature, candidates, selected_breaks, confidence,
            "; ".join(errors) or None, status="error" if errors else
            "manual" if overrides else "analyzed",
        )
        return BreakpointResult(
            asset_id, "error" if errors else "manual" if overrides else "analyzed",
            int(selected_breaks[0]["offset_ms"]) if selected_breaks else 0, method,
            confidence, len(candidates)
        )

    def _boundary_bonus(self, offset_ms: int, duration_ms: int) -> float:
        # Clock boundaries are a preference, never a reason to invent a cut.
        slots = max(1, round(duration_ms / self.clock_slot_ms))
        if slots < 2:
            return 0.0
        boundaries = (duration_ms * index / slots for index in range(1, slots))
        distance = min(abs(offset_ms - boundary) for boundary in boundaries)
        return 0.06 * max(0.0, 1.0 - distance / self.search_window_ms)

    def _asset(self, asset_id: int):
        row = self.connection.execute(
            """
            SELECT a.*, m.duration_seconds, n.proxy_status, n.proxy_relative_path
            FROM assets a JOIN asset_media_info m ON m.asset_id=a.id
            LEFT JOIN normalization_profiles n ON n.asset_id=a.id
            WHERE a.id=?
            """,
            (asset_id,),
        ).fetchone()
        if row is None or row["duration_seconds"] is None:
            raise ValueError(f"Asset {asset_id} has no usable duration metadata")
        return row

    @staticmethod
    def _signature(row) -> str:
        return (
            f"{row['file_size']}:{row['modified_ns']}:"
            f"breaks-v{BreakpointAnalyzer.ANALYSIS_VERSION}"
        )

    def _playback_path(self, row) -> Path:
        if not is_external(self.connection, row["id"]) and row["proxy_status"] == "ready" and row["proxy_relative_path"]:
            proxy = self.config.storage_root("normalized") / Path(row["proxy_relative_path"])
            if proxy.is_file():
                return proxy
        source = resolve_asset_path(self.connection, self.config, row["id"])
        if not source.is_file():
            raise FileNotFoundError(source)
        return source

    def _run_detection(self, source: Path, start_ms: int, end_ms: int) -> str:
        duration = max(0.1, (end_ms - start_ms) / 1000)
        args = [
            self.config.ffmpeg, "-hide_banner", "-nostdin", "-ss", f"{start_ms / 1000:.3f}",
            "-t", f"{duration:.3f}", "-i", os.fspath(source),
            "-vf", "scale=160:-2,blackdetect=d=0.08:pic_th=0.98,select='gt(scene,0.28)',showinfo",
            "-af", "silencedetect=n=-35dB:d=0.15", "-f", "null", "-",
        ]
        if self.cancel_event is not None:
            result = run_cancellable(args, max(60.0, duration * 4), self.cancel_event)
        else:
            result = subprocess.run(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        if result.returncode:
            raise RuntimeError(result.stderr[-1000:] or "FFmpeg breakpoint analysis failed")
        return result.stderr

    def _chapter_starts(self, row, playback_path: Path) -> list[int]:
        # Probe the working source first: a proxy may have discarded chapters.
        source = resolve_asset_path(self.connection, self.config, row["id"])
        for path in (source, playback_path):
            if not path.is_file():
                continue
            try:
                result = subprocess.run(
                    [self.config.ffprobe, "-v", "error", "-show_chapters", "-of", "json", os.fspath(path)],
                    capture_output=True, text=True, timeout=30, check=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                chapters = json.loads(result.stdout).get("chapters", [])
                starts = sorted({round(float(item["start_time"]) * 1000)
                                 for item in chapters if float(item.get("start_time", 0)) > 0})
                if starts:
                    return starts
            except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
                continue
        return []

    @staticmethod
    def _apply_chapter_hints(
        candidates: list[dict], chapters: list[int], start_ms: int, end_ms: int
    ) -> None:
        # A chapter is evidence only when an independently detected transition
        # is close to it. Never create a candidate at the chapter alone.
        for item in candidates:
            nearby = [marker for marker in chapters
                      if start_ms <= marker <= end_ms
                      and abs(marker - item["offset_ms"]) <= 2500]
            if not nearby:
                continue
            quality = (item["method"] == "fade"
                       or bool(item["signals"].get("silence"))
                       or bool(item["signals"].get("scene_change")))
            if not quality:
                continue
            marker = min(nearby, key=lambda value: abs(value - item["offset_ms"]))
            item["signals"].update({"chapter_supported": True,
                                    "chapter_marker_ms": marker,
                                    "chapter_distance_ms": abs(marker - item["offset_ms"])})
            bonus = 0.12 if item["method"] == "fade" or item["signals"].get("silence") else 0.06
            item["score"] = min(1.0, item["score"] + bonus)

    def _parse_candidates(
        self, stderr: str, start_ms: int, midpoint_ms: int, duration_ms: int
    ) -> list[dict]:
        silence_ranges = []
        starts = [float(value) for value in re.findall(r"silence_start:\s*([0-9.]+)", stderr)]
        ends = [float(value) for value in re.findall(r"silence_end:\s*([0-9.]+)", stderr)]
        for index, value in enumerate(starts):
            silence_ranges.append((value, ends[index] if index < len(ends) else value + 0.2))
        raw: list[tuple[float, str, dict]] = []
        for match in re.finditer(
            r"black_start:([0-9.]+)\s+black_end:([0-9.]+)\s+black_duration:([0-9.]+)", stderr
        ):
            left, right, length = map(float, match.groups())
            point = (left + right) / 2
            silent = any(a - 0.25 <= point <= b + 0.25 for a, b in silence_ranges)
            raw.append((point, "fade", {"black_duration": length, "silence": silent}))
        for value in re.findall(r"pts_time:([0-9.]+)", stderr):
            raw.append((float(value), "scene", {"scene_change": True}))
        by_second: dict[int, dict] = {}
        for relative, method, signals in raw:
            offset_ms = start_ms + round(relative * 1000)
            if not (self.minimum_segment_ms <= offset_ms <= duration_ms - self.minimum_segment_ms):
                continue
            distance = abs(offset_ms - midpoint_ms)
            proximity = max(0.0, 1.0 - distance / max(1, self.search_window_ms))
            score = 0.35 * proximity
            if method == "fade":
                score += 0.45
                if signals.get("silence"):
                    score += 0.20
            else:
                score += 0.25
                signals["silence"] = any(a - 0.5 <= relative <= b + 0.5
                                         for a, b in silence_ranges)
            key = round(offset_ms / 1000)
            item = {
                "offset_ms": offset_ms, "method": method,
                "score": min(1.0, score), "signals": signals,
            }
            if key not in by_second or item["score"] > by_second[key]["score"]:
                by_second[key] = item
        return sorted(by_second.values(), key=lambda item: item["offset_ms"])

    def _store(self, asset_id, signature, candidates, selected, confidence, detail, *, status="analyzed"):
        selected_items = selected if isinstance(selected, list) else [selected]
        now = utc_now()
        self.connection.execute("DELETE FROM asset_break_candidates WHERE asset_id=?", (asset_id,))
        for candidate in candidates:
            self.connection.execute(
                """
                INSERT INTO asset_break_candidates(
                    asset_id, offset_ms, method, score, signals_json, selected, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id, int(candidate["offset_ms"]), candidate["method"],
                    float(candidate["score"]), json.dumps(candidate["signals"], sort_keys=True),
                    int(any(candidate is item for item in selected_items)), now,
                ),
            )
        self.connection.execute(
            """
            INSERT INTO asset_break_analyses(
                asset_id, source_signature, status, selected_offset_ms,
                selected_method, confidence, detail, analyzed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                source_signature=excluded.source_signature, status=excluded.status,
                selected_offset_ms=excluded.selected_offset_ms,
                selected_method=excluded.selected_method,
                confidence=excluded.confidence, detail=excluded.detail,
                analyzed_at=excluded.analyzed_at
            """,
            (
                asset_id, signature, status,
                int(selected_items[0]["offset_ms"]) if selected_items else None,
                (
                    "midpoint" if selected_items and all(item["method"] == "midpoint" for item in selected_items)
                    else selected_items[0]["method"]
                    if len(selected_items) == 1 else "multiple" if selected_items else None
                ),
                confidence, detail, now,
            ),
        )
        self.connection.commit()

    def _validate_offset(self, offset_ms: int, duration_ms: int) -> None:
        if not self.minimum_segment_ms <= offset_ms <= duration_ms - self.minimum_segment_ms:
            raise ValueError(
                "Breakpoint must leave at least "
                f"{self.minimum_segment_ms / 60000:g} minutes on each side"
            )
