from __future__ import annotations


import base64
import hashlib
import json
import os
import platform
import random
import re
import shutil
import subprocess
import uuid
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from retrotv.tts import BreezeTTSProvider, provider_for_profile, profile_voice
from retrotv.guide import GuideService
from retrotv.graphics import GraphicsService
from retrotv.ai_announcements import (
    AnnouncementProfileService,
    OpenAIHTTPClient,
    OpenAIScriptProvider,
    OpenAIWeeklyPromoScriptProvider,
    OpenAITTSProvider,
)


@dataclass(frozen=True)
class AnnouncementLine:
    starts_at: datetime
    title: str


@dataclass(frozen=True)
class AnnouncementPlan:
    channel_slug: str
    channel_name: str
    starts_at: datetime
    lines: tuple[AnnouncementLine, ...]
    tagline: str
    background_color: str
    spoken_text: str
    daypart_slug: str | None = None
    weekly_collection_slug: str | None = None
    weekly_special_promo: bool = False
    special_name: str | None = None
    special_airing_at: datetime | None = None
    promo_lineup_more: bool = False


@dataclass(frozen=True)
class AnnouncementResult:
    output: Path
    plan: AnnouncementPlan
    duration_seconds: float
    tts_provider: str
    cached: bool
    audio_mode: str = "silent"
    fallback_reason: str | None = None


class TTSProvider(Protocol):
    name: str

    def synthesize(
        self, text: str, output: Path, *, voice: str, rate: int,
        instructions: str = "",
    ) -> None:
        ...


class WindowsSapiTTS:
    name = "windows-sapi"

    def __init__(self, executable: str | None = None):
        self.executable = executable or shutil.which("powershell.exe") or shutil.which(
            "powershell"
        )

    @property
    def available(self) -> bool:
        return platform.system() == "Windows" and bool(self.executable)

    def synthesize(
        self, text: str, output: Path, *, voice: str, rate: int,
        instructions: str = "",
    ) -> None:
        if not self.available or not self.executable:
            raise RuntimeError("Windows SAPI speech synthesis is unavailable")
        script = """
Add-Type -AssemblyName System.Speech
$text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:RETROTV_TTS_TEXT))
$output = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:RETROTV_TTS_OUTPUT))
$voice = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:RETROTV_TTS_VOICE))
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  if ($voice) { $speaker.SelectVoice($voice) }
  $speaker.Rate = [Math]::Max(-10, [Math]::Min(10, [int]$env:RETROTV_TTS_RATE))
  $speaker.SetOutputToWaveFile($output)
  $speaker.Speak($text)
} finally {
  $speaker.Dispose()
}
"""
        encoded_script = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        environment = os.environ.copy()
        environment.update(
            {
                "RETROTV_TTS_TEXT": base64.b64encode(text.encode("utf-8")).decode(
                    "ascii"
                ),
                "RETROTV_TTS_OUTPUT": base64.b64encode(
                    str(output.resolve()).encode("utf-8")
                ).decode("ascii"),
                "RETROTV_TTS_VOICE": base64.b64encode(voice.encode("utf-8")).decode(
                    "ascii"
                ),
                "RETROTV_TTS_RATE": str(rate),
            }
        )
        completed = hidden_run(
            [
                self.executable,
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded_script,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=environment,
        )
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(detail or "Windows SAPI speech synthesis failed")
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("Windows SAPI did not create an audio file")


class UpNextService:
    def __init__(self, connection, config):
        self.connection = connection
        self.config = config
        self.guide = GuideService(connection, config)
        self.settings = config.data.get("announcements", {})

    def build_plan(
        self,
        channel_slug: str,
        when: datetime | None = None,
        *,
        listing_count: int | None = None,
    ) -> AnnouncementPlan:
        moment = when or datetime.now(self.guide.timezone)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self.guide.timezone)
        else:
            moment = moment.astimezone(self.guide.timezone)
        channel = self.connection.execute(
            "SELECT * FROM channels WHERE slug = ? AND enabled = 1",
            (channel_slug,),
        ).fetchone()
        if channel is None:
            raise ValueError(f"Enabled channel {channel_slug!r} does not exist")

        count = max(
            1,
            min(
                6,
                int(
                    listing_count
                    if listing_count is not None
                    else self.settings.get("listing_count", 3)
                ),
            ),
        )
        slot_minutes = self.guide.slot_minutes
        slot_start = self.guide._slot_start(moment) + timedelta(minutes=slot_minutes)
        lines: list[AnnouncementLine] = []
        listing_cache: dict[int, list] = {}
        for index in range(count):
            starts_at = slot_start + timedelta(minutes=index * slot_minutes)
            generation = self.connection.execute(
                """
                SELECT id FROM schedule_generations
                WHERE channel_id = ? AND schedule_date = ? AND status = 'active'
                ORDER BY activated_at DESC, id DESC LIMIT 1
                """,
                (int(channel["id"]), starts_at.date().isoformat()),
            ).fetchone()
            title = "TBD"
            if generation is not None:
                generation_id = int(generation["id"])
                programs = listing_cache.setdefault(
                    generation_id, self.guide._program_listings(generation_id)
                )
                listing = self.guide._listing_for_slot(
                    programs,
                    starts_at,
                    starts_at + timedelta(minutes=slot_minutes),
                )
                if listing is not None:
                    title = listing.series_title or listing.title
            lines.append(AnnouncementLine(starts_at, title))

        graphics = GraphicsService(self.connection, self.config)
        # The presentation introduces the next slot, so its branding and
        # graphics scope belong to that slot—not to the outgoing daypart.
        target_time = lines[0].starts_at if lines else slot_start
        weekly_collection = graphics.weekly_collection_at(channel_slug, target_time)
        target_daypart = graphics.daypart_slug_at(target_time)
        override = graphics.branding_override(channel_slug, target_daypart)
        channel_name = graphics.identity_name(
            channel_slug, str(channel["display_name"]), target_daypart
        )
        channel_settings = {
            **self.settings.get("channels", {}).get(channel_slug, {}),
            **override,
        }
        resolved_profile = AnnouncementProfileService(
            self.connection, self.config
        ).profile(channel_slug, target_daypart, weekly_collection)
        if resolved_profile.weekly_collection_slug:
            channel_name = resolved_profile.channel_name
        tagline = str(
            resolved_profile.tagline or channel_settings.get(
                "tagline",
                self.settings.get("default_tagline", "You're watching {channel}!"),
            )
        ).format(channel=channel_name)
        spoken_tagline = str(
            resolved_profile.spoken_tagline or channel_settings.get(
                "spoken_tagline",
                self.settings.get("default_spoken_tagline", tagline),
            )
        ).format(channel=channel_name)
        color = self._color(
            str(channel_settings.get("background_color", "0x123B68"))
        )
        spoken_text = self._spoken_script(
            channel_name,
            lines,
            spoken_tagline,
            channel_settings.get("script", {}),
        )
        return AnnouncementPlan(
            channel_slug=channel_slug,
            channel_name=channel_name,
            starts_at=slot_start,
            lines=tuple(lines),
            tagline=tagline,
            background_color=color,
            spoken_text=spoken_text,
            daypart_slug=target_daypart,
            weekly_collection_slug=weekly_collection,
        )

    def build_opportunity_plan(self, opportunity_id: int) -> AnnouncementPlan:
        self.refresh_opportunity_facts(opportunity_id)
        row = self.connection.execute(
            """
            SELECT ao.*, c.slug AS channel_slug, c.display_name AS channel_name
            FROM announcement_opportunities ao
            JOIN channels c ON c.id=ao.channel_id
            WHERE ao.id=?
            """,
            (opportunity_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Announcement opportunity {opportunity_id} does not exist")
        facts = json.loads(row["facts_json"])
        if row["opportunity_type"] == "weekly_special_promo":
            return self._weekly_special_promo_plan(row, facts)
        lines = [
            AnnouncementLine(
                self._nearest_guide_slot(datetime.fromisoformat(item["starts_at"])),
                item["title"],
            )
            for item in facts.get("listings", [])
        ]
        channel_slug = str(row["channel_slug"])
        graphics = GraphicsService(self.connection, self.config)
        opportunity_time = datetime.fromisoformat(row["starts_at"])
        target_time = lines[0].starts_at if lines else opportunity_time
        # An announcement immediately before a Weekly Special must use that
        # special's Up Next graphics, profile, and optional holiday variant.
        weekly_collection = graphics.weekly_collection_at(channel_slug, target_time)
        target_daypart = graphics.daypart_slug_at(target_time)
        override = graphics.branding_override(channel_slug, target_daypart)
        channel_name = graphics.identity_name(
            channel_slug, str(row["channel_name"]), target_daypart
        )
        channel_settings = {
            **self.settings.get("channels", {}).get(channel_slug, {}),
            **override,
        }
        resolved_profile = AnnouncementProfileService(
            self.connection, self.config
        ).profile(channel_slug, target_daypart, weekly_collection)
        if resolved_profile.weekly_collection_slug:
            channel_name = resolved_profile.channel_name
        color = self._color(str(channel_settings.get("background_color", "0x123B68")))
        if facts.get("station_identification") or not lines:
            tagline = str(
                resolved_profile.tagline or channel_settings.get(
                    "tagline", self.settings.get("default_tagline", "You're watching {channel}!")
                )
            ).format(channel=channel_name)
            spoken_tagline = str(
                resolved_profile.spoken_tagline or tagline
            ).format(channel=channel_name)
            return AnnouncementPlan(
                channel_slug, channel_name, datetime.fromisoformat(row["starts_at"]),
                (), tagline, color, spoken_tagline, target_daypart, weekly_collection,
            )
        tagline = str(
            resolved_profile.tagline or channel_settings.get(
                "tagline", self.settings.get("default_tagline", "You're watching {channel}!")
            )
        ).format(channel=channel_name)
        spoken_tagline = str(
            resolved_profile.spoken_tagline or channel_settings.get(
                "spoken_tagline", self.settings.get("default_spoken_tagline", tagline)
            )
        ).format(channel=channel_name)
        return AnnouncementPlan(
            channel_slug, channel_name, datetime.fromisoformat(row["starts_at"]),
            tuple(lines), tagline, color,
            self._spoken_script(channel_name, lines, spoken_tagline, channel_settings.get("script", {})),
            target_daypart, weekly_collection,
        )

    def _weekly_special_promo_plan(self, row, facts: dict) -> AnnouncementPlan:
        details = facts.get("weekly_special_promo", {})
        airing_at = datetime.fromisoformat(str(details["airing_at"]))
        name = str(details.get("special_name") or details.get("collection_slug") or "Weekly Special")
        selected = list(facts.get("selected_lineup", []))[:2]
        lines = tuple(AnnouncementLine(airing_at, str(title)) for title in selected)
        date_label = airing_at.strftime("%A, %B %d").replace(" 0", " ")
        time_label = self._display_time(airing_at)
        graphics = GraphicsService(self.connection, self.config)
        daypart = graphics.daypart_slug_at(datetime.fromisoformat(row["starts_at"]))
        channel_name = graphics.identity_name(
            str(row["channel_slug"]), str(row["channel_name"]), daypart
        )
        profile = AnnouncementProfileService(self.connection, self.config).profile(
            str(row["channel_slug"]), daypart,
            str(details.get("collection_slug") or "") or None,
        )
        if profile.weekly_collection_slug:
            channel_name = profile.channel_name
        return AnnouncementPlan(
            channel_slug=str(row["channel_slug"]),
            channel_name=channel_name,
            starts_at=datetime.fromisoformat(row["starts_at"]),
            lines=lines,
            tagline=f"{name} · {date_label} at {time_label}",
            background_color="0x123B68",
            spoken_text="",
            daypart_slug=daypart,
            weekly_collection_slug=str(details.get("collection_slug") or "") or None,
            weekly_special_promo=True,
            special_name=name,
            special_airing_at=airing_at,
            promo_lineup_more=bool(facts.get("lineup_more", False)),
        )

    def refresh_opportunity_facts(self, opportunity_id: int) -> None:
        opportunity = self.connection.execute(
            "SELECT * FROM announcement_opportunities WHERE id=?", (opportunity_id,)
        ).fetchone()
        if opportunity is None:
            raise ValueError(f"Announcement opportunity {opportunity_id} does not exist")
        if opportunity["opportunity_type"] == "weekly_special_promo":
            self._refresh_weekly_special_promo_facts(opportunity)
            return
        rows = self.connection.execute(
            """
            SELECT sg.id AS generation_id, se.programming_block_id,
                   MIN(se.starts_at) AS starts_at,
                   COALESCE(MAX(a.series_title), MAX(se.guide_listing_title),
                            MAX(se.title)) AS title
            FROM schedule_entries se
            JOIN schedule_generations sg ON sg.id=se.generation_id
            LEFT JOIN assets a ON a.id=se.asset_id
            WHERE sg.channel_id=? AND sg.status='active' AND se.is_program=1
              AND se.starts_at>=?
            GROUP BY sg.id, se.programming_block_id
            ORDER BY MIN(se.starts_at)
            LIMIT 3
            """,
            (int(opportunity["channel_id"]), opportunity["ends_at"]),
        ).fetchall()
        old = json.loads(opportunity["facts_json"])
        facts = {
            "channel": old.get("channel"),
            "station_identification": not bool(rows),
            "listings": [
                {
                    "generation_id": int(item["generation_id"]),
                    "programming_block_id": item["programming_block_id"],
                    "starts_at": item["starts_at"],
                    "title": item["title"] or "TBD",
                }
                for item in rows
            ],
        }
        if facts == old:
            return
        encoded = json.dumps(facts, sort_keys=True)
        cache_key = hashlib.sha256(
            json.dumps(
                {"generation": int(opportunity["generation_id"]),
                 "entry": int(opportunity["schedule_entry_id"]),
                 "facts": facts, "renderer": 3},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        self.connection.execute(
            """
            UPDATE announcement_opportunities
            SET facts_json=?, cache_key=?, fallback_path=NULL,
                validated_output_path=NULL, render_status='pending',
                detail='Schedule facts refreshed', updated_at=?
            WHERE id=?
            """,
            (encoded, cache_key, datetime.now().astimezone().isoformat(timespec="seconds"), opportunity_id),
        )
        self.connection.commit()

    def _refresh_weekly_special_promo_facts(self, opportunity) -> None:
        old = json.loads(opportunity["facts_json"])
        details = old.get("weekly_special_promo", {})
        airing_at = datetime.fromisoformat(str(details["airing_at"]))
        airing_end = datetime.fromisoformat(str(details.get("airing_ends_at", details["airing_at"])))
        generation = self.connection.execute(
            """SELECT id FROM schedule_generations
               WHERE channel_id=? AND schedule_date=? AND status='active'
               ORDER BY activated_at DESC,id DESC LIMIT 1""",
            (int(opportunity["channel_id"]), airing_at.date().isoformat()),
        ).fetchone()
        lineup = list(old.get("lineup", []))
        if generation is not None:
            rows = self.connection.execute(
                """SELECT DISTINCT COALESCE(NULLIF(a.series_title,''),
                           NULLIF(se.guide_listing_title,''),NULLIF(se.title,'')) AS title,
                           MIN(se.starts_at) AS starts_at
                   FROM schedule_entries se LEFT JOIN assets a ON a.id=se.asset_id
                   WHERE se.generation_id=? AND se.is_program=1
                     AND se.programming_owner=? AND se.starts_at>=? AND se.starts_at<?
                   GROUP BY title ORDER BY MIN(se.starts_at)""",
                (int(generation["id"]), str(details.get("window_slug", "")),
                 airing_at.isoformat(timespec="seconds"), airing_end.isoformat(timespec="seconds")),
            ).fetchall()
            lineup = [str(item["title"]).strip() for item in rows if item["title"] and str(item["title"]).strip()]
        updated = dict(old)
        updated["lineup"] = lineup
        if lineup != old.get("lineup", []):
            chosen = random.SystemRandom().sample(lineup, min(2, len(lineup)))
            updated["selected_lineup"] = chosen
            updated["lineup_more"] = len(lineup) > len(chosen)
        else:
            updated.setdefault("selected_lineup", [])
            updated.setdefault("lineup_more", False)
        if updated == old:
            return
        encoded = json.dumps(updated, sort_keys=True)
        cache_key = hashlib.sha256(json.dumps(
            {"generation": int(opportunity["generation_id"]),
             "entry": int(opportunity["schedule_entry_id"]),
             "facts": updated, "renderer": 5}, sort_keys=True
        ).encode()).hexdigest()
        self.connection.execute(
            """UPDATE announcement_opportunities
               SET facts_json=?,cache_key=?,fallback_path=NULL,
                   validated_output_path=NULL,render_status='pending',
                   detail='Weekly special facts refreshed',updated_at=? WHERE id=?""",
            (encoded, cache_key, datetime.now().astimezone().isoformat(timespec="seconds"),
             int(opportunity["id"])),
        )
        self.connection.commit()

    def _spoken_script(
        self,
        channel_name: str,
        lines: list[AnnouncementLine],
        spoken_tagline: str,
        channel_script: dict,
    ) -> str:
        defaults = {
            "opening": "Up next on {channel}...",
            "first": "{title}.",
            "second": "Followed by {title}.",
            "third": "Then, later today, {title}.",
            "additional": "And later, {title}.",
            "closing": "{tagline}",
        }
        configured = self.settings.get("script", {})
        templates = {
            key: str(channel_script.get(key, configured.get(key, value)))
            for key, value in defaults.items()
        }
        parts = [templates["opening"].format(channel=channel_name)]
        known_lines = [line for line in lines if line.title != "TBD"]
        roles = ("first", "second", "third")
        for index, line in enumerate(known_lines):
            role = roles[index] if index < len(roles) else "additional"
            parts.append(
                templates[role].format(
                    channel=channel_name,
                    title=line.title,
                    time=self._spoken_time(line.starts_at),
                )
            )
        parts.append(
            templates["closing"].format(
                channel=channel_name,
                tagline=spoken_tagline,
            )
        )
        return " ".join(part.strip() for part in parts if part.strip())

    @staticmethod
    def _color(value: str) -> str:
        normalized = value.strip().removeprefix("#").removeprefix("0x")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", normalized):
            raise ValueError(f"Invalid announcement background color: {value!r}")
        return f"0x{normalized.upper()}"

    @staticmethod
    def _display_time(value: datetime) -> str:
        return f"{value.strftime('%I').lstrip('0')}:{value:%M} {value:%p}"

    @staticmethod
    def _spoken_time(value: datetime) -> str:
        hour = value.strftime("%I").lstrip("0")
        return f"{hour} {value:%p}" if value.minute == 0 else f"{hour}:{value:%M} {value:%p}"

    def _nearest_guide_slot(self, value: datetime) -> datetime:
        slot = max(1, int(self.guide.slot_minutes))
        hour = value.replace(minute=0, second=0, microsecond=0)
        rounded_minutes = int(
            (value.minute + value.second / 60 + slot / 2) // slot
        ) * slot
        return hour + timedelta(minutes=rounded_minutes)


class UpNextRenderer:
    _fallback_failures: dict[tuple[str, int, str], tuple[float, str]] = {}
    _fallback_video_validation: dict[tuple, bool] = {}
    def __init__(
        self, connection, config, *, tts_provider: TTSProvider | None = None,
        script_provider=None, ai_client: OpenAIHTTPClient | None = None,
    ):
        self.connection = connection
        self.config = config
        self.service = UpNextService(connection, config)
        self.settings = config.data.get("announcements", {})
        self._tts_provider = tts_provider
        self._ai_client = ai_client or OpenAIHTTPClient(
            base_url=str(self.settings.get("openai_base_url", "https://api.openai.com/v1")),
            timeout_seconds=float(self.settings.get("provider_timeout_seconds", 90)),
        )
        self._script_provider = script_provider
        self._promo_script_provider = None
        self.profiles = AnnouncementProfileService(connection, config)

    def render(
        self,
        channel_slug: str,
        when: datetime | None = None,
        *,
        listing_count: int | None = None,
        output: Path | None = None,
        use_tts: bool = True,
        force: bool = False,
        exact_duration: float | None = None,
        background: Path | None = None,
        music: Path | None = None,
        plan_override: AnnouncementPlan | None = None,
        provider_override: TTSProvider | None = None,
        audio_override: Path | None = None,
        voice_override: str | None = None,
        instructions: str = "",
        select_generic: bool = True,
    ) -> AnnouncementResult:
        plan = plan_override or self.service.build_plan(
            channel_slug, when, listing_count=listing_count
        )
        if not use_tts and audio_override is None and select_generic and not plan.weekly_special_promo:
            recordings, reason = self.profiles.generic_recordings(
                channel_slug, plan.daypart_slug, plan.weekly_collection_slug
            )
            for recording in [*recordings, None]:
                try:
                    result = self.render(
                        channel_slug, when, listing_count=listing_count, output=output,
                        use_tts=False, force=force or (output is not None and recording is not None), exact_duration=exact_duration,
                        background=background, music=music, plan_override=plan,
                        audio_override=recording.path if recording else None,
                        voice_override=voice_override, instructions=instructions, select_generic=False,
                    )
                    return replace(result, audio_mode="generic" if recording else "silent", fallback_reason=reason)
                except Exception as exc:
                    if recording is None:
                        raise
                    reason = f"Generic audio could not be incorporated: {exc}"
        provider = provider_override or self._provider(
            use_tts,
            channel_slug,
            plan.daypart_slug,
            plan.weekly_collection_slug,
        )
        provider_label = provider.name if provider else "generic" if audio_override else "none"
        voice, rate = self._voice_settings(channel_slug)
        if voice_override is not None:
            voice = voice_override
        if use_tts and provider is not None and provider_override is None and isinstance(
            provider, (OpenAITTSProvider, BreezeTTSProvider)
        ):
            profile = self.profiles.profile(
                channel_slug, plan.daypart_slug, plan.weekly_collection_slug
            )
            script_provider = self._script_provider or OpenAIScriptProvider(self._ai_client)
            plan = replace(plan, spoken_text=script_provider.generate(profile, plan))
            voice = profile_voice(profile)
            instructions = profile.voice_instructions
        graphics = GraphicsService(self.connection, self.config)
        if background is None:
            background = graphics.select(
                plan.channel_slug, "up-next", plan.starts_at,
                daypart_slug=plan.daypart_slug,
                weekly_collection_slug=plan.weekly_collection_slug,
            )
        if music is None:
            music = graphics.select_music(
                plan.channel_slug, "up-next", plan.starts_at,
                daypart_slug=plan.daypart_slug,
                weekly_collection_slug=plan.weekly_collection_slug,
            )
        target = output or self._cache_path(
            plan, provider, voice, rate, background=background, music=music, instructions=instructions,
            audio=audio_override,
        )
        target = target if target.is_absolute() else (Path.cwd() / target).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and not force:
            try:
                duration = self._validate(target, exact_duration)
                return AnnouncementResult(
                    target, plan, duration, provider_label, True,
                    "ai" if provider else "generic" if audio_override else "silent",
                )
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
                # Repair a damaged cached video before abandoning usable speech.
                pass

        work = target.parent / f".retrotv-up-next-{uuid.uuid4().hex}"
        work.mkdir(parents=True)
        temporary_target: Path | None = None
        try:
            audio: Path | None = audio_override
            if provider is not None and audio is None:
                audio = work / "speech.wav"
                provider.synthesize(
                    plan.spoken_text, audio, voice=voice, rate=rate,
                    instructions=instructions,
                )
            speech_seconds = self._audio_duration(audio) if audio else 0.0
            minimum = max(4.0, float(self.settings.get("minimum_duration_seconds", 12)))
            tail = max(0.0, float(self.settings.get("ending_silence_seconds", 1.25)))
            duration = exact_duration if exact_duration is not None else max(minimum, speech_seconds + tail)
            if audio is not None and speech_seconds + tail > duration:
                raise RuntimeError(
                    f"Narration is {speech_seconds:.1f}s and does not fit the {duration:.1f}s opportunity"
                )
            ass = work / "screen.ass"
            ass.write_text(self._ass(plan, duration), encoding="utf-8-sig")
            staged = work / f"announcement-{uuid.uuid4().hex}.mp4"
            self._encode(
                work, ass, audio, staged, plan.background_color, duration,
                background=background,
                music=music,
            )
            actual_duration = self._validate(staged, duration)
            temporary_target = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            shutil.copy2(staged, temporary_target)
            os.replace(temporary_target, target)
            temporary_target = None
        finally:
            if temporary_target is not None:
                temporary_target.unlink(missing_ok=True)
            shutil.rmtree(work, ignore_errors=True)
        return AnnouncementResult(
            target,
            plan,
            actual_duration,
            provider_label,
            False,
            "ai" if provider else "generic" if audio_override else "silent",
        )

    def render_opportunity(
        self,
        opportunity_id: int,
        *,
        use_tts: bool,
        force: bool = False,
        activate: bool = True,
    ) -> AnnouncementResult:
        if not use_tts:
            return self._render_fallback(opportunity_id, force=force, activate=activate)
        try:
            result = self._render_ai_opportunity(opportunity_id, force=force, activate=activate)
            return replace(result, audio_mode="ai")
        except Exception as exc:
            plan = self.service.build_opportunity_plan(opportunity_id)
            if not plan.weekly_special_promo:
                try:
                    self._render_fallback(opportunity_id, activate=True, ai_error=str(exc))
                except Exception as fallback_error:
                    self.connection.execute(
                        "UPDATE announcement_opportunities SET fallback_reason=? WHERE id=?",
                        (f"AI failed: {exc}; fallback render failed: {fallback_error}", opportunity_id),
                    )
                    self.connection.commit()
            # Keep the failed AI job visible. Its fallback is activated separately.
            raise

    def _fallback_selection(self, opportunity_id, plan):
        if plan.weekly_special_promo:
            return [], "Weekly promo uses its visual fallback"
        row = self.connection.execute(
            "SELECT starts_at,ends_at FROM announcement_opportunities WHERE id=?", (opportunity_id,)
        ).fetchone()
        seconds = (datetime.fromisoformat(row["ends_at"]) - datetime.fromisoformat(row["starts_at"])).total_seconds()
        recordings, reason = self.profiles.generic_recordings(
            plan.channel_slug, plan.daypart_slug, plan.weekly_collection_slug,
            maximum_duration=seconds - float(self.settings.get("ending_silence_seconds", 1.25)),
        )
        available = []
        for recording in recordings:
            failure = self._fallback_failures.get((str(self._output_dir()), opportunity_id, recording.identity))
            if failure and time.monotonic() - failure[0] < 60:
                reason = failure[1]
            else:
                available.append(recording)
        return available, reason

    def fallback_is_current(self, opportunity_id: int) -> bool:
        row = self.connection.execute(
            "SELECT fallback_path,fallback_mode,fallback_audio_identity,starts_at,ends_at FROM announcement_opportunities WHERE id=?",
            (opportunity_id,),
        ).fetchone()
        if row is None or not row["fallback_path"] or not Path(row["fallback_path"]).is_file():
            return False
        plan = self.service.build_opportunity_plan(opportunity_id)
        if plan.weekly_special_promo:
            return True
        recordings, _reason = self._fallback_selection(opportunity_id, plan)
        if (row["fallback_mode"] != ("generic" if recordings else "silent")
                or row["fallback_audio_identity"] != (recordings[0].identity if recordings else None)):
            return False
        path = Path(row["fallback_path"])
        try:
            stat = path.stat()
            seconds = (datetime.fromisoformat(row["ends_at"]) - datetime.fromisoformat(row["starts_at"])).total_seconds()
            key = (str(path), stat.st_size, stat.st_mtime_ns, seconds)
            if key not in self._fallback_video_validation:
                if len(self._fallback_video_validation) >= 256:
                    self._fallback_video_validation.clear()
                try:
                    self._validate(path, seconds)
                    self._fallback_video_validation[key] = True
                except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
                    self._fallback_video_validation[key] = False
            return self._fallback_video_validation[key]
        except OSError:
            return False

    def _render_fallback(self, opportunity_id, *, force=False, activate=True, ai_error=None):
        plan = self.service.build_opportunity_plan(opportunity_id)
        row = self.connection.execute(
            "SELECT cache_key,starts_at,ends_at FROM announcement_opportunities WHERE id=?", (opportunity_id,)
        ).fetchone()
        recordings, reason = self._fallback_selection(opportunity_id, plan)
        graphics = GraphicsService(self.connection, self.config)
        background = graphics.select(plan.channel_slug, "up-next", plan.starts_at,
                                     daypart_slug=plan.daypart_slug, weekly_collection_slug=plan.weekly_collection_slug)
        music = graphics.select_music(plan.channel_slug, "up-next", plan.starts_at,
                                      daypart_slug=plan.daypart_slug, weekly_collection_slug=plan.weekly_collection_slug)
        duration = max(1.0, (datetime.fromisoformat(row["ends_at"]) - datetime.fromisoformat(row["starts_at"])).total_seconds())
        # Each usable recording gets one attempt; silence is tried only after all fail.
        for recording in [*recordings, None]:
            kind = f"generic-{recording.identity[:16]}" if recording else "silent"
            target = self._output_dir() / f"opportunity-{opportunity_id}-{row['cache_key'][:12]}-v9-{kind}.mp4"
            try:
                result = self.render(
                    plan.channel_slug, plan.starts_at, output=target, use_tts=False, force=force,
                    exact_duration=duration, background=background, music=music, plan_override=plan,
                    audio_override=recording.path if recording else None, select_generic=False,
                )
            except Exception as exc:
                if recording is None:
                    raise
                reason = f"Generic audio could not be incorporated: {exc}"
                if len(self._fallback_failures) >= 256:
                    self._fallback_failures.clear()
                self._fallback_failures[(str(self._output_dir()), opportunity_id, recording.identity)] = (time.monotonic(), reason)
                continue
            mode = "generic" if recording else "silent"
            explanation = (f"AI failed: {ai_error}" if ai_error else None)
            if reason:
                explanation = "; ".join(filter(None, (explanation, reason)))
            if activate:
                self.connection.execute(
                    "UPDATE announcement_opportunities SET fallback_path=?,fallback_mode=?,fallback_audio_identity=?,"
                    "fallback_reason=?,render_status='ready',updated_at=? WHERE id=?",
                    (str(result.output), mode, recording.identity if recording else None, explanation,
                     datetime.now().astimezone().isoformat(timespec="seconds"), opportunity_id),
                )
                self.connection.commit()
            return replace(result, audio_mode=mode, fallback_reason=explanation)

    def _render_ai_opportunity(self, opportunity_id: int, *, force=False, activate=True):
        plan = self.service.build_opportunity_plan(opportunity_id)
        row = self.connection.execute(
            "SELECT cache_key,starts_at,ends_at,opportunity_type,facts_json "
            "FROM announcement_opportunities WHERE id=?",
            (opportunity_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Announcement opportunity {opportunity_id} does not exist")
        provider = None
        audio_override = None
        voice = None
        instructions = ""
        if plan.weekly_special_promo:
            weekly = self.profiles.scoped_profile(
                plan.channel_slug,
                weekly_collection_slug=plan.weekly_collection_slug,
            )
            if not weekly.promo_narration_enabled:
                raise RuntimeError("AI promo narration is disabled for this weekly special")
        profile = self.profiles.profile(
            plan.channel_slug, plan.daypart_slug, plan.weekly_collection_slug
        )
        if not profile.enabled:
            raise RuntimeError(
                f"AI announcer is disabled for {profile.channel_name}"
            )
        script_provider = self._script_provider or OpenAIScriptProvider(self._ai_client)
        if plan.weekly_special_promo:
            promo_provider = self._promo_script_provider or OpenAIWeeklyPromoScriptProvider(self._ai_client)
            profile = replace(profile, promo_prompt=weekly.promo_prompt)
            facts = json.loads(row["facts_json"])
            details = facts.get("weekly_special_promo", {})
            facts["weekly_special_promo"] = {
                **details,
                "airing_at_display": (
                    f"{plan.special_airing_at.strftime('%A, %B %d').replace(' 0', ' ')} "
                    f"at {self.service._display_time(plan.special_airing_at)}"
                ) if plan.special_airing_at else "",
            }
            spoken_text = promo_provider.generate(profile, facts)
        else:
            spoken_text = script_provider.generate(profile, plan)
        plan = replace(plan, spoken_text=spoken_text)
        provider = self._tts_provider or provider_for_profile(profile, self.config, self._ai_client)
        voice = profile_voice(profile)
        instructions = profile.voice_instructions
        target = self._output_dir() / (
            # v6 chooses presentation scope from the introduced program. Keep
            # the old render as a cache miss so existing opportunities receive
            # their corrected daypart or Weekly Special artwork.
            f"opportunity-{opportunity_id}-{row['cache_key'][:12]}-v8-narrated.mp4"
        )
        graphics = GraphicsService(self.connection, self.config)
        background = graphics.select(
            plan.channel_slug, "up-next", plan.starts_at,
            daypart_slug=plan.daypart_slug,
            weekly_collection_slug=plan.weekly_collection_slug,
        )
        music = graphics.select_music(
            plan.channel_slug, "up-next", plan.starts_at,
            daypart_slug=plan.daypart_slug,
            weekly_collection_slug=plan.weekly_collection_slug,
        )
        duration = max(1.0, (datetime.fromisoformat(row["ends_at"])
                            - datetime.fromisoformat(row["starts_at"])).total_seconds())
        result = self.render(
            plan.channel_slug,
            plan.starts_at,
            output=target,
            use_tts=True,
            force=force,
            exact_duration=duration,
            background=background,
            music=music,
            plan_override=plan,
            provider_override=provider,
            audio_override=audio_override,
            voice_override=voice,
            instructions=instructions,
        )
        if activate:
            self.connection.execute(
                "UPDATE announcement_opportunities SET validated_output_path=?, render_status='ready', detail=NULL, updated_at=? WHERE id=?",
                (str(result.output), datetime.now().astimezone().isoformat(timespec="seconds"), opportunity_id),
            )
            self.connection.commit()
        return result

    def _cache_path(
        self,
        plan: AnnouncementPlan,
        provider: TTSProvider | None,
        voice: str,
        rate: int,
        *,
        background: Path | None = None,
        music: Path | None = None,
        instructions: str = "",
        audio: Path | None = None,
    ) -> Path:
        identity = json.dumps(
            {
                "channel": plan.channel_slug,
                "starts_at": plan.starts_at.isoformat(),
                "lines": [
                    (line.starts_at.isoformat(), line.title) for line in plan.lines
                ],
                "tagline": plan.tagline,
                "spoken_text": plan.spoken_text,
                "color": plan.background_color,
                "voice": voice,
                "instructions": instructions,
                "rate": rate,
                "tts": provider.name if provider else "none",
                "tts_model": getattr(provider, "model", None),
                "background": self._file_identity(background),
                "music": self._file_identity(music),
                "audio": hashlib.sha256(audio.read_bytes()).hexdigest() if audio else None,
                "renderer": 4,
            },
            sort_keys=True,
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        return self._output_dir() / (
            f"{plan.channel_slug}-{plan.starts_at:%Y%m%d-%H%M}-{digest}.mp4"
        )

    def _provider(
        self,
        use_tts: bool,
        channel_slug: str,
        daypart_slug: str | None = None,
        weekly_collection_slug: str | None = None,
    ) -> TTSProvider | None:
        if not use_tts:
            return None
        if self._tts_provider is not None:
            return self._tts_provider
        configured = str(self.settings.get("tts_provider", "openai")).casefold()
        if configured in {"none", "off", "disabled"}:
            return None
        if configured not in {"auto", "openai", "cloud"}:
            raise RuntimeError(f"Configured TTS provider {configured!r} is unavailable")
        profile = self.profiles.profile(
            channel_slug, daypart_slug, weekly_collection_slug
        )
        if not profile.enabled:
            raise RuntimeError(f"AI announcer is disabled for {profile.channel_name}")
        return provider_for_profile(profile, self.config, self._ai_client)

    def _voice_settings(self, channel_slug: str) -> tuple[str, int]:
        channel = self.settings.get("channels", {}).get(channel_slug, {})
        voice = str(channel.get("voice", self.settings.get("voice", "")))
        rate = int(channel.get("voice_rate", self.settings.get("voice_rate", 0)))
        return voice, max(-10, min(10, rate))

    def _output_dir(self) -> Path:
        raw = Path(str(self.settings.get("output_dir", "./data/generated/announcements")))
        return raw if raw.is_absolute() else (self.config.project_root / raw).resolve()

    def _fonts_dir(self) -> Path:
        raw = Path(str(self.settings.get("fonts_dir", "assets/fonts")))
        return raw if raw.is_absolute() else (self.config.project_root / raw).resolve()

    def _audio_duration(self, path: Path) -> float:
        completed = hidden_run(
            [
                self.config.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return float(completed.stdout.strip())

    def _encode(
        self,
        work: Path,
        ass: Path,
        audio: Path | None,
        output: Path,
        color: str,
        duration: float,
        *,
        background: Path | None = None,
        music: Path | None = None,
    ) -> None:
        fonts = self._fonts_dir()
        if not fonts.is_dir():
            raise FileNotFoundError(f"Announcement fonts directory is missing: {fonts}")
        relative_fonts = os.path.relpath(fonts, work).replace("\\", "/")
        audio_input = (
            ["-i", str(audio)]
            if audio is not None
            else ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        )
        if background is None:
            video_input = [
                "-f", "lavfi", "-i", f"color=c={color}:s=640x480:r=30:d={duration:g}"
            ]
        elif background.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}:
            video_input = ["-loop", "1", "-framerate", "30", "-i", str(background)]
        else:
            video_input = ["-stream_loop", "-1", "-i", str(background)]
        music_input = (
            ["-stream_loop", "-1", "-i", str(music)]
            if music is not None else []
        )
        if music is None:
            audio_mapping = ["-map", "1:a", "-af", "apad"]
            filter_complex: list[str] = []
        else:
            volume = float(
                self.config.data.get("graphics", {}).get(
                    "up_next_music_volume", 0.16
                )
            )
            filter_complex = [
                "-filter_complex",
                f"[1:a]apad[speech];[2:a]volume={volume:g},apad[music];"
                "[speech][music]amix=inputs=2:duration=longest:normalize=0[mixed]",
            ]
            audio_mapping = ["-map", "[mixed]"]
        command = [
            self.config.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            *video_input,
            *audio_input,
            *music_input,
            "-vf",
            f"scale=640:480:force_original_aspect_ratio=increase,crop=640:480,subtitles={ass.name}:fontsdir={relative_fonts}",
            "-map",
            "0:v",
            *filter_complex,
            *audio_mapping,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-g",
            "30",
            "-keyint_min",
            "30",
            "-sc_threshold",
            "0",
            "-bf",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-t",
            f"{duration:g}",
            str(output),
        ]
        completed = hidden_run(
            command,
            cwd=work,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or "Announcement render failed")

    @staticmethod
    def _file_identity(path: Path | None) -> tuple[str, int, int] | None:
        if path is None or not path.is_file():
            return None
        stat = path.stat()
        return str(path), stat.st_size, stat.st_mtime_ns

    def _validate(self, path: Path, expected_duration: float | None = None) -> float:
        completed = hidden_run(
            [
                self.config.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=width,height,codec_type",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        payload = json.loads(completed.stdout)
        video = next(
            stream for stream in payload["streams"] if stream["codec_type"] == "video"
        )
        if video.get("width") != 640 or video.get("height") != 480:
            raise RuntimeError("Announcement output is not 640x480")
        if not any(stream["codec_type"] == "audio" for stream in payload["streams"]):
            raise RuntimeError("Announcement output has no audio stream")
        actual = float(payload["format"]["duration"])
        if expected_duration is not None and abs(actual - expected_duration) > 0.25:
            raise RuntimeError(
                f"Announcement duration {actual:.3f}s differs from expected "
                f"{expected_duration:.3f}s"
            )
        return actual

    def _ass(self, plan: AnnouncementPlan, duration: float) -> str:
        end = self._ass_time(duration)
        if plan.weekly_special_promo:
            name = plan.special_name or "Weekly Special"
            airing = plan.special_airing_at
            date_label = (
                f"{airing.strftime('%A, %B %d').replace(' 0', ' ')}  ·  "
                f"{self.service._display_time(airing)}"
                if airing else ""
            )
            days_until = (airing.date() - plan.starts_at.date()).days if airing else 7
            invitation = ("TONIGHT" if days_until == 0 else
                          "TOMORROW" if days_until == 1 else "LATER THIS WEEK")
            heading = f"{invitation}, JOIN US FOR {name.upper()}!"
            heading_size = max(17, min(26, 750 // max(1, len(heading))))
            events = [
                f"Dialogue: 0,0:00:00.00,{end},Header,,0,0,0,,"
                f"{{\\fs{heading_size}\\pos(320,155)}}{self._escape(heading)}",
                f"Dialogue: 0,0:00:00.00,{end},Tagline,,0,0,0,,"
                f"{{\\fs21\\pos(320,225)}}{self._escape(date_label)}",
            ]
            for index, line in enumerate(plan.lines[:2]):
                events.append(
                    f"Dialogue: 0,0:00:00.00,{end},Listing,,0,0,0,,"
                    f"{{\\fs21\\pos(320,{305 + index * 34})}}{self._escape(line.title)}"
                )
            if plan.promo_lineup_more:
                y = 305 + min(2, len(plan.lines)) * 34
                events.append(
                    f"Dialogue: 0,0:00:00.00,{end},Tagline,,0,0,0,,"
                    f"{{\\fs18\\pos(320,{y})}}…AND MORE"
                )
            return self._ass_document(events)
        heading = (
            f"COMING UP NEXT ON {plan.channel_name.upper()}..."
            if plan.lines else plan.channel_name.upper()
        )
        heading_size = max(16, min(28, 830 // max(1, len(heading))))
        events = [
            f"Dialogue: 0,0:00:00.00,{end},Header,,0,0,0,,"
            f"{{\\fs{heading_size}\\pos(320,112)}}{self._escape(heading)}"
        ]
        count = len(plan.lines)
        spacing = 64 if count <= 3 else max(44, 160 // max(1, count - 1))
        start_y = 188 if count <= 3 else 158
        for index, line in enumerate(plan.lines):
            value = f"{line.title}  -  {self.service._display_time(line.starts_at)}"
            size = max(16, min(24, 800 // max(1, len(value))))
            events.append(
                f"Dialogue: 0,0:00:00.00,{end},Listing,,0,0,0,,"
                f"{{\\fs{size}\\pos(320,{start_y + index * spacing})}}{self._escape(value)}"
            )
        tagline_size = max(15, min(20, 800 // max(1, len(plan.tagline))))
        events.append(
            f"Dialogue: 0,0:00:00.00,{end},Tagline,,0,0,0,,"
            f"{{\\fs{tagline_size}\\pos(320,390)}}{self._escape(plan.tagline)}"
        )
        return self._ass_document(events)

    def _ass_document(self, events: list[str]) -> str:
        font_name = "VCR OSD Mono" if (self._fonts_dir() / "VCR_OSD_MONO_1.001.ttf").exists() else "DejaVu Sans Mono"
        return "\n".join(
            [
                "[Script Info]",
                "ScriptType: v4.00+",
                "PlayResX: 640",
                "PlayResY: 480",
                "WrapStyle: 2",
                "ScaledBorderAndShadow: yes",
                "",
                "[V4+ Styles]",
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
                "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
                "Alignment, MarginL, MarginR, MarginV, Encoding",
                f"Style: Header,{font_name},28,&H00FFFFFF,&H00FFFFFF,&H00202020,"
                "&H80000000,-1,0,0,0,100,100,0,0,1,2,1,5,64,64,20,1",
                f"Style: Listing,{font_name},24,&H00FFFFFF,&H00FFFFFF,&H00202020,"
                "&H80000000,0,0,0,0,100,100,0,0,1,2,1,5,64,64,20,1",
                f"Style: Tagline,{font_name},20,&H00FFFFFF,&H00FFFFFF,&H00202020,"
                "&H80000000,-1,0,0,0,100,100,0,0,1,2,1,5,64,64,20,1",
                "",
                "[Events]",
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
                "Effect, Text",
                *events,
                "",
            ]
        )

    @staticmethod
    def _escape(value: str) -> str:
        return (
            value.replace("\\", r"\\")
            .replace("{", "(")
            .replace("}", ")")
            .replace("\n", " ")
        )

    @staticmethod
    def _ass_time(seconds: float) -> str:
        centiseconds = max(0, round(seconds * 100))
        hours, remainder = divmod(centiseconds, 360_000)
        minutes, remainder = divmod(remainder, 6_000)
        whole_seconds, fraction = divmod(remainder, 100)
        return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"
from retrotv.processes import hidden_run

