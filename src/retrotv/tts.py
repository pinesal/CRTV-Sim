"""Provider routing shared by renderer, reusable audio, and connection tests."""
from __future__ import annotations
import io
import logging
import os
import ssl
from pathlib import Path
import uuid
import wave
from retrotv.credentials import configured_breeze_api_key


class BreezeProviderError(RuntimeError):
    pass


def _error(exc, operation=None):
    try:
        import httpx
        is_timeout = isinstance(exc, httpx.TimeoutException)
        is_connection = isinstance(exc, httpx.ConnectError)
    except ImportError:
        is_timeout = is_connection = False
    causes, current = [], exc
    while current is not None and id(current) not in {id(cause) for cause in causes}:
        causes.append(current)
        current = current.__cause__ or current.__context__
    is_certificate = any(isinstance(cause, ssl.SSLCertVerificationError)
                         or 'CERTIFICATE_VERIFY_FAILED' in str(cause) for cause in causes)
    code = str(getattr(exc, "code", ""))
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        detail = "API key is invalid or access is denied"
    elif status == 404:
        detail = "selected voice or model is unavailable"
    elif status == 429:
        detail = "rate limit reached; try again later"
    elif "BILLING" in code or "CREDIT" in code:
        detail = "account has insufficient credits"
    elif status == 400 or status == 422:
        detail = "selected voice, model, or delivery instructions were rejected"
    elif is_certificate:
        detail = "HTTPS certificate validation failed"
    elif is_timeout or isinstance(exc, TimeoutError):
        detail = "request timed out; try again"
    elif is_connection:
        detail = "could not connect to the API; check the network connection"
    elif isinstance(status, int) and status >= 500:
        detail = f"service temporarily unavailable (HTTP {status}); try again later"
    else:
        detail = "request failed or timed out; check the connection and account"
    if operation:
        detail += f" (while {operation})"
    # Record only safe classifications; raw exceptions can contain credentials.
    logging.getLogger(__name__).warning("Breeze request failed: operation=%s status=%s error=%s",
                                       operation or 'speech generation', status, type(exc).__name__)
    return BreezeProviderError("Breeze Blue: " + detail)


class BreezeTTSProvider:
    name = "breeze"

    def __init__(self, *, model="breeze-tts-2", client=None, timeout_seconds=90):
        self.model = model
        self.timeout = max(1.0, float(timeout_seconds))
        self._http_client = None
        if client is None:
            key = configured_breeze_api_key()
            if not key:
                raise BreezeProviderError("Breeze Blue API key is not configured")
            try:
                from breeze_blue import BreezeBlue
                import httpx
                from retrotv.ai_announcements import verified_ssl_context
            except ImportError as exc:
                raise BreezeProviderError("Breeze Blue SDK is not installed") from exc
            self._http_client = httpx.Client(verify=verified_ssl_context(), timeout=self.timeout)
            try:
                client = BreezeBlue(api_key=key, base_url="https://api.breeze.blue",
                                    timeout=self.timeout, http_client=self._http_client)
            except Exception:
                self._http_client.close()
                raise
        self.client = client

    def close(self):
        if self._http_client is not None:
            self._http_client.close()

    def synthesize(self, text, output: Path, *, voice, rate=0, instructions=""):
        if not voice:
            raise BreezeProviderError("Choose a Breeze Blue voice")
        try:
            audio = self.client.text_to_speech.convert(
                voice_id=voice, text=text, model_id=self.model,
                instructions=instructions or None, output_format="wav", timeout=self.timeout,
            )
        except Exception as exc:
            raise _error(exc) from exc
        try:
            content = audio.content
            with wave.open(io.BytesIO(content), "rb") as wav:
                frames = wav.readframes(wav.getnframes())
                block_size = wav.getnchannels() * wav.getsampwidth()
                declared_frames = wav.getnframes()
                if (not frames or wav.getframerate() <= 0 or len(frames) % block_size
                        or (declared_frames < 0xFFFFFFFF and len(frames) != declared_frames * block_size)):
                    raise ValueError("empty or truncated audio")
                # Rewrite the header to a finite frame count for downstream readers.
                normalized = io.BytesIO()
                with wave.open(normalized, "wb") as target:
                    target.setnchannels(wav.getnchannels()); target.setsampwidth(wav.getsampwidth())
                    target.setframerate(wav.getframerate()); target.writeframes(frames)
            output = Path(output)
            output.parent.mkdir(parents=True, exist_ok=True)
            partial = output.with_name(output.name + "." + uuid.uuid4().hex + ".partial")
            try:
                partial.write_bytes(normalized.getvalue()); os.replace(partial, output)
            finally:
                partial.unlink(missing_ok=True)
        except (OSError, ValueError, EOFError, wave.Error, AttributeError) as exc:
            raise BreezeProviderError("Breeze Blue returned invalid WAV audio or the audio could not be saved") from exc

    def options(self, *, page=1, next_page_token=None, search=None, include_models=True):
        """Fetch one voice page so a large catalog cannot block the dropdown."""
        if not isinstance(page, int) or page < 1 or page > 10_000:
            raise ValueError("Choose a valid Breeze voice page")
        operation = 'loading models'
        try:
            models = self.client.models.list(timeout=self.timeout) if include_models else []
            operation = 'loading voices'
            response = self.client.voices.search(page=page, next_page_token=next_page_token,
                                                search=search or None, page_size=50, timeout=self.timeout)
            voices = {voice["voice_id"]: {"id": voice["voice_id"], "name": voice.get("name") or voice["voice_id"]}
                      for voice in response.get("voices", []) if voice.get("voice_id")}
            token = response.get("next_page_token") or None
            if token and token == next_page_token:
                raise BreezeProviderError("Breeze Blue returned a repeated voice page; try refreshing")
            return {"models": [{"id": m["model_id"], "name": m.get("name") or m["model_id"]}
                               for m in models if m.get("model_id")],
                    "voices": sorted(voices.values(), key=lambda v: (v["name"].casefold(), v["id"])),
                    "has_more": bool(token or response.get("has_more")), "next_page_token": token,
                    "page": page}
        except BreezeProviderError:
            raise
        except Exception as exc:
            raise _error(exc, operation) from exc


def profile_voice(profile):
    return profile.breeze_voice_id if profile.tts_provider == "breeze" else profile.voice


def provider_for_profile(profile, config, openai_client=None):
    if profile.tts_provider == "breeze":
        return BreezeTTSProvider(model=profile.breeze_speech_model, timeout_seconds=config.data.get("announcements", {}).get("provider_timeout_seconds", 90))
    from retrotv.ai_announcements import OpenAIHTTPClient, OpenAITTSProvider
    client = openai_client or OpenAIHTTPClient(base_url=str(config.data.get("announcements", {}).get("openai_base_url", "https://api.openai.com/v1")))
    return OpenAITTSProvider(client, model=profile.speech_model)


def profile_tts_ready(profile):
    if profile.tts_provider == "breeze":
        return bool(configured_breeze_api_key() and profile.breeze_voice_id)
    from retrotv.ai_announcements import configured_openai_api_key
    return bool(configured_openai_api_key())
