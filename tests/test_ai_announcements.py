import io
import json
import shutil
import threading
import unittest
import uuid
import wave
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch

from retrotv.ai_announcements import (
    AnnouncementProfileService,
    OpenAIHTTPClient,
    OpenAIScriptProvider,
    OpenAIWeeklyPromoScriptProvider,
    OpenAITTSProvider,
    verified_ssl_context,
)
from retrotv.announcements import AnnouncementLine, AnnouncementPlan
from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database
from retrotv.web import make_handler


class FakeOpenAIClient:
    def __init__(self):
        self.json_requests = []
        self.byte_requests = []

    def request_json(self, path, payload):
        self.json_requests.append((path, payload))
        return {"output_text": "Coming up, three back-to-back episodes of Doug!"}

    def request_bytes(self, path, payload):
        self.byte_requests.append((path, payload))
        data = io.BytesIO()
        with wave.open(data, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\0\0" * 1600)
        return data.getvalue()


class AIAnnouncementTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(__file__).resolve().parents[1]
        self.root = self.project / ".test-tmp" / f"ai-announcements-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.config = AppConfig(
            self.root / "test.toml", self.project,
            {
                "application": {"database_path": str(self.root / "retrotv.sqlite3")},
                "announcements": {
                    "output_dir": str(self.root / "generated"),
                    "script_model": "test-script-model",
                    "speech_model": "test-speech-model",
                    "voice": "marin",
                    "maximum_words": 45,
                },
                "daypart_branding": {
                    "nickelodeon": {
                        "night": {
                            "display_name": "Nick at Nite",
                            "voice": "onyx",
                            "personality_prompt": "Warm and nostalgic.",
                            "generic_script": "You're watching Nick at Nite.",
                        }
                    }
                },
                "seasonal": {"windows": {
                    "snick-main": {
                        "channel": "nickelodeon", "collection": "snick-main",
                        "guide_label": "SNICK", "enabled": True,
                    }
                }},
            },
        )
        self.connection = connect_database(self.config.database_path)
        apply_migrations(self.connection, self.project / "migrations")
        self.connection.execute(
            "INSERT INTO channels(id,slug,display_name,enabled) VALUES (1,'nickelodeon','Nickelodeon',1)"
        )
        self.connection.commit()
        self.service = AnnouncementProfileService(self.connection, self.config)

    def tearDown(self):
        self.connection.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_dashboard_profile_save_accepts_null_scopes_and_persists(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.config))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            payload = {
                "action": "save",
                "daypart_slug": None,
                "weekly_collection_slug": None,
                "enabled": True,
                "generic_script": "You're watching PBS.",
            }
            request = Request(
                f"{base}/api/announcement-profiles/nickelodeon",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                self.assertTrue(json.load(response)["saved"])
            with urlopen(f"{base}/api/announcement-profiles") as response:
                saved = json.load(response)["items"]
            channel = next(item for item in saved if item["channel_slug"] == "nickelodeon" and item["daypart_slug"] is None and item["weekly_collection_slug"] is None)
            self.assertTrue(channel["enabled"])
            self.assertEqual(channel["generic_script"], "You're watching PBS.")
            with connect_database(self.config.database_path) as connection:
                row = connection.execute(
                    "SELECT enabled,generic_script FROM announcement_channel_profiles WHERE channel_id=1"
                ).fetchone()
            self.assertEqual(tuple(row), (1, "You're watching PBS."))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_free_form_script_prompt_contains_authoritative_schedule(self):
        profile = self.service.update(
            "nickelodeon",
            {"enabled": True, "personality_prompt": "Be playful like 1990s Nickelodeon.", "maximum_words": 45},
        )
        lines = tuple(
            AnnouncementLine(datetime.fromisoformat(f"2026-08-03T{hour}:00:00-07:00"), "Doug")
            for hour in (15, 16, 17)
        )
        plan = AnnouncementPlan(
            "nickelodeon", "Nickelodeon", lines[0].starts_at, lines,
            "The only network for kids!", "0xF58220", "deterministic",
        )
        client = FakeOpenAIClient()
        result = OpenAIScriptProvider(client).generate(profile, plan)
        self.assertEqual(result, "Coming up, three back-to-back episodes of Doug!")
        path, payload = client.json_requests[0]
        self.assertEqual(path, "responses")
        self.assertEqual(payload["model"], "test-script-model")
        self.assertIn("Be playful like 1990s Nickelodeon", payload["input"])
        self.assertEqual(payload["input"].count("Doug"), 3)

    def test_weekly_promo_script_provider_uses_only_supplied_facts(self):
        profile = self.service.update(
            "nickelodeon",
            {"promo_prompt": "Energetic Saturday night invitation."},
            weekly_collection_slug="snick-main",
        )
        client = FakeOpenAIClient()
        facts = {
            "weekly_special_promo": {
                "special_name": "SNICK",
                "airing_at_display": "Saturday, September 26 at 8 PM",
            },
            "selected_lineup": ["Roundhouse", "Are You Afraid of the Dark?"],
            "lineup_more": True,
        }
        result = OpenAIWeeklyPromoScriptProvider(client).generate(profile, facts)
        self.assertTrue(result)
        prompt = client.json_requests[0][1]["input"]
        self.assertIn("Do not invent or infer programs, episode details, dates, times", prompt)
        self.assertIn("SNICK", prompt)
        self.assertIn("Saturday, September 26 at 8 PM", prompt)
        self.assertIn("Roundhouse", prompt)
        self.assertIn("Are You Afraid of the Dark?", prompt)
        self.assertIn("Energetic Saturday night invitation.", prompt)
        self.assertNotIn("Doug", prompt)

    def test_weekly_promo_controls_persist_independently_of_announcer_override(self):
        changed = self.service.update(
            "nickelodeon",
            {"promo_enabled": False, "promo_narration_enabled": False,
             "promo_prompt": "Keep the delivery playful."},
            weekly_collection_slug="snick-main",
        )
        self.assertFalse(changed.override_enabled)
        self.assertFalse(changed.promo_enabled)
        self.assertFalse(changed.promo_narration_enabled)
        self.assertEqual(changed.promo_prompt, "Keep the delivery playful.")
        again = AnnouncementProfileService(self.connection, self.config).scoped_profile(
            "nickelodeon", weekly_collection_slug="snick-main"
        )
        self.assertEqual(again.promo_prompt, changed.promo_prompt)
        self.assertFalse(again.promo_enabled)

    def test_tts_uses_channel_voice_and_delivery_instructions(self):
        client = FakeOpenAIClient()
        output = self.root / "speech.wav"
        OpenAITTSProvider(client, model="test-speech-model").synthesize(
            "Hello!", output, voice="marin", instructions="Dude-bro energy."
        )
        self.assertTrue(output.is_file())
        path, payload = client.byte_requests[0]
        self.assertEqual(path, "audio/speech")
        self.assertEqual(payload["model"], "test-speech-model")
        self.assertEqual(payload["voice"], "marin")
        self.assertEqual(payload["instructions"], "Dude-bro energy.")
        self.assertEqual(payload["response_format"], "wav")

    def test_generic_audio_and_profile_staleness(self):
        profile = self.service.update(
            "nickelodeon",
            {"generic_script": "You're watching Nickelodeon!", "voice": "marin", "voice_instructions": "Bright and playful."},
        )
        client = FakeOpenAIClient()
        output = self.service.generate_generic(
            "nickelodeon", OpenAITTSProvider(client, model=profile.speech_model)
        )
        self.assertEqual(self.service.generic_audio("nickelodeon"), output)
        status = self.service.generic_status("nickelodeon")
        self.assertEqual(status["status"], "ready")
        self.assertGreater(status["duration_seconds"], 0)
        self.service.update("nickelodeon", {"voice": "onyx"})
        self.assertEqual(self.service.generic_audio("nickelodeon"), output)
        self.assertEqual(self.service.generic_status("nickelodeon")["status"], "stale")
        self.assertTrue(self.service.generic_status("nickelodeon")["usable"])
        self.assertTrue(self.service.generic_status("nickelodeon")["needs_update"])

    def test_profile_save_normalizes_voice_and_rejects_blank_models(self):
        profile = self.service.update("nickelodeon", {"voice": "Onyx"})
        self.assertEqual(profile.voice, "onyx")
        with self.assertRaisesRegex(ValueError, "Script model cannot be blank"):
            self.service.update("nickelodeon", {"script_model": ""})

    def test_dynamic_settings_do_not_stale_generic_recording(self):
        profile = self.service.update("nickelodeon", {"generic_script": "Stay tuned!"})
        output = self.service.generate_generic("nickelodeon", OpenAITTSProvider(FakeOpenAIClient(), model=profile.speech_model))
        self.service.update("nickelodeon", {"enabled": True, "script_model": "another-model",
                                            "personality_prompt": "Something else", "maximum_words": 100})
        status = self.service.generic_status("nickelodeon")
        self.assertEqual(status["status"], "ready")
        self.assertFalse(status["needs_update"])
        self.assertEqual(self.service.generic_audio("nickelodeon", "morning"), output)

    def test_failed_regeneration_preserves_last_success_and_works_during_generation(self):
        profile = self.service.update("nickelodeon", {"generic_script": "The old recording"})
        output = self.service.generate_generic("nickelodeon", OpenAITTSProvider(FakeOpenAIClient(), model=profile.speech_model))
        original = dict(self.service.generic_status("nickelodeon"))
        self.service.update("nickelodeon", {"generic_script": "Replacement script", "voice": "echo"})
        def fail(_text, _path, **_kwargs):
            self.assertEqual(self.service.generic_status("nickelodeon")["status"], "generating")
            self.assertEqual(self.service.generic_audio("nickelodeon"), output)
            raise RuntimeError("TTS timeout")
        provider = Mock()
        provider.synthesize.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, "TTS timeout"):
            self.service.generate_generic("nickelodeon", provider)
        status = self.service.generic_status("nickelodeon")
        for name in ("audio_path", "profile_fingerprint", "script_text", "provider", "generated_at", "duration_seconds"):
            self.assertEqual(status[name], original[name])
        self.assertEqual(status["status"], "failed")
        self.assertTrue(status["usable"])
        self.assertTrue(status["needs_update"])
        self.assertEqual(self.service.generic_audio("nickelodeon"), output)
        self.assertEqual(list(output.parent.glob("*.partial.wav")), [])

    def test_missing_corrupt_or_oversized_scoped_track_uses_channel_recording(self):
        provider = OpenAITTSProvider(FakeOpenAIClient(), model="test")
        channel = self.service.generate_generic("nickelodeon", provider)
        scoped = self.service.generate_generic("nickelodeon", provider, "night")
        scoped.unlink()
        self.assertEqual(self.service.generic_audio("nickelodeon", "night"), channel)
        scoped.write_bytes(b"not audio")
        self.assertEqual(self.service.generic_audio("nickelodeon", "night"), channel)
        scoped = self.service.generate_generic("nickelodeon", provider, "night")
        malformed = bytearray(scoped.read_bytes())
        malformed[24:28] = b"\0" * 4  # Invalid zero sample rate.
        scoped.write_bytes(malformed)
        self.assertEqual(self.service.generic_audio("nickelodeon", "night"), channel)
        with wave.open(str(scoped), "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000)
            wav.writeframes(b"\0\0" * 8000 * 31)
        self.assertEqual(self.service.generic_audio("nickelodeon", "night"), channel)

    def test_relative_paths_relocation_and_exact_orphan_recovery(self):
        output = self.service.generate_generic("nickelodeon", OpenAITTSProvider(FakeOpenAIClient(), model="test"))
        self.assertEqual(self.service.generic_status("nickelodeon")["audio_path"], output.name)
        self.connection.execute("UPDATE announcement_generic_audio SET audio_path=?", (str(self.root / "previous-install" / output.name),))
        self.connection.commit()
        self.assertEqual(self.service.generic_audio("nickelodeon"), output)
        self.assertEqual(self.service.generic_status("nickelodeon")["audio_path"], output.name)
        self.connection.execute("DELETE FROM announcement_generic_audio")
        self.connection.commit()
        self.assertEqual(self.service.generic_audio("nickelodeon"), output)
        self.assertTrue(self.service.generic_status("nickelodeon")["usable"])
        output.rename(output.parent / "nickelodeon-arbitrary.wav")
        self.connection.execute("DELETE FROM announcement_generic_audio")
        self.connection.commit()
        self.assertIsNone(self.service.generic_audio("nickelodeon"))

    def test_truncated_wav_is_not_usable(self):
        output = self.service.generate_generic("nickelodeon", OpenAITTSProvider(FakeOpenAIClient(), model="test"))
        output.write_bytes(output.read_bytes()[:-100])
        self.assertIsNone(self.service.generic_audio("nickelodeon"))
        self.assertFalse(self.service.generic_status("nickelodeon")["usable"])

    def test_successful_replacement_publishes_new_audio_identity(self):
        provider = OpenAITTSProvider(FakeOpenAIClient(), model="test")
        old = self.service.generate_generic("nickelodeon", provider)
        self.service.update("nickelodeon", {"generic_script": "New generic script"})
        new = self.service.generate_generic("nickelodeon", provider)
        self.assertNotEqual(old, new)
        self.assertTrue(old.is_file())
        self.assertEqual(self.service.generic_audio("nickelodeon"), new)
        self.assertFalse(self.service.generic_status("nickelodeon")["needs_update"])

    def test_current_legacy_fingerprint_survives_unrelated_profile_edit(self):
        profile = self.service.profile("nickelodeon")
        output = self.service.generate_generic("nickelodeon", OpenAITTSProvider(FakeOpenAIClient(), model="test"))
        self.connection.execute("UPDATE announcement_generic_audio SET profile_fingerprint=?,audio_path=?",
                                (profile.fingerprint, str(output)))
        self.connection.commit()
        self.service.update("nickelodeon", {"enabled": True})
        status = self.service.generic_status("nickelodeon")
        self.assertTrue(status["usable"])
        self.assertFalse(status["needs_update"])
        self.assertEqual(status["status"], "ready")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_streamed_wav_with_unknown_length_is_validated_using_actual_payload(self):
        class StreamedClient(FakeOpenAIClient):
            def request_bytes(self, path, payload):
                data = bytearray(super().request_bytes(path, payload))
                data[4:8] = b"\xff" * 4
                data[40:44] = b"\xff" * 4
                return bytes(data)
        output = self.service.generate_generic("nickelodeon", OpenAITTSProvider(StreamedClient(), model="test"))
        self.assertEqual(self.service.generic_audio("nickelodeon"), output)
        self.assertAlmostEqual(self.service.generic_status("nickelodeon")["duration_seconds"], 0.1, places=2)

    def test_daypart_profile_has_independent_identity_voice_and_fallback(self):
        daytime = self.service.profile("nickelodeon")
        night = self.service.profile("nickelodeon", "night")
        self.assertEqual(daytime.channel_name, "Nickelodeon")
        self.assertEqual(night.channel_name, "Nick at Nite")
        self.assertEqual(night.daypart_slug, "night")
        self.assertEqual(night.voice, "onyx")

        changed = self.service.update(
            "nickelodeon", {"voice": "echo"}, "night"
        )
        self.assertEqual(changed.voice, "echo")
        self.assertNotEqual(self.service.profile("nickelodeon").voice, "echo")

        client = FakeOpenAIClient()
        output = self.service.generate_generic(
            "nickelodeon",
            OpenAITTSProvider(client, model=changed.speech_model),
            "night",
        )
        self.assertEqual(
            self.service.generic_audio("nickelodeon", "night"), output
        )
        self.assertIsNone(self.service.generic_audio("nickelodeon"))

    def test_weekly_profile_is_optional_and_precedes_daypart_when_enabled(self):
        fallback = self.service.profile(
            "nickelodeon", "night", "snick-main"
        )
        self.assertEqual(fallback.daypart_slug, "night")
        weekly = self.service.scoped_profile(
            "nickelodeon", weekly_collection_slug="snick-main"
        )
        self.assertFalse(weekly.override_enabled)
        self.assertEqual(weekly.channel_name, "SNICK")

        changed = self.service.update(
            "nickelodeon",
            {
                "override_enabled": True,
                "enabled": True,
                "identity_name": "Saturday Night Nickelodeon",
                "tagline": "Saturday belongs to {channel}!",
                "spoken_tagline": "You're inside Saturday Night Nickelodeon!",
                "voice": "Echo",
            },
            weekly_collection_slug="snick-main",
        )
        self.assertTrue(changed.override_enabled)
        resolved = self.service.profile(
            "nickelodeon", "night", "snick-main"
        )
        self.assertEqual(resolved.profile_scope, "weekly")
        self.assertEqual(resolved.channel_name, "Saturday Night Nickelodeon")
        self.assertEqual(resolved.voice, "echo")
        self.assertEqual(self.service.profile("nickelodeon", "night").voice, "onyx")

    def test_weekly_generic_audio_is_isolated_from_channel_and_daypart(self):
        profile = self.service.update(
            "nickelodeon",
            {
                "override_enabled": True,
                "generic_script": "You're watching SNICK!",
            },
            weekly_collection_slug="snick-main",
        )
        client = FakeOpenAIClient()
        output = self.service.generate_generic(
            "nickelodeon",
            OpenAITTSProvider(client, model=profile.speech_model),
            weekly_collection_slug="snick-main",
        )
        self.assertEqual(
            self.service.generic_audio(
                "nickelodeon", "night", "snick-main"
            ),
            output,
        )
        self.assertIsNone(self.service.generic_audio("nickelodeon"))

    def test_scoped_generic_audio_falls_back_to_channel_track(self):
        channel = self.service.update(
            "nickelodeon", {"generic_script": "You're watching Nickelodeon!"}
        )
        client = FakeOpenAIClient()
        output = self.service.generate_generic(
            "nickelodeon", OpenAITTSProvider(client, model=channel.speech_model)
        )
        self.service.update(
            "nickelodeon",
            {"override_enabled": True, "generic_script": "SNICK is on now."},
            weekly_collection_slug="snick-main",
        )
        self.assertEqual(
            self.service.generic_audio("nickelodeon", "night", "snick-main"),
            output,
        )

    def test_explicit_api_key_takes_precedence(self):
        client = OpenAIHTTPClient(api_key="explicit-test-key")
        self.assertTrue(client.available)
        self.assertEqual(client.api_key, "explicit-test-key")

    def test_verified_ssl_context_loads_windows_root_certificates(self):
        context = Mock()
        with (
            patch("retrotv.ai_announcements._native_trust_context", return_value=None),
            patch("retrotv.ai_announcements.ssl.create_default_context", return_value=context),
            patch("certifi.where", return_value="portable-ca-bundle.pem"),
            patch(
                "retrotv.ai_announcements.ssl.enum_certificates",
                side_effect=[
                    [(b"root-der", "x509_asn", True)],
                    [(b"intermediate-der", "x509_asn", True)],
                ],
                create=True,
            ),
            patch(
                "retrotv.ai_announcements.ssl.DER_cert_to_PEM_cert",
                return_value="-----BEGIN CERTIFICATE-----\nroot\n-----END CERTIFICATE-----\n",
            ),
        ):
            self.assertIs(verified_ssl_context(), context)
        self.assertEqual(
            context.load_verify_locations.call_count,
            1,
        )
        context.load_verify_locations.assert_called_once()
        self.assertIn(
            "BEGIN CERTIFICATE",
            context.load_verify_locations.call_args.kwargs["cadata"],
        )

    def test_verified_ssl_context_prefers_native_system_trust(self):
        native = Mock()
        with patch(
            "retrotv.ai_announcements._native_trust_context", return_value=native
        ):
            self.assertIs(verified_ssl_context(), native)

    def test_openai_requests_use_the_verified_ssl_context(self):
        context = Mock()
        response = Mock()
        response.read.return_value = b'{"output_text":"Test"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        client = OpenAIHTTPClient(
            api_key="explicit-test-key", ssl_context=context
        )
        with patch(
            "retrotv.ai_announcements.urllib.request.urlopen",
            return_value=response,
        ) as open_url:
            self.assertEqual(client.request_json("responses", {"model": "test"}), {
                "output_text": "Test"
            })
        self.assertIs(open_url.call_args.kwargs["context"], context)


if __name__ == "__main__":
    unittest.main()
