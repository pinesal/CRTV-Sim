from __future__ import annotations


import hashlib
import json
import os
import re
import ssl
import subprocess
import urllib.error
import urllib.request
import wave
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


class OpenAIProviderError(RuntimeError):
    pass


def _native_trust_context() -> ssl.SSLContext | None:
    """Use the operating system's certificate-chain engine when available."""
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except (ImportError, OSError, RuntimeError):
        return None


def verified_ssl_context() -> ssl.SSLContext:
    """Build a verified TLS context suitable for a frozen portable runtime.

    Prefer the native Windows CryptoAPI chain builder because it can use local
    roots and retrieve missing intermediates. If that provider is unavailable,
    seed OpenSSL with certifi's portable Mozilla roots and merge the Windows root
    and intermediate stores. Certificate verification is never disabled.
    """
    native = _native_trust_context()
    if native is not None:
        return native
    try:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
    except (ImportError, OSError):
        context = ssl.create_default_context()
    enumerate_certificates = getattr(ssl, "enum_certificates", None)
    if enumerate_certificates is None:
        return context
    roots: list[str] = []
    for store in ("ROOT", "CA"):
        try:
            certificates = enumerate_certificates(store)
        except OSError:
            continue
        for certificate, encoding, _trust in certificates:
            if encoding == "x509_asn":
                roots.append(ssl.DER_cert_to_PEM_cert(certificate))
    if roots:
        context.load_verify_locations(cadata="".join(roots))
    return context


def configured_openai_api_key() -> str:
    """Resolve environment overrides, then Windows Credential Manager."""
    from retrotv.credentials import configured_openai_api_key as resolve
    return resolve()



@dataclass(frozen=True)
class ChannelAnnouncementProfile:
    channel_id: int
    channel_slug: str
    channel_name: str
    enabled: bool
    script_model: str
    speech_model: str
    voice: str
    personality_prompt: str
    voice_instructions: str
    generic_script: str
    maximum_words: int
    profile_version: int
    daypart_slug: str | None = None
    weekly_collection_slug: str | None = None
    override_enabled: bool = True
    tagline: str | None = None
    spoken_tagline: str | None = None
    promo_enabled: bool = True
    promo_narration_enabled: bool = True
    promo_prompt: str = ""
    tts_provider: str = "openai"
    breeze_speech_model: str = "breeze-tts-2"
    breeze_voice_id: str = ""

    @property
    def profile_scope(self) -> str:
        if self.weekly_collection_slug:
            return "weekly"
        if self.daypart_slug:
            return "daypart"
        return "channel"

    @property
    def fingerprint(self) -> str:
        payload = {
            "channel": self.channel_slug,
            "daypart": self.daypart_slug,
            "weekly_collection": self.weekly_collection_slug,
            "override_enabled": self.override_enabled,
            "channel_name": self.channel_name,
            "tagline": self.tagline,
            "spoken_tagline": self.spoken_tagline,
            "promo_narration_enabled": self.promo_narration_enabled,
            "promo_prompt": self.promo_prompt,
            "enabled": self.enabled,
            "script_model": self.script_model,
            "tts_provider": self.tts_provider,
            "breeze_speech_model": self.breeze_speech_model,
            "breeze_voice_id": self.breeze_voice_id,
            "speech_model": self.speech_model,
            "voice": self.voice,
            "personality_prompt": self.personality_prompt,
            "voice_instructions": self.voice_instructions,
            "generic_script": self.generic_script,
            "maximum_words": self.maximum_words,
            "profile_version": self.profile_version,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()

    @property
    def generic_fingerprint(self) -> str:
        """Identity of the prerecorded audio, independent of dynamic AI policy."""
        payload = {
            "script": self.generic_script,
            "provider": self.tts_provider,
            "model": self.breeze_speech_model if self.tts_provider == "breeze" else self.speech_model,
            "voice": profile_voice(self),
            "instructions": self.voice_instructions,
        }
        return "generic-v1:" + hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class GenericRecording:
    path: Path
    identity: str
    duration_seconds: float


# Key by file metadata, not profile state. Avoid re-probing on every demand poll.
_generic_validation_cache: dict[tuple, tuple[GenericRecording | None, str | None]] = {}


class OpenAIHTTPClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 90,
        ssl_context: ssl.SSLContext | None = None,
    ):
        self.api_key = api_key or configured_openai_api_key()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.ssl_context = ssl_context or verified_ssl_context()
        backend_module = type(self.ssl_context).__module__.split(".", 1)[0]
        if backend_module == "truststore":
            self.ssl_backend = (
                "Windows system certificate store"
                if os.name == "nt"
                else "operating-system certificate store"
            )
        elif backend_module == "ssl":
            self.ssl_backend = "bundled and system certificates"
        else:
            self.ssl_backend = backend_module

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def request_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        raw = self._request(path, payload)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenAIProviderError("OpenAI returned invalid JSON") from exc

    def request_bytes(self, path: str, payload: dict[str, Any]) -> bytes:
        return self._request(path, payload)

    def _request(self, path: str, payload: dict[str, Any]) -> bytes:
        if not self.api_key:
            raise OpenAIProviderError("OpenAI API key is not configured; add it in Settings")
        request = urllib.request.Request(
            f"{self.base_url}/{path.lstrip('/')}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "CRTV-Sim/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
                context=self.ssl_context,
            ) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-1500:]
            try:
                message = json.loads(detail).get("error", {}).get("message")
            except json.JSONDecodeError:
                message = None
            if message:
                message = str(message).replace(self.api_key, "[redacted]")
                message = re.sub(r"sk-[A-Za-z0-9_*.-]+", "[redacted]", message)
            raise OpenAIProviderError(
                message or f"OpenAI request failed with HTTP {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise OpenAIProviderError(
                f"Unable to reach OpenAI using {self.ssl_backend} TLS trust: {exc.reason}"
            ) from exc


class OpenAIScriptProvider:
    name = "openai"

    def __init__(self, client: OpenAIHTTPClient):
        self.client = client

    def generate(self, profile: ChannelAnnouncementProfile, plan) -> str:
        schedule = "\n".join(
            f"- {line.starts_at.strftime('%-I:%M %p') if os.name != 'nt' else self._windows_time(line.starts_at)} - {line.title}"
            for line in plan.lines
            if line.title != "TBD"
        )
        if not schedule:
            return profile.generic_script.strip()
        personality = profile.personality_prompt.strip() or (
            f"You are a station announcer for {profile.channel_name} in the 1990s. "
            "Speak in a period-appropriate manner for a coming-up-next station segment."
        )
        prompt = (
            f"{personality}\n\n"
            f"Write only the spoken announcement. Keep it under {profile.maximum_words} words. "
            "You may naturally combine repeated shows, omit times, or summarize the lineup. "
            "Do not add commentary outside the announcement.\n\n"
            f"Upcoming schedule:\n{schedule}"
        )
        payload = {
            "model": profile.script_model,
            "input": prompt,
            "max_output_tokens": 220,
        }
        response = self.client.request_json("responses", payload)
        text = self._output_text(response).strip()
        text = re.sub(r"^```(?:text)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
        if not text:
            raise OpenAIProviderError("OpenAI generated an empty announcement")
        return text

    @staticmethod
    def _windows_time(value: datetime) -> str:
        return f"{value.strftime('%I').lstrip('0')}:{value:%M} {value:%p}"

    @staticmethod
    def _output_text(payload: dict[str, Any]) -> str:
        if isinstance(payload.get("output_text"), str):
            return str(payload["output_text"])
        parts: list[str] = []
        for item in payload.get("output", []):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if isinstance(content, dict) and content.get("type") == "output_text":
                    parts.append(str(content.get("text", "")))
        if not parts:
            raise OpenAIProviderError("OpenAI response contained no announcement text")
        return "".join(parts)


class OpenAIWeeklyPromoScriptProvider:
    """Improvise short promo dialogue from supplied weekly-special facts only."""

    def __init__(self, client: OpenAIHTTPClient):
        self.client = client

    def generate(self, profile: ChannelAnnouncementProfile, facts: dict[str, Any]) -> str:
        special = facts.get("weekly_special_promo", {})
        lineup = [str(value) for value in facts.get("selected_lineup", []) if str(value).strip()]
        fact_lines = [
            f"Special name: {special.get('special_name', 'Weekly Special')}",
            f"Channel: {profile.channel_name}",
            f"Scheduled date and time: {special.get('airing_at_display', '')}",
            "Known lineup: " + ("; ".join(lineup) if lineup else "No lineup details are available"),
            f"More scheduled titles exist: {'yes' if facts.get('lineup_more') else 'no'}",
        ]
        personality = profile.personality_prompt.strip() or (
            f"You are the announcer for {profile.channel_name}."
        )
        direction = profile.promo_prompt.strip()
        prompt = (
            f"{personality}\n\n"
            + (f"Promo creative direction: {direction}\n\n" if direction else "")
            + "Improvise only the spoken dialogue for a short television promo. "
            f"Keep it under {profile.maximum_words} words. Use only the facts below. "
            "Do not invent or infer programs, episode details, dates, times, channel claims, "
            "or other promises. You may omit lineup details, but do not add details not listed. "
            "Return only the dialogue, with no labels or explanation.\n\nFacts:\n"
            + "\n".join(f"- {line}" for line in fact_lines)
        )
        response = self.client.request_json("responses", {
            "model": profile.script_model,
            "input": prompt,
            "max_output_tokens": 220,
        })
        text = OpenAIScriptProvider._output_text(response).strip()
        text = re.sub(r"^```(?:text)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
        if not text:
            raise OpenAIProviderError("OpenAI generated an empty weekly-special promo")
        return text


class OpenAITTSProvider:
    name = "openai"

    def __init__(self, client: OpenAIHTTPClient, *, model: str):
        self.client = client
        self.model = model

    def synthesize(
        self,
        text: str,
        output: Path,
        *,
        voice: str,
        rate: int = 0,
        instructions: str = "",
    ) -> None:
        payload: dict[str, Any] = {
            "model": self.model,
            "voice": voice or "alloy",
            "input": text,
            "response_format": "wav",
        }
        if instructions.strip():
            payload["instructions"] = instructions.strip()
        # Preserve the legacy rate setting as a modest speed adjustment.
        if rate:
            payload["speed"] = max(0.75, min(1.35, 1.0 + rate * 0.035))
        content = self.client.request_bytes("audio/speech", payload)
        if not content:
            raise OpenAIProviderError("OpenAI returned empty speech audio")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)


class AnnouncementProfileService:
    def __init__(self, connection, config):
        self.connection = connection
        self.config = config
        self.settings = config.data.get("announcements", {})

    def _weekly_collections(self) -> dict[tuple[str, str], str]:
        """Return enabled weekly collections keyed by channel and collection."""
        raw = self.config.data.get("seasonal", {}).get("windows", {})
        windows = raw.values() if isinstance(raw, dict) else raw
        result: dict[tuple[str, str], str] = {}
        for item in windows:
            if not isinstance(item, dict) or not bool(item.get("enabled", False)):
                continue
            channel = str(item.get("channel", "")).strip().casefold()
            collection = str(
                item.get("collection", item.get("slug", ""))
            ).strip().casefold()
            if not channel or not collection:
                continue
            result.setdefault(
                (channel, collection),
                str(item.get("guide_label", collection)).strip() or collection,
            )
        return result

    def ensure_profiles(self) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        channels = self.connection.execute(
            "SELECT id,slug,display_name FROM channels WHERE enabled=1"
        ).fetchall()
        configured = self.settings.get("channels", {})
        for channel in channels:
            values = configured.get(channel["slug"], {})
            personality = str(values.get("personality_prompt", "")).strip() or (
                f"You are a station announcer for {channel['display_name']} in the 1990s. "
                "Speak in a period-appropriate manner for a coming-up-next station segment. "
                "Be lively, concise, and true to the station identity."
            )
            generic = str(values.get("generic_script", "")).strip() or (
                f"You're watching {channel['display_name']}. Don't change the channel - "
                "more great programming is coming up!"
            )
            self.connection.execute(
                """
                INSERT INTO announcement_channel_profiles(
                    channel_id,enabled,script_model,speech_model,voice,
                    personality_prompt,voice_instructions,generic_script,
                    maximum_words,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(channel_id) DO NOTHING
                """,
                (
                    int(channel["id"]),
                    int(bool(values.get("ai_enabled", False))),
                    str(values.get("script_model", self.settings.get("script_model", "gpt-5.6-luna"))),
                    str(values.get("speech_model", self.settings.get("speech_model", "gpt-4o-mini-tts"))),
                    str(values.get("voice", self.settings.get("voice", "alloy"))) or "alloy",
                    personality,
                    str(values.get("voice_instructions", "")),
                    generic,
                    int(values.get("maximum_words", self.settings.get("maximum_words", 55))),
                    now,
                ),
            )
            overrides = self.config.data.get("daypart_branding", {}).get(
                str(channel["slug"]), {}
            )
            if isinstance(overrides, dict):
                for daypart_slug, override in overrides.items():
                    if not isinstance(override, dict):
                        continue
                    identity = str(
                        override.get("display_name", channel["display_name"])
                    ).strip() or str(channel["display_name"])
                    personality = str(
                        override.get(
                            "personality_prompt",
                            f"You are a station announcer for {identity} in the 1990s.",
                        )
                    ).strip()
                    generic = str(
                        override.get(
                            "generic_script",
                            f"You're watching {identity}. More great programming is coming up!",
                        )
                    ).strip()
                    self.connection.execute(
                        """
                        INSERT INTO announcement_daypart_profiles(
                            channel_id,daypart_slug,identity_name,enabled,
                            script_model,speech_model,voice,personality_prompt,
                            voice_instructions,generic_script,maximum_words,updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(channel_id,daypart_slug) DO NOTHING
                        """,
                        (
                            int(channel["id"]), str(daypart_slug).strip().casefold(),
                            identity, int(bool(override.get("ai_enabled", False))),
                            str(override.get("script_model", self.settings.get("script_model", "gpt-5.6-luna"))),
                            str(override.get("speech_model", self.settings.get("speech_model", "gpt-4o-mini-tts"))),
                            str(override.get("voice", self.settings.get("voice", "alloy"))) or "alloy",
                            personality, str(override.get("voice_instructions", "")),
                            generic,
                            int(override.get("maximum_words", self.settings.get("maximum_words", 55))),
                            now,
                        ),
                    )
        channels_by_slug = {str(row["slug"]): row for row in channels}
        for (channel_slug, collection_slug), label in self._weekly_collections().items():
            channel = channels_by_slug.get(channel_slug)
            if channel is None:
                continue
            values = configured.get(channel_slug, {})
            identity = label
            personality = (
                f"You are the announcer for {identity}, a weekly programming block "
                f"on {channel['display_name']} in the 1990s. Be lively, concise, and "
                "true to the block's identity."
            )
            generic = (
                f"You're watching {identity} on {channel['display_name']}. "
                "More great programming is coming up!"
            )
            self.connection.execute(
                """
                INSERT INTO announcement_weekly_profiles(
                    channel_id,collection_slug,override_enabled,identity_name,
                    tagline,spoken_tagline,enabled,script_model,speech_model,
                    voice,personality_prompt,voice_instructions,generic_script,
                    maximum_words,updated_at
                ) VALUES (?,?,0,?,'','',?,?,?,?,?,?,?,?,?)
                ON CONFLICT(channel_id,collection_slug) DO NOTHING
                """,
                (
                    int(channel["id"]), collection_slug, identity,
                    int(bool(values.get("ai_enabled", False))),
                    str(values.get("script_model", self.settings.get("script_model", "gpt-5.6-luna"))),
                    str(values.get("speech_model", self.settings.get("speech_model", "gpt-4o-mini-tts"))),
                    str(values.get("voice", self.settings.get("voice", "alloy"))) or "alloy",
                    personality, str(values.get("voice_instructions", "")), generic,
                    int(values.get("maximum_words", self.settings.get("maximum_words", 55))),
                    now,
                ),
            )
        self.connection.commit()

    def profiles(self) -> list[ChannelAnnouncementProfile]:
        self.ensure_profiles()
        rows = self.connection.execute(
            """
            SELECT p.*,c.slug,c.display_name FROM announcement_channel_profiles p
            JOIN channels c ON c.id=p.channel_id WHERE c.enabled=1
            ORDER BY c.channel_number IS NULL,c.channel_number,c.display_name
            """
        ).fetchall()
        result = [self._profile(row) for row in rows]
        override_rows = self.connection.execute(
            """
            SELECT p.*,c.slug,p.identity_name AS display_name
            FROM announcement_daypart_profiles p
            JOIN channels c ON c.id=p.channel_id WHERE c.enabled=1
            ORDER BY c.channel_number IS NULL,c.channel_number,c.display_name,
                     p.daypart_slug
            """
        ).fetchall()
        result.extend(self._profile(row) for row in override_rows)
        configured_weekly = set(self._weekly_collections())
        weekly_rows = self.connection.execute(
            """
            SELECT p.*,c.slug,p.identity_name AS display_name
            FROM announcement_weekly_profiles p
            JOIN channels c ON c.id=p.channel_id WHERE c.enabled=1
            ORDER BY c.channel_number IS NULL,c.channel_number,c.display_name,
                     p.collection_slug
            """
        ).fetchall()
        result.extend(
            self._profile(row)
            for row in weekly_rows
            if (str(row["slug"]), str(row["collection_slug"])) in configured_weekly
        )
        return result

    def profile(
        self,
        channel_slug: str,
        daypart_slug: str | None = None,
        weekly_collection_slug: str | None = None,
    ) -> ChannelAnnouncementProfile:
        """Resolve the active profile with weekly, daypart, then channel fallback."""
        self.ensure_profiles()
        if weekly_collection_slug:
            weekly = self._weekly_profile(channel_slug, weekly_collection_slug)
            if weekly is not None and weekly.override_enabled:
                return weekly
        if daypart_slug:
            daypart = self._daypart_profile(channel_slug, daypart_slug)
            if daypart is not None:
                return daypart
        return self._channel_profile(channel_slug)

    def scoped_profile(
        self,
        channel_slug: str,
        daypart_slug: str | None = None,
        weekly_collection_slug: str | None = None,
    ) -> ChannelAnnouncementProfile:
        """Return the profile card for an exact scope, including disabled overrides."""
        self.ensure_profiles()
        if weekly_collection_slug:
            profile = self._weekly_profile(channel_slug, weekly_collection_slug)
            if profile is None:
                raise ValueError(
                    f"Weekly announcer profile for {channel_slug!r}/"
                    f"{weekly_collection_slug!r} was not found"
                )
            return profile
        if daypart_slug:
            profile = self._daypart_profile(channel_slug, daypart_slug)
            if profile is None:
                raise ValueError(
                    f"Daypart announcer profile for {channel_slug!r}/"
                    f"{daypart_slug!r} was not found"
                )
            return profile
        return self._channel_profile(channel_slug)

    def _channel_profile(self, channel_slug: str) -> ChannelAnnouncementProfile:
        row = self.connection.execute(
            """
            SELECT p.*,c.slug,c.display_name FROM announcement_channel_profiles p
            JOIN channels c ON c.id=p.channel_id WHERE c.slug=?
            """,
            (channel_slug,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Announcement profile for {channel_slug!r} was not found")
        return self._profile(row)

    def _daypart_profile(
        self, channel_slug: str, daypart_slug: str
    ) -> ChannelAnnouncementProfile | None:
        row = self.connection.execute(
            """
            SELECT p.*,c.slug,p.identity_name AS display_name
            FROM announcement_daypart_profiles p
            JOIN channels c ON c.id=p.channel_id
            WHERE c.slug=? AND p.daypart_slug=?
            """,
            (channel_slug, daypart_slug.strip().casefold()),
        ).fetchone()
        return self._profile(row) if row is not None else None

    def _weekly_profile(
        self, channel_slug: str, collection_slug: str
    ) -> ChannelAnnouncementProfile | None:
        row = self.connection.execute(
            """
            SELECT p.*,c.slug,p.identity_name AS display_name
            FROM announcement_weekly_profiles p
            JOIN channels c ON c.id=p.channel_id
            WHERE c.slug=? AND p.collection_slug=?
            """,
            (channel_slug, collection_slug.strip().casefold()),
        ).fetchone()
        return self._profile(row) if row is not None else None

    def update(
        self,
        channel_slug: str,
        payload: dict[str, Any],
        daypart_slug: str | None = None,
        weekly_collection_slug: str | None = None,
    ) -> ChannelAnnouncementProfile:
        current = self.scoped_profile(
            channel_slug, daypart_slug, weekly_collection_slug
        )
        # Upgrade a known-current legacy identity before profile_version changes.
        table, key_column, key_value = self._generic_target(current)
        self.connection.execute(
            f"UPDATE {table} SET profile_fingerprint=? WHERE {key_column}=? AND profile_fingerprint=?",
            (current.generic_fingerprint, key_value, current.fingerprint),
        )
        maximum_words = max(10, min(150, int(payload.get("maximum_words", current.maximum_words))))
        script_model = str(payload.get("script_model", current.script_model)).strip()
        speech_model = str(payload.get("speech_model", current.speech_model)).strip()
        voice = str(payload.get("voice", current.voice)).strip().casefold()
        if not script_model:
            raise ValueError("Script model cannot be blank")
        if not speech_model:
            raise ValueError("TTS model cannot be blank")
        if not voice:
            raise ValueError("Voice cannot be blank")
        provider = str(payload.get("tts_provider", current.tts_provider)).strip()
        breeze_model = str(payload.get("breeze_speech_model", current.breeze_speech_model)).strip()
        breeze_voice = str(payload.get("breeze_voice_id", current.breeze_voice_id)).strip()
        if provider not in {"openai", "breeze"}:
            raise ValueError("Choose OpenAI or Breeze Blue as the TTS provider")
        if not breeze_model or (provider == "breeze" and not breeze_voice):
            raise ValueError("Choose a Breeze model and voice")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        values = (
            int(bool(payload.get("enabled", current.enabled))), script_model,
            speech_model, voice,
            str(payload.get("personality_prompt", current.personality_prompt)).strip(),
            str(payload.get("voice_instructions", current.voice_instructions)).strip(),
            str(payload.get("generic_script", current.generic_script)).strip(),
            maximum_words, now,
        )
        if current.weekly_collection_slug:
            identity_name = str(
                payload.get("identity_name", current.channel_name)
            ).strip()
            if not identity_name:
                raise ValueError("Weekly identity name cannot be blank")
            promo_prompt = str(payload.get("promo_prompt", current.promo_prompt)).strip()
            if len(promo_prompt) > 2000:
                raise ValueError("Promo narration direction must be 2000 characters or less")
            self.connection.execute(
                """
                UPDATE announcement_weekly_profiles SET
                    override_enabled=?,identity_name=?,tagline=?,spoken_tagline=?,
                    enabled=?,script_model=?,speech_model=?,voice=?,personality_prompt=?,
                    voice_instructions=?,generic_script=?,maximum_words=?,
                    promo_enabled=?,promo_narration_enabled=?,promo_prompt=?,
                    profile_version=profile_version+1,updated_at=?
                WHERE channel_id=? AND collection_slug=?
                """,
                (
                    int(bool(payload.get("override_enabled", current.override_enabled))),
                    identity_name,
                    str(payload.get("tagline", current.tagline or "")).strip(),
                    str(payload.get("spoken_tagline", current.spoken_tagline or "")).strip(),
                    *values[:-1],
                    int(bool(payload.get("promo_enabled", current.promo_enabled))),
                    int(bool(payload.get("promo_narration_enabled", current.promo_narration_enabled))),
                    promo_prompt,
                    values[-1],
                    current.channel_id,
                    current.weekly_collection_slug,
                ),
            )
            self.connection.execute(
                """
                UPDATE announcement_weekly_generic_audio SET
                    updated_at=?
                WHERE profile_id=(SELECT id FROM announcement_weekly_profiles
                    WHERE channel_id=? AND collection_slug=?)
                """,
                (now, current.channel_id, current.weekly_collection_slug),
            )
        elif current.daypart_slug:
            self.connection.execute(
                """
                UPDATE announcement_daypart_profiles SET
                    enabled=?,script_model=?,speech_model=?,voice=?,personality_prompt=?,
                    voice_instructions=?,generic_script=?,maximum_words=?,
                    profile_version=profile_version+1,updated_at=?
                WHERE channel_id=? AND daypart_slug=?
                """,
                (*values, current.channel_id, current.daypart_slug),
            )
            self.connection.execute(
                """
                UPDATE announcement_daypart_generic_audio SET
                    updated_at=?
                WHERE profile_id=(SELECT id FROM announcement_daypart_profiles
                    WHERE channel_id=? AND daypart_slug=?)
                """,
                (now, current.channel_id, current.daypart_slug),
            )
        else:
            self.connection.execute(
                """
                UPDATE announcement_channel_profiles SET
                    enabled=?,script_model=?,speech_model=?,voice=?,personality_prompt=?,
                    voice_instructions=?,generic_script=?,maximum_words=?,
                    profile_version=profile_version+1,updated_at=? WHERE channel_id=?
                """,
                (*values, current.channel_id),
            )
            self.connection.execute(
                "UPDATE announcement_generic_audio SET updated_at=? WHERE channel_id=?",
                (now, current.channel_id),
            )
        table = ("announcement_weekly_profiles" if current.weekly_collection_slug else
                 "announcement_daypart_profiles" if current.daypart_slug else "announcement_channel_profiles")
        condition, identity = ("collection_slug=?", current.weekly_collection_slug) if current.weekly_collection_slug else ("daypart_slug=?", current.daypart_slug)
        where = "channel_id=?" + (" AND " + condition if identity else "")
        self.connection.execute(f"UPDATE {table} SET tts_provider=?,breeze_speech_model=?,breeze_voice_id=? WHERE {where}",
                                (provider, breeze_model, breeze_voice, current.channel_id, *((identity,) if identity else ())))
        scope_sql = ""
        scope_values: tuple[Any, ...] = ()
        if current.weekly_collection_slug:
            # Opportunities created before migration 024 have no persisted
            # weekly scope. Include those legacy rows so they cannot retain an
            # old voice after a weekly profile is first configured.
            scope_sql = " AND (weekly_collection_slug=? OR weekly_collection_slug IS NULL)"
            scope_values = (current.weekly_collection_slug,)
        self.connection.execute(
            f"""
            UPDATE announcement_opportunities
            SET validated_output_path=NULL,render_status='pending',
                detail='Station announcer profile changed',updated_at=?
            WHERE channel_id=? AND starts_at>=?{scope_sql}
            """,
            (now, current.channel_id, now, *scope_values),
        )
        self.connection.execute(
            f"""
            UPDATE announcement_jobs SET status='stale',
                detail='Station announcer profile changed',completed_at=?
            WHERE status='ready' AND opportunity_id IN (
                SELECT id FROM announcement_opportunities
                WHERE channel_id=? AND starts_at>=?{scope_sql}
            )
            """,
            (now, current.channel_id, now, *scope_values),
        )
        self.connection.commit()
        updated = self.scoped_profile(
            channel_slug, current.daypart_slug, current.weekly_collection_slug
        )
        table, key_column, key_value = self._generic_target(updated)
        self.connection.execute(
            f"UPDATE {table} SET status='stale' WHERE {key_column}=? "
            "AND status='ready' AND profile_fingerprint<>?",
            (key_value, updated.generic_fingerprint),
        )
        self.connection.commit()
        return updated

    def generic_audio(
        self,
        channel_slug: str,
        daypart_slug: str | None = None,
        weekly_collection_slug: str | None = None,
    ) -> Path | None:
        recordings, _reason = self.generic_recordings(
            channel_slug, daypart_slug, weekly_collection_slug
        )
        return recordings[0].path if recordings else None

    def validate_generic(self, path: Path, maximum_duration=None) -> tuple[GenericRecording | None, str | None]:
        maximum = (float(self.settings.get("opportunity_duration_seconds", 30)) - float(
            self.settings.get("ending_silence_seconds", 1.25)
        )) if maximum_duration is None else float(maximum_duration)
        try:
            stat = path.stat()
            key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns, maximum)
        except OSError:
            return None, "Generic recording is missing"
        if key in _generic_validation_cache:
            return _generic_validation_cache[key]
        try:
            try:
                with wave.open(str(path), "rb") as audio:
                    frames = audio.getnframes()
                    rate = audio.getframerate()
                    frame_bytes = audio.getnchannels() * audio.getsampwidth()
                    if rate <= 0 or frame_bytes <= 0:
                        raise ValueError("Generic WAV has invalid audio parameters")
                    duration = frames / float(rate)
                    streamed = frames == 0xFFFFFFFF // frame_bytes
                    # Reading PCM also detects truncated payloads, without spawning FFmpeg.
                    expected = frames * frame_bytes
                    actual = 0
                    while chunk := audio.readframes(65536):
                        actual += len(chunk)
                        if actual > (maximum + 1) * rate * frame_bytes:
                            raise ValueError("Generic narration exceeds the screen duration")
                    if actual % frame_bytes or (actual != expected and not streamed):
                        raise ValueError("Generic WAV is truncated")
                    if streamed:
                        duration = actual / float(rate * frame_bytes)
            except wave.Error:
                probe = hidden_run(
                    [self.config.ffprobe, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                    capture_output=True, text=True, timeout=30, check=True,
                )
                duration = float(probe.stdout.strip())
                hidden_run(
                    [self.config.ffmpeg, "-v", "error", "-xerror", "-i", str(path),
                     "-map", "0:a:0", "-f", "null", "-"],
                    capture_output=True, timeout=30, check=True,
                )
            if not 0 < duration <= maximum:
                raise ValueError(f"Generic narration must be between 0 and {maximum:g} seconds")
            with path.open("rb") as source:
                identity = hashlib.file_digest(source, "sha256").hexdigest()
            result = (GenericRecording(path.resolve(), identity, duration), None)
        except (OSError, ValueError, EOFError, subprocess.SubprocessError) as exc:
            result = (None, f"Generic recording is unusable: {exc}")
        if len(_generic_validation_cache) >= 256:
            _generic_validation_cache.clear()
        _generic_validation_cache[key] = result
        return result

    def _usable_generic(self, profile: ChannelAnnouncementProfile, maximum_duration=None):
        row = self._generic_row(profile, ready_only=False)
        paths = []
        if row is not None and row["audio_path"]:
            stored = Path(row["audio_path"])
            # Prefer the copy under the current installation after a portable move.
            paths.append(self._generic_dir() / stored.name)
            if stored.is_absolute():
                paths.append(stored)
        scope = (f"-weekly-{profile.weekly_collection_slug}" if profile.weekly_collection_slug
                 else f"-{profile.daypart_slug}" if profile.daypart_slug else "")
        for fingerprint in (profile.generic_fingerprint.split(":")[-1], profile.fingerprint):
            paths.append(self._generic_dir() / f"{profile.channel_slug}{scope}-{fingerprint[:12]}.wav")
        seen = set()
        reason = "No generic recording has been generated"
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            recording, error = self.validate_generic(path, maximum_duration)
            if recording is None:
                reason = error or reason
                continue
            if row is None or not row["audio_path"] or Path(row["audio_path"]).name != path.name:
                # Only an exact expected filename can recover an unreferenced recording.
                table, column, identity = self._generic_target(profile)
                now = datetime.now().astimezone().isoformat(timespec="seconds")
                self.connection.execute(
                    f"INSERT INTO {table}({column},profile_fingerprint,script_text,provider,status,"
                    "audio_path,duration_seconds,generated_at,updated_at) VALUES (?,?,?,?,'ready',?,?,?,?) "
                    f"ON CONFLICT({column}) DO UPDATE SET profile_fingerprint=excluded.profile_fingerprint,"
                    "script_text=excluded.script_text,provider=excluded.provider,status='ready',audio_path=excluded.audio_path,"
                    "duration_seconds=excluded.duration_seconds,generated_at=excluded.generated_at,updated_at=excluded.updated_at",
                    (identity, profile.generic_fingerprint, profile.generic_script, profile.tts_provider,
                     path.name, recording.duration_seconds, now, now),
                )
                self.connection.commit()
            elif path.parent == self._generic_dir() and row["audio_path"] != path.name:
                table, column, identity = self._generic_target(profile)
                self.connection.execute(f"UPDATE {table} SET audio_path=? WHERE {column}=?", (path.name, identity))
                self.connection.commit()
            return recording, None
        return None, reason

    def generic_recordings(self, channel_slug, daypart_slug=None, weekly_collection_slug=None, *, maximum_duration=None):
        active = self.profile(channel_slug, daypart_slug, weekly_collection_slug)
        profiles = [active]
        if active.weekly_collection_slug and daypart_slug:
            daypart = self._daypart_profile(channel_slug, daypart_slug)
            if daypart is not None:
                profiles.append(daypart)
        profiles.append(self._channel_profile(channel_slug))
        recordings, reasons, seen = [], [], set()
        for profile in profiles:
            scope = (profile.daypart_slug, profile.weekly_collection_slug)
            if scope in seen:
                continue
            seen.add(scope)
            recording, error = self._usable_generic(profile, maximum_duration)
            if recording and all(recording.identity != item.identity for item in recordings):
                recordings.append(recording)
            elif error and not recordings:
                reasons.append(f"{profile.channel_name}: {error}")
        return recordings, "; ".join(dict.fromkeys(reasons)) or None

    def generic_status(
        self,
        channel_slug: str,
        daypart_slug: str | None = None,
        weekly_collection_slug: str | None = None,
    ) -> dict[str, Any] | None:
        profile = self.scoped_profile(
            channel_slug, daypart_slug, weekly_collection_slug
        )
        row = self._generic_row(profile, ready_only=False)
        recording, reason = self._usable_generic(profile)
        row = self._generic_row(profile, ready_only=False)
        result = dict(row) if row else {"status": "pending"}
        result["usable"] = recording is not None
        result["needs_update"] = bool(row and row["profile_fingerprint"] not in
                                      {profile.generic_fingerprint, profile.fingerprint})
        result["availability_reason"] = reason
        return result

    def generate_generic(
        self,
        channel_slug: str,
        tts_provider,
        daypart_slug: str | None = None,
        weekly_collection_slug: str | None = None,
    ) -> Path:
        profile = self.scoped_profile(
            channel_slug, daypart_slug, weekly_collection_slug
        )
        if not profile.generic_script.strip():
            raise ValueError("Enter a generic fallback script first")
        if profile.weekly_collection_slug:
            scope = f"-weekly-{profile.weekly_collection_slug}"
        else:
            scope = f"-{profile.daypart_slug}" if profile.daypart_slug else ""
        target = self._generic_dir() / f"{channel_slug}{scope}-{profile.generic_fingerprint.split(':')[-1][:12]}.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.stem}-{uuid.uuid4().hex}.partial.wav")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        table, key_column, key_value = self._generic_target(profile)
        self.connection.execute(
            f"INSERT INTO {table}({key_column},profile_fingerprint,script_text,provider,status,updated_at) "
            "VALUES (?,?,?,?,'generating',?) "
            f"ON CONFLICT({key_column}) DO UPDATE SET status='generating',detail=NULL,updated_at=excluded.updated_at",
            (key_value, profile.generic_fingerprint, profile.generic_script, profile.tts_provider, now),
        )
        self.connection.commit()
        try:
            tts_provider.synthesize(
                profile.generic_script, temporary, voice=profile_voice(profile),
                instructions=profile.voice_instructions, rate=0,
            )
            recording, error = self.validate_generic(temporary)
            if recording is None:
                raise RuntimeError(error)
            duration = recording.duration_seconds
            os.replace(temporary, target)
            table, key_column, key_value = self._generic_target(profile)
            self.connection.execute(
                f"""
                UPDATE {table} SET audio_path=?,profile_fingerprint=?,script_text=?,provider=?,status='ready',
                    detail=NULL,duration_seconds=?,generated_at=?,updated_at=?
                WHERE {key_column}=?
                """,
                (target.name, profile.generic_fingerprint, profile.generic_script, profile.tts_provider,
                 duration, now, now, key_value),
            )
            self.connection.commit()
            return target
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            table, key_column, key_value = self._generic_target(profile)
            self.connection.execute(
                f"UPDATE {table} SET status='failed',detail=?, updated_at=? WHERE {key_column}=?",
                (str(exc), now, key_value),
            )
            self.connection.commit()
            raise

    def _generic_dir(self) -> Path:
        raw = Path(str(self.settings.get("output_dir", "./data/generated/announcements")))
        root = raw if raw.is_absolute() else (self.config.project_root / raw).resolve()
        return root / "generic"

    def _daypart_profile_id(self, profile: ChannelAnnouncementProfile) -> int:
        row = self.connection.execute(
            """
            SELECT id FROM announcement_daypart_profiles
            WHERE channel_id=? AND daypart_slug=?
            """,
            (profile.channel_id, profile.daypart_slug),
        ).fetchone()
        if row is None:
            raise ValueError("Daypart announcer profile was not found")
        return int(row["id"])

    def _weekly_profile_id(self, profile: ChannelAnnouncementProfile) -> int:
        row = self.connection.execute(
            """
            SELECT id FROM announcement_weekly_profiles
            WHERE channel_id=? AND collection_slug=?
            """,
            (profile.channel_id, profile.weekly_collection_slug),
        ).fetchone()
        if row is None:
            raise ValueError("Weekly announcer profile was not found")
        return int(row["id"])

    def _generic_target(
        self, profile: ChannelAnnouncementProfile
    ) -> tuple[str, str, int]:
        if profile.weekly_collection_slug:
            return (
                "announcement_weekly_generic_audio",
                "profile_id",
                self._weekly_profile_id(profile),
            )
        if profile.daypart_slug:
            return (
                "announcement_daypart_generic_audio",
                "profile_id",
                self._daypart_profile_id(profile),
            )
        return "announcement_generic_audio", "channel_id", profile.channel_id

    def _generic_row(self, profile: ChannelAnnouncementProfile, *, ready_only: bool):
        table, key_column, key_value = self._generic_target(profile)
        ready = " AND status='ready' AND profile_fingerprint IN (?,?)" if ready_only else ""
        values: tuple[Any, ...] = (
            (key_value, profile.generic_fingerprint, profile.fingerprint) if ready_only else (key_value,)
        )
        return self.connection.execute(
            f"SELECT * FROM {table} WHERE {key_column}=?{ready}", values
        ).fetchone()

    @staticmethod
    def _profile(row) -> ChannelAnnouncementProfile:
        keys = set(row.keys())
        return ChannelAnnouncementProfile(
            int(row["channel_id"]),str(row["slug"]),str(row["display_name"]),
            bool(row["enabled"]),str(row["script_model"]),str(row["speech_model"]),
            str(row["voice"]),str(row["personality_prompt"]),
            str(row["voice_instructions"]),str(row["generic_script"]),
            int(row["maximum_words"]),int(row["profile_version"]),
            str(row["daypart_slug"]) if "daypart_slug" in keys else None,
            str(row["collection_slug"]) if "collection_slug" in keys else None,
            bool(row["override_enabled"]) if "override_enabled" in keys else True,
            str(row["tagline"]) if "tagline" in keys else None,
            str(row["spoken_tagline"]) if "spoken_tagline" in keys else None,
            bool(row["promo_enabled"]) if "promo_enabled" in keys else True,
            bool(row["promo_narration_enabled"]) if "promo_narration_enabled" in keys else True,
            str(row["promo_prompt"]) if "promo_prompt" in keys else "",
            str(row["tts_provider"]) if "tts_provider" in keys else "openai",
            str(row["breeze_speech_model"]) if "breeze_speech_model" in keys else "breeze-tts-2",
            str(row["breeze_voice_id"]) if "breeze_voice_id" in keys else "",
        )
from retrotv.tts import profile_voice
from retrotv.processes import hidden_run

