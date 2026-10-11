from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class WindowOccurrence:
    occurrence_date: str
    starts_at: datetime
    ends_at: datetime


class MaintenanceWindowSchedule:
    def __init__(self, config) -> None:
        self.config = config

    @property
    def settings(self) -> dict[str, object]:
        return self.config.data.get("maintenance_window", {})

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", False))

    @property
    def timezone(self) -> ZoneInfo:
        name = str(self.config.data.get("application", {}).get("timezone", "UTC"))
        try:
            return ZoneInfo(name)
        except Exception:
            return ZoneInfo("UTC")

    def active(self, now: datetime | None = None) -> WindowOccurrence | None:
        current = self._local(now)
        start_value = self._time("start", "03:00")
        end_value = self._time("end", "05:00")
        today_start = datetime.combine(current.date(), start_value, self.timezone)
        if start_value < end_value:
            occurrence = WindowOccurrence(
                current.date().isoformat(), today_start,
                datetime.combine(current.date(), end_value, self.timezone),
            )
        elif current.timetz().replace(tzinfo=None) >= start_value:
            occurrence = WindowOccurrence(
                current.date().isoformat(), today_start,
                datetime.combine(current.date() + timedelta(days=1), end_value, self.timezone),
            )
        else:
            previous = current.date() - timedelta(days=1)
            occurrence = WindowOccurrence(
                previous.isoformat(), datetime.combine(previous, start_value, self.timezone),
                datetime.combine(current.date(), end_value, self.timezone),
            )
        return occurrence if occurrence.starts_at <= current < occurrence.ends_at else None

    def next_occurrence(self, now: datetime | None = None) -> WindowOccurrence | None:
        if not self.enabled:
            return None
        current = self._local(now)
        active = self.active(current)
        if active is not None:
            return active
        start_value = self._time("start", "03:00")
        end_value = self._time("end", "05:00")
        day = current.date() if current.timetz().replace(tzinfo=None) < start_value else current.date() + timedelta(days=1)
        starts = datetime.combine(day, start_value, self.timezone)
        ends_day = day if start_value < end_value else day + timedelta(days=1)
        return WindowOccurrence(day.isoformat(), starts, datetime.combine(ends_day, end_value, self.timezone))

    def status(self, persisted: dict[str, object], now: datetime | None = None) -> dict[str, object]:
        current = self._local(now)
        active = self.active(current) if self.enabled else None
        upcoming = self.next_occurrence(current)
        result = {
            "enabled": self.enabled,
            "start": str(self.settings.get("start", "03:00")),
            "end": str(self.settings.get("end", "05:00")),
            "timezone": self.timezone.key,
            "state": "disabled" if not self.enabled else "waiting",
            "occurrence_date": None,
            "starts_at": None,
            "ends_at": None,
            "seconds_remaining": None,
            "next_starts_at": upcoming.starts_at.isoformat() if upcoming else None,
            "resume_channel": persisted.get("resume_channel"),
            "auto_resume": bool(persisted.get("auto_resume", False)),
            "completed_at": persisted.get("completed_at"),
            "reason": persisted.get("reason"),
        }
        if active is not None:
            same = persisted.get("occurrence_date") == active.occurrence_date
            state = str(persisted.get("state", "waiting")) if same else "waiting"
            if same and state in {"completed_early", "complete", "skipped", "failed"}:
                following = self.next_occurrence(active.ends_at + timedelta(seconds=1))
                result.update({
                    "state": state,
                    "occurrence_date": active.occurrence_date,
                    "starts_at": active.starts_at.isoformat(),
                    "ends_at": active.ends_at.isoformat(),
                    "seconds_remaining": None,
                    "next_starts_at": following.starts_at.isoformat() if following else None,
                })
                return result
            result.update({
                "state": state,
                "occurrence_date": active.occurrence_date,
                "starts_at": active.starts_at.isoformat(),
                "ends_at": active.ends_at.isoformat(),
                "seconds_remaining": max(0, int((active.ends_at - current).total_seconds())),
                "next_starts_at": active.starts_at.isoformat(),
            })
        return result

    def _time(self, key: str, default: str) -> time:
        value = str(self.settings.get(key, default))
        try:
            parsed = datetime.strptime(value, "%H:%M").time()
        except ValueError:
            parsed = datetime.strptime(default, "%H:%M").time()
        return parsed

    def _local(self, supplied: datetime | None) -> datetime:
        if supplied is None:
            return datetime.now(self.timezone)
        if supplied.tzinfo is None:
            return supplied.replace(tzinfo=self.timezone)
        return supplied.astimezone(self.timezone)


class MaintenanceWindowStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, object]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def write(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)
