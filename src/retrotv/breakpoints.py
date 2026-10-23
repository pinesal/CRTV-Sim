from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from threading import Event
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from retrotv.asset_paths import resolve_asset_path, is_external
from retrotv.db import utc_now
from retrotv.process_control import ProcessCancelled, run_cancellable
from retrotv.program_timing import normal_package_ms, break_targets
from retrotv.break_selection import match_manual, select_breaks


def resolve_breakpoint_media(connection, config, asset_id: int) -> Path:
    """Resolve the same working representation for analysis and its workshop."""
    row = connection.execute(
        "SELECT proxy_status,proxy_relative_path FROM normalization_profiles WHERE asset_id=?",
        (asset_id,),
    ).fetchone()
    if not is_external(connection, asset_id) and row is not None and row["proxy_status"] == "ready" and row["proxy_relative_path"]:
        root = config.storage_root("normalized").resolve()
        proxy = (root / row["proxy_relative_path"]).resolve()
        if not proxy.is_relative_to(root):
            raise ValueError("Working video path escapes its storage root")
        if proxy.is_file():
            return proxy
    source = resolve_asset_path(connection, config, asset_id)
    if not source.is_file():
        raise FileNotFoundError(source)
    return source


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

    ANALYSIS_VERSION = 8
    CHUNK_MS = 480_000
    CHUNK_OVERLAP_MS = 16_000
    NOISY_BLACK_PIXEL_THRESHOLD = 0.15
    NOISY_BLACK_PICTURE_THRESHOLD = 0.95
    BLACK_MIN_SECONDS = 0.08
    BLACK_CONTRAST = 12
    FALLBACK_METHODS = frozenset({"midpoint", "quiet_pause", "scene_fallback"})
    QUIET_PAUSE_MIN_MS = 1500
    STILL_CARD_MIN_MS = 2000
    STILL_CARD_MAX_MS = 15000
    STILL_NOISE_TOLERANCE = 0.003
    STILL_TRANSITION_TOLERANCE_MS = 500
    STILL_PAIR_GAP_MS = 1000
    SILENCE_THRESHOLD_DB = -35

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
        self.clock_slot_ms = max(15, int(settings.get('clock_slot_minutes', 30))) * 60_000
        short_target_ms = max(1, round(float(settings.get('short_program_target_minutes',
            settings.get('short_episode_max_minutes', 20))) * 60_000))
        self.short_slot_ms = max(short_target_ms, round(float(settings.get(
            'short_program_slot_minutes', settings.get('pairing_slot_minutes',
                                                       settings.get('clock_slot_minutes', 30)))) * 60_000))
        self.policy_key = hashlib.sha256(json.dumps([
            self.clock_slot_ms, self.short_slot_ms, self.minimum_duration_ms,
            self.search_window_ms, self.minimum_segment_ms,
            str(settings.get('midroll_fallback', 'midpoint')).casefold(),
        ]).encode()).hexdigest()[:20]

    def pending_assets(self, *, limit: int | None = None) -> list:
        timezone_name = str(self.config.data.get("application", {}).get("timezone", "UTC"))
        local_now = datetime.now(ZoneInfo(timezone_name))
        now = local_now.isoformat(timespec="milliseconds")
        earliest_date = (local_now.date() - timedelta(days=1)).isoformat()
        maximum = None if limit is None else max(0, int(limit))
        if maximum == 0:
            return []
        pending_where = f"""
            a.media_type IN ('episode','movie')
            AND a.enabled_for_scheduling=1 AND a.review_status='ready'
            AND a.metadata_status='ready'
            AND (a.source_root_id IN (SELECT id FROM storage_roots WHERE source_kind='managed') OR
                 a.source_root_id IN (SELECT s.root_id FROM external_sources s JOIN storage_roots r ON r.id=s.root_id WHERE r.enabled=1 AND s.availability='available'))
            AND a.integrity_status IN ('ready', 'not_scanned')
            AND m.duration_seconds > ?
            AND (b.asset_id IS NULL OR b.status IN ('pending', 'error') OR b.refresh_error IS NOT NULL OR
                 b.source_signature != CAST(a.file_size AS TEXT) || ':' ||
                 CAST(a.modified_ns AS TEXT) || ':breaks-v{self.ANALYSIS_VERSION}:{self.policy_key}')
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
            SELECT b.source_signature, b.status, b.selected_offset_ms,b.selected_method,
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
        # Recorded layouts/cuts stay usable while their policy is refreshed.
        if not re.fullmatch(re.escape(base_signature) + r':breaks-v[5678](?::[0-9a-f]+)?', row['source_signature']):
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
                   if allow_fallback or item["method"] not in self.FALLBACK_METHODS]
        if not selected and row["selected_offset_ms"] is not None and (
            allow_fallback or row["selected_method"] not in self.FALLBACK_METHODS
        ):
            offsets.append(int(row["selected_offset_ms"]))
        safe_offsets = []
        duration_ms = round(float(row["duration_seconds"]) * 1000)
        for offset in sorted(offsets):
            if (self.minimum_segment_ms <= offset <= duration_ms - self.minimum_segment_ms
                    and (not safe_offsets or offset - safe_offsets[-1] >= self.minimum_segment_ms)):
                safe_offsets.append(offset)
        return safe_offsets

    def normal_package_ms(self, duration_ms: int) -> int:
        return normal_package_ms(duration_ms, self.clock_slot_ms, self.short_slot_ms)

    def target_offsets(self, duration_ms: int) -> list[int]:
        return [item['target_ms'] for item in break_targets(
            duration_ms, self.normal_package_ms(duration_ms), self.minimum_segment_ms)]

    def _manuals(self, asset_id):
        return {int(row['target_index']): int(row['offset_ms']) for row in self.connection.execute(
            'SELECT target_index,offset_ms FROM asset_break_overrides WHERE asset_id=?', (asset_id,))}

    def _new_layout(self, duration_ms, overrides, *, remap=True):
        targets = break_targets(duration_ms, self.normal_package_ms(duration_ms), self.minimum_segment_ms)
        intended_count = len(targets)
        overflow = len(overrides) > intended_count
        if overflow:
            overrides = {index: point for index, point in enumerate(sorted(overrides.values()), 1)}
            targets = [{'index': index, 'target_ms': point, 'role': 'legacy_manual'}
                       for index, point in overrides.items()]
        elif remap:
            overrides = match_manual(targets, overrides)
        for item in targets:
            item['manual'] = item['index'] in overrides
            if item['manual']:
                item['manual_offset_ms'] = overrides[item['index']]
        layout = {'version': self.ANALYSIS_VERSION, 'policy_key': self.policy_key,
                  'normal_package_ms': self.normal_package_ms(duration_ms),
                  'intended_count': intended_count, 'manual_overflow': overflow, 'targets': targets}
        layout['identity'] = hashlib.sha256(json.dumps(layout, sort_keys=True).encode()).hexdigest()
        return layout, overrides

    def recorded_layout(self, asset_id):
        row = self._asset(asset_id)
        record = self.connection.execute('SELECT * FROM asset_break_analyses WHERE asset_id=?', (asset_id,)).fetchone()
        if record is not None and record['target_layout_json']:
            return json.loads(record['target_layout_json'])
        duration_ms = round(row['duration_seconds'] * 1000)
        overrides = self._manuals(asset_id)
        if record is None or f':breaks-v{self.ANALYSIS_VERSION}:' in record['source_signature']:
            return self._new_layout(duration_ms, overrides)[0]
        # Reconstruct old target numbering without changing records on a GET.
        count = max(1, round(duration_ms / 720_000) - 1)
        points = [duration_ms * index // (count + 1) for index in range(1, count + 1)]
        if count == 3 and 2_400_000 <= duration_ms <= 3_000_000:
            points[1] = min(duration_ms - 2 * self.minimum_segment_ms, self.clock_slot_ms - 300_000)
            points[2] = (points[1] + duration_ms) // 2
        evidence = self.connection.execute('SELECT signals_json FROM asset_break_candidates WHERE asset_id=?', (asset_id,)).fetchall()
        for entry in evidence:
            signals = json.loads(entry[0])
            index = signals.get('window', 0)
            if 1 <= index <= len(points) and 'target_ms' in signals:
                points[index - 1] = signals['target_ms']
        targets = [{'index': index, 'target_ms': point, 'role': 'legacy', 'manual': index in overrides}
                   for index, point in enumerate(points, 1)]
        layout = {'version': 7, 'policy_key': None, 'normal_package_ms': self.normal_package_ms(duration_ms),
                  'intended_count': len(targets), 'manual_overflow': False, 'targets': targets}
        layout['identity'] = hashlib.sha256(json.dumps([layout, overrides], sort_keys=True).encode()).hexdigest()
        return layout

    def set_manual(self, asset_id: int, offset_ms: int, target_index: int = 1) -> BreakpointResult:
        row = self._asset(asset_id)
        self._validate_offset(offset_ms, round(row['duration_seconds'] * 1000))
        if target_index not in {item['index'] for item in self.recorded_layout(asset_id)['targets']}:
            raise ValueError('Unknown act-break target')
        overrides = self._manuals(asset_id)
        overrides[target_index] = offset_ms
        self._validate_manuals(overrides, round(row['duration_seconds'] * 1000))
        result = self.analyze_asset(asset_id, _override_edits=overrides)
        if result.status == 'error':
            raise ValueError('Act-break refresh failed; manual change was not saved')
        return result

    def reset_manual(self, asset_id: int, target_index: int) -> BreakpointResult:
        overrides = self._manuals(asset_id)
        overrides.pop(target_index, None)
        result = self.analyze_asset(asset_id, _override_edits=overrides)
        if result.status == 'error':
            raise ValueError('Act-break refresh failed; reset was not saved')
        return result

    def _validate_manuals(self, overrides, duration_ms):
        previous = None
        for index, point in sorted(overrides.items()):
            self._validate_offset(point, duration_ms)
            if previous is not None and point - previous < self.minimum_segment_ms:
                raise ValueError('Manual breaks must remain ordered and respect the minimum segment length')
            previous = point

    def _collect_candidates(self, source, duration_ms, chapters):
        candidates = []
        # Context halos preserve complete holds/fades crossing core boundaries.
        for core in range(self.minimum_segment_ms, duration_ms - self.minimum_segment_ms, self.CHUNK_MS):
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise ProcessCancelled('Act-break analysis cancelled')
            start = max(0, core - self.CHUNK_OVERLAP_MS)
            end = min(duration_ms, core + self.CHUNK_MS + self.CHUNK_OVERLAP_MS)
            log = self._run_detection(source, start, end)
            raw = self._parse_candidates(log, start, core, duration_ms)
            self._apply_chapter_hints(raw, chapters, start, end)
            for item in raw:
                if item['method'] == 'scene' and not (item['signals'].get('silence') or item['signals'].get('chapter_supported')):
                    item['method'] = 'scene_fallback'
                    item['score'] = .30
                    item['signals'].update(fallback=True, reason='scene transition without silence or chapter support')
            raw.extend(self._still_candidates(log, start, end, duration_ms))
            raw.extend(self._quiet_candidates(log, start, end, duration_ms))
            candidates.extend(raw)
        merged = {}
        for item in sorted(candidates, key=lambda c: (c['offset_ms'], c['method'])):
            signals = item['signals']
            # Overlap halos see identical source frames; merge by interval identity.
            key = (item['method'], signals.get('black_start_ms', item['offset_ms']),
                   signals.get('black_end_ms', item['offset_ms']))
            if key not in merged or item['score'] > merged[key]['score']:
                merged[key] = item
        return list(merged.values())

    def analyze_asset(self, asset_id: int, *, _override_edits=None) -> BreakpointResult:
        row = self._asset(asset_id)
        duration_ms = round(float(row['duration_seconds']) * 1000)
        if row['media_type'] not in {'episode', 'movie'} or duration_ms <= self.minimum_duration_ms:
            raise ValueError(f'Asset {asset_id} is not an eligible program over the threshold')
        prior = self.connection.execute('SELECT * FROM asset_break_analyses WHERE asset_id=?', (asset_id,)).fetchone()
        saved_manuals = self._manuals(asset_id)
        overrides = saved_manuals if _override_edits is None else _override_edits
        self._validate_manuals(overrides, duration_ms)
        prior_layout = json.loads(prior['target_layout_json']) if prior is not None and prior['target_layout_json'] else None
        remap = (prior_layout is None or prior_layout['policy_key'] != self.policy_key
                 or prior_layout['manual_overflow'] or prior_layout['normal_package_ms'] != self.normal_package_ms(duration_ms))
        if prior is None and _override_edits is not None:
            # A first workshop edit already addresses the new layout.
            remap = False
        layout, overrides = self._new_layout(duration_ms, overrides, remap=remap)
        if not layout['targets']:
            raise ValueError(f'Asset {asset_id} has no safe act-break window')
        try:
            if len(overrides) == len(layout['targets']):
                candidates = []
            else:
                source = self._playback_path(row)
                candidates = self._collect_candidates(source, duration_ms, self._chapter_starts(row, source))
            selected, candidates = select_breaks(layout['targets'], candidates, overrides,
                self.minimum_segment_ms, self.search_window_ms,
                str(self.config.data.get('scheduling', {}).get('midroll_fallback', 'midpoint')).casefold() == 'midpoint', duration_ms)
        except ProcessCancelled:
            raise
        except Exception as exc:
            detail = str(exc)
            if prior is not None and self.selected_offsets(asset_id):
                self.connection.execute('UPDATE asset_break_analyses SET refresh_error=? WHERE asset_id=?', (detail, asset_id))
            else:
                self.connection.execute("""INSERT INTO asset_break_analyses(asset_id,source_signature,status,detail,refresh_error)
                    VALUES (?,?,'error',?,?) ON CONFLICT(asset_id) DO UPDATE SET status='error',detail=excluded.detail,
                    refresh_error=excluded.refresh_error""", (asset_id, self._signature(row), detail, detail))
            self.connection.commit()
            return BreakpointResult(asset_id, 'error', 0, 'none', 0.0, 0)
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise ProcessCancelled('Act-break analysis cancelled before publication')
        self._store(asset_id, self._signature(row), candidates, selected,
                    min((item['score'] for item in selected), default=0.0), None,
                    status='manual' if overrides else 'analyzed', layout=layout, overrides=overrides,
                    expected_layout=prior['target_layout_json'] if prior is not None else None,
                    expected_manuals=saved_manuals)
        methods = [item['method'] for item in selected]
        method = methods[0] if methods and len(set(methods)) == 1 else 'multiple' if methods else 'none'
        return BreakpointResult(asset_id, 'manual' if overrides else 'analyzed',
            selected[0]['offset_ms'] if selected else 0, method,
            min((item['score'] for item in selected), default=0.0), len(candidates))

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

    def _signature(self, row) -> str:
        return (
            f"{row['file_size']}:{row['modified_ns']}:"
            f"breaks-v{self.ANALYSIS_VERSION}:{self.policy_key}"
        )

    def _playback_path(self, row) -> Path:
        return resolve_breakpoint_media(self.connection, self.config, int(row["id"]))

    def _run_detection(self, source: Path, start_ms: int, end_ms: int) -> str:
        duration = max(0.1, (end_ms - start_ms) / 1000)
        args = [
            self.config.ffmpeg, "-hide_banner", "-nostdin", "-ss", f"{start_ms / 1000:.3f}",
            "-t", f"{duration:.3f}", "-i", os.fspath(source),
            "-vf", (f"scale=160:-2,freezedetect=n={self.STILL_NOISE_TOLERANCE}:d={self.STILL_CARD_MIN_MS/1000:g},"
                    "blackdetect@strict=d=0.08:pic_th=0.98,"
                    "metadata=print:key=lavfi.black_start:file=-,metadata=print:key=lavfi.black_end:file=-,"
                    "metadata=delete:key=lavfi.black_start,metadata=delete:key=lavfi.black_end,"
                    f"blackdetect@noisy=d={self.BLACK_MIN_SECONDS}:pix_th={self.NOISY_BLACK_PIXEL_THRESHOLD}:pic_th={self.NOISY_BLACK_PICTURE_THRESHOLD},"
                    "metadata=print:key=lavfi.black_start,metadata=print:key=lavfi.black_end,"
                    "signalstats,metadata@luma=print:key=lavfi.signalstats.YAVG,select='gt(scene,0.28)',metadata=print:key=lavfi.scene_score,showinfo"),
            "-af", f"silencedetect=n={self.SILENCE_THRESHOLD_DB}dB:d=0.15", "-f", "null", "-",
        ]
        if self.cancel_event is not None:
            result = run_cancellable(args, max(60.0, duration * 4), self.cancel_event)
        else:
            result = subprocess.run(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                check=False, timeout=max(60.0, duration * 4), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        if result.returncode:
            raise RuntimeError(result.stderr[-1000:] or "FFmpeg breakpoint analysis failed")
        # Older FFmpeg versions omit filter instance names in human log lines.
        # Machine metadata on separate streams identifies the passes reliably.
        log = '\n'.join(line for line in result.stderr.splitlines() if not re.search(r'black_start:[0-9.-]+', line))
        for name, stream in [('strict', result.stdout), ('noisy', result.stderr)]:
            left = None
            for event in re.finditer(r'lavfi.black_(start|end)=(-?[0-9.]+)', stream):
                point = float(event[2])
                if event[1] == 'start':
                    left = point
                elif left is not None:
                    if point - left >= self.BLACK_MIN_SECONDS and left >= 0:
                        log += f'\n[blackdetect@{name}] black_start:{left} black_end:{point} black_duration:{point-left}'
                    left = None
        return log

    def _chapter_starts(self, row, playback_path: Path) -> list[int]:
        # Probe the working source first: a proxy may have discarded chapters.
        source = resolve_asset_path(self.connection, self.config, row["id"])
        for path in (source, playback_path):
            if not path.is_file():
                continue
            try:
                args = [self.config.ffprobe, "-v", "error", "-show_chapters", "-of", "json", os.fspath(path)]
                if self.cancel_event is not None:
                    result = run_cancellable(args, 30, self.cancel_event)
                    if result.returncode:
                        continue
                else:
                    result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=True,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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

    def _quiet_candidates(
        self, stderr: str, start_ms: int, end_ms: int, duration_ms: int
    ) -> list[dict]:
        """Use measured, fully bounded pauses only after stronger cues fail."""
        candidates = []
        pause_start = None
        window_ms = end_ms - start_ms
        for match in re.finditer(r"silence_(start|end):\s*(-?[0-9.]+)", stderr):
            point_ms = round(float(match[2]) * 1000)
            if match[1] == "start":
                pause_start = point_ms
                continue
            if pause_start is None:
                continue
            left, right = pause_start, point_ms
            pause_start = None
            # FFmpeg reports a leading silence from zero and closes trailing
            # silence at EOF. Their full extent is unknown; allow 20 ms for
            # timestamp/sample rounding at either edge of the scan.
            if left <= 20 or right >= window_ms - 20 or right - left < self.QUIET_PAUSE_MIN_MS:
                continue
            offset_ms = start_ms + (left + right) // 2
            if not self.minimum_segment_ms <= offset_ms <= duration_ms - self.minimum_segment_ms:
                continue
            candidates.append({
                "offset_ms": offset_ms, "method": "quiet_pause", "score": 0.35,
                "signals": {"fallback": True, "silence": True,
                            "silence_start_ms": start_ms + left,
                            "silence_end_ms": start_ms + right,
                            "silence_duration_ms": right - left,
                            "silence_threshold_db": self.SILENCE_THRESHOLD_DB,
                            "reason": "no stronger natural cue; sustained quiet pause"},
            })
        return candidates

    @staticmethod
    def _scene_events(stderr: str) -> list[tuple[float, float]]:
        """Pair scene scores with their frame timestamps, retaining legacy logs."""
        events = {}
        current = None
        for line in stderr.splitlines():
            timestamp = re.search(r'pts_time:\s*(-?[0-9.]+)',line)
            if timestamp:
                current = float(timestamp[1])
                if 'showinfo' in line:
                    events.setdefault(current,0.0)
            score = re.search(r'lavfi\.scene_score[=:]\s*([0-9.eE+-]+)',line)
            if score and current is not None:
                events[current] = float(score[1])
        return sorted(events.items())

    def _still_candidates(self, stderr, start_ms, end_ms, duration_ms):
        window_ms = end_ms-start_ms
        transitions = [(round(point*1000),score) for point,score in self._scene_events(stderr)]
        holds, left = [], None
        for match in re.finditer(r'freeze_(start|end):\s*(-?[0-9.]+)',stderr):
            point = round(float(match[2])*1000)
            if match[1] == 'start':
                left = point
                continue
            if left is None:
                continue
            right, begin = point, left
            left = None
            if (begin <= 20 or right >= window_ms-20
                    or not self.STILL_CARD_MIN_MS <= right-begin <= self.STILL_CARD_MAX_MS):
                continue
            entry = [p for p,_ in transitions if abs(p-begin)<=self.STILL_TRANSITION_TOLERANCE_MS]
            exit_ = [p for p,_ in transitions if abs(p-right)<=self.STILL_TRANSITION_TOLERANCE_MS]
            if not entry or not exit_:
                continue
            holds.append({'start_ms':start_ms+begin,'end_ms':start_ms+right,
                'duration_ms':right-begin,
                'entry_transition_ms':start_ms+min(entry,key=lambda p:(abs(p-begin),p)),
                'exit_transition_ms':start_ms+min(exit_,key=lambda p:(abs(p-right),p))})
        candidates, paired = [], set()

        def candidate(offset, evidence, pair):
            if self.minimum_segment_ms <= offset <= duration_ms-self.minimum_segment_ms:
                candidates.append({'offset_ms':offset,'method':'still_card','score':0.65,
                    'signals':{'still_card':True,'holds':evidence,'paired_cards':pair,
                        'still_noise_tolerance':self.STILL_NOISE_TOLERANCE,
                        'reason':'transition between adjacent held cards' if pair else
                                 'near-still card bounded by scene transitions; midpoint within hold'}})

        holds.sort(key=lambda h:(h['start_ms'],h['end_ms']))
        for index,(first,second) in enumerate(zip(holds,holds[1:])):
            if not 0 <= second['start_ms']-first['end_ms'] <= self.STILL_PAIR_GAP_MS:
                continue
            center = (first['end_ms']+second['start_ms'])//2
            boundaries = [first['exit_transition_ms'],second['entry_transition_ms']]
            offset = min(boundaries,key=lambda p:(abs(p-center),p))
            if self.minimum_segment_ms <= offset <= duration_ms-self.minimum_segment_ms:
                candidate(offset,[first,second],True)
                paired.update((index,index+1))
        for index,hold in enumerate(holds):
            if index not in paired:
                candidate((hold['start_ms']+hold['end_ms'])//2,[hold],False)
        return candidates

    def _parse_candidates(
        self, stderr: str, start_ms: int, midpoint_ms: int, duration_ms: int
    ) -> list[dict]:
        silence_ranges = []
        starts = [float(value) for value in re.findall(r"silence_start:\s*([0-9.]+)", stderr)]
        ends = [float(value) for value in re.findall(r"silence_end:\s*([0-9.]+)", stderr)]
        for index, value in enumerate(starts):
            silence_ranges.append((value, ends[index] if index < len(ends) else value + 0.2))
        raw: list[tuple[float, str, dict]] = []
        for item in self._fade_intervals(stderr, start_ms):
            point = (item['black_start_ms'] + item['black_end_ms']) / 2000 - start_ms / 1000
            item['silence'] = any(a - .25 <= point <= b + .25 for a, b in silence_ranges)
            raw.append((point, 'fade', item))
        for value, scene_score in self._scene_events(stderr):
            raw.append((value, "scene", {"scene_change": True, "scene_score":scene_score}))
        by_second: dict[tuple[int,str], dict] = {}
        for relative, method, signals in raw:
            offset_ms = start_ms + round(relative * 1000)
            if not (self.minimum_segment_ms <= offset_ms <= duration_ms - self.minimum_segment_ms):
                continue
            distance = abs(offset_ms - midpoint_ms)
            proximity = max(0.0, 1.0 - distance / max(1, self.search_window_ms))
            score = 0.35 * proximity
            if method == "fade":
                score = .80 if signals.get("strict_black") else .70
                if signals.get("silence"):
                    score += 0.20
            else:
                score += 0.25
                signals["silence"] = any(a - 0.5 <= relative <= b + 0.5
                                         for a, b in silence_ranges)
            key = (offset_ms, method)
            item = {
                "offset_ms": offset_ms, "method": method,
                "score": min(1.0, score), "signals": signals,
            }
            if key not in by_second or item["score"] > by_second[key]["score"]:
                by_second[key] = item
        return sorted(by_second.values(), key=lambda item: item["offset_ms"])

    def _fade_intervals(self, stderr, start_ms):
        samples, current = [], None
        intervals = []
        for line in stderr.splitlines():
            match = re.search(r'pts_time:\s*(-?[\d.]+)', line)
            if match:
                current = float(match[1])
            match = re.search(r'lavfi.signalstats.YAVG=([\d.]+)', line)
            if match and current is not None:
                samples.append((current, float(match[1])))
            match = re.search(r'black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)', line)
            if match:
                left, right, length = map(float, match.groups())
                intervals.append((left, right, length, 'blackdetect@noisy' not in line))
        times = [point for point, _ in samples]

        def values(left, right):
            return samples[bisect_left(times, left):bisect_right(times, right)]

        accepted = []
        for left, right, length, strict in intervals:
            evidence = {'strict_black': strict, 'noisy_black': not strict,
                        'black_start_ms': start_ms + round(left * 1000),
                        'black_end_ms': start_ms + round(right * 1000),
                        'black_duration': length,
                        'pixel_threshold': .10 if strict else self.NOISY_BLACK_PIXEL_THRESHOLD,
                        'picture_threshold': .98 if strict else self.NOISY_BLACK_PICTURE_THRESHOLD}
            if not strict:
                before, during, after = values(left - 2, left), values(left, right), values(right, right + 2)
                if not before or not during or not after or left <= times[0] or right >= times[-1]:
                    continue
                dark = min(v for _, v in during)
                contrasts = [max(v for _, v in side) - dark for side in (before, after)]
                run_start = last_fall = None
                fade_ms = 0
                fall_total = fall_max = fall_count = 0
                sequence = before + during
                for (previous_t, previous_v), (point, value) in zip(sequence, sequence[1:]):
                    delta = previous_v - value
                    if delta >= .5:
                        if run_start is None:
                            run_start = (previous_t, previous_v)
                            fall_total = fall_max = fall_count = 0
                        fall_total += delta
                        fall_max = max(fall_max, delta)
                        fall_count += 1
                        last_fall = point
                    elif delta < -2 or (last_fall is not None and point - last_fall > .1):
                        run_start = last_fall = None
                    if (run_start is not None and run_start[1] - value >= self.BLACK_CONTRAST
                            and fall_count >= 3 and fall_max <= .8 * fall_total):
                        fade_ms = max(fade_ms, round((last_fall - run_start[0]) * 1000))
                if min(contrasts) < self.BLACK_CONTRAST or (length < .3 and fade_ms < 100):
                    continue
                evidence.update(luma_min=dark, before_contrast=contrasts[0],
                                after_contrast=contrasts[1], fade_in_ms=fade_ms)
            accepted.append(evidence)
        # A strict interval contained in a tolerant one is the same physical fade.
        merged = []
        for item in sorted(accepted, key=lambda i: i['black_start_ms']):
            if merged and item['black_start_ms'] <= merged[-1]['black_end_ms']:
                previous = merged[-1]
                previous['black_end_ms'] = max(previous['black_end_ms'], item['black_end_ms'])
                previous['black_duration'] = (previous['black_end_ms'] - previous['black_start_ms']) / 1000
                previous['strict_black'] |= item['strict_black']
                previous['noisy_black'] |= item['noisy_black']
                previous.setdefault('passes', []).append(item)
            else:
                merged.append(item)
        return merged

    def _store(self, *args, **kwargs):
        with self.connection:
            self._write_result(*args, **kwargs)

    def _write_result(self, asset_id, signature, candidates, selected, confidence, detail, *, status="analyzed", layout=None, overrides=None, expected_layout=None, expected_manuals=None):
        selected_items = selected if isinstance(selected, list) else [selected]
        now = utc_now()
        # One transaction publishes cuts, target numbering and manual timestamps.
        self.connection.execute('SAVEPOINT publish_breaks')
        # Acquire the write reservation before checking for a concurrent edit.
        self.connection.execute('UPDATE asset_break_analyses SET asset_id=asset_id WHERE asset_id=?', (asset_id,))
        latest = self.connection.execute('SELECT target_layout_json FROM asset_break_analyses WHERE asset_id=?', (asset_id,)).fetchone()
        if ((latest[0] if latest is not None else None) != expected_layout
                or expected_manuals is not None and self._manuals(asset_id) != expected_manuals):
            raise ValueError('Act-break targets or manual choices changed during analysis; retry')
        if overrides is not None:
            self.connection.execute('DELETE FROM asset_break_overrides WHERE asset_id=?', (asset_id,))
            self.connection.executemany('INSERT INTO asset_break_overrides(asset_id,target_index,offset_ms,updated_at) VALUES (?,?,?,?)',
                                       [(asset_id, index, point, now) for index, point in overrides.items()])
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
        self.connection.execute('UPDATE asset_break_analyses SET target_layout_json=?,refresh_error=NULL WHERE asset_id=?',
                                (json.dumps(layout, sort_keys=True), asset_id))
        self.connection.execute('RELEASE publish_breaks')
        self.connection.commit()

    def _validate_offset(self, offset_ms: int, duration_ms: int) -> None:
        if not self.minimum_segment_ms <= offset_ms <= duration_ms - self.minimum_segment_ms:
            raise ValueError(
                "Breakpoint must leave at least "
                f"{self.minimum_segment_ms / 60000:g} minutes on each side"
            )
