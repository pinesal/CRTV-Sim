import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from retrotv.config import AppConfig
from retrotv.playback import MpvIpcPlayer, PlaybackTarget
from retrotv.track_preferences import SeriesTrackPreferences, describe_choice, match_choice


def audio(track_id, lang="jpn", title=""):
    return {"id": track_id, "type": "audio", "lang": lang, "title": title}


def subtitle(track_id, lang="eng", **extra):
    return {"id": track_id, "type": "sub", "lang": lang, **extra}


class TrackMatchingTests(unittest.TestCase):
    def test_languages_and_titles_survive_reordered_streams(self):
        choice = describe_choice([audio(1), audio(2, "eng", "English Dub")], "audio", 2)
        self.assertEqual(match_choice([audio(7, "en", "english dub"), audio(8)], "audio", choice), 7)
        self.assertIsNone(match_choice([audio(3, "eng", "Commentary")], "audio", choice))
        self.assertIsNone(match_choice([audio(1)], "audio", choice))

    def test_forced_and_accessibility_tracks_do_not_replace_normal_subtitles(self):
        choice = describe_choice([subtitle(1)], "sub", 1)
        self.assertIsNone(match_choice([subtitle(2, forced=True)], "sub", choice))
        self.assertIsNone(match_choice([subtitle(2, **{"hearing-impaired": True})], "sub", choice))
        self.assertEqual(match_choice([subtitle(2)], "sub", choice), 2)
        self.assertIsNone(match_choice([subtitle(2, external=True)], "sub", choice))

    def test_untagged_tracks_use_relative_order_and_off_is_explicit(self):
        choice = describe_choice([audio(1, ""), audio(2, "")], "audio", 2)
        self.assertEqual(match_choice([audio(4, ""), audio(6, "")], "audio", choice), 6)
        self.assertIsNone(match_choice([audio(4, "")], "audio", choice))
        self.assertEqual(match_choice([], "sub", {"mode": "off"}), "no")
        self.assertIsNone(describe_choice([], "sub", None))


class TrackPlaybackTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".test-tmp"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)
        self.config = AppConfig(
            profile_path=self.root / "profile.toml", project_root=self.root,
            data={"storage_roots": {"state": str(self.root / "state")},
                  "player": {"foreground_on_start": False}},
        )
        self.files = {
            "one.mkv": [audio(1), audio(2, "eng"), subtitle(1)],
            "two.mkv": [audio(7, "en"), audio(8), subtitle(4)],
            "missing.mkv": [audio(1)],
            "commercial.mp4": [audio(1, "eng")],
        }
        self.player = self.make_player()

    def tearDown(self):
        self.temp.cleanup()

    def make_player(self):
        player = MpvIpcPlayer(self.config)
        player.connection = Mock()
        player.start = Mock()
        player._command = Mock(side_effect=lambda cmd: self.command(player, cmd))
        player.test_tracks = []
        player.test_values = {"aid": None, "sid": None, "sub-visibility": True, "time-pos": 0}
        return player

    def command(self, player, command):
        op = command[0]
        if op == "loadfile":
            player.test_tracks = copy.deepcopy(self.files[Path(command[1]).name])
            options = command[4]
            for kind, prop in (("audio", "aid"), ("sub", "sid")):
                available = [track["id"] for track in player.test_tracks if track["type"] == kind]
                choice = options[prop]
                player.test_values[prop] = (available[0] if available else None) if choice == "auto" else None
            player.test_values["sub-visibility"] = options["sub-visibility"] == "yes"
            player.file_loaded.set()
        elif op == "get_property":
            if command[1] == "track-list":
                tracks = copy.deepcopy(player.test_tracks)
                for track in tracks:
                    track["selected"] = track["id"] == player.test_values["aid" if track["type"] == "audio" else "sid"]
                return tracks
            return player.test_values[command[1]]
        elif op == "set_property":
            player.test_values[command[1]] = None if command[2] == "no" else command[2]
        elif op == "seek":
            player.test_values["time-pos"] = command[1]

    def target(self, filename="one.mkv", series="Gundam Wing"):
        now = datetime.now(timezone.utc)
        return PlaybackTarget(1, 1, filename, "episode" if series else "commercial", now,
                              now + timedelta(minutes=20), self.root / filename, "original", 0, 1200,
                              series_title=series)

    def change(self, name, value):
        self.player.test_values[name] = value
        self.player.connection.readline.return_value = json.dumps(
            {"event": "property-change", "name": name, "data": value}
        ).encode() + b"\n"
        self.player._read_message()

    def test_changes_save_without_button_and_restore_after_reorder_and_restart(self):
        self.player.load(self.target())
        self.change("aid", 2)
        self.change("sid", None)
        self.change("sub-visibility", False)
        self.player.poll_action()
        self.player.load(self.target("two.mkv"))
        self.assertEqual(self.player.test_values["aid"], 7)
        self.assertIsNone(self.player.test_values["sid"])
        self.assertFalse(self.player.test_values["sub-visibility"])
        restarted = self.make_player()
        restarted.load(self.target("two.mkv"))
        self.assertEqual(restarted.test_values["aid"], 7)
        self.assertIsNone(restarted.test_values["sid"])
        self.assertFalse(restarted.test_values["sub-visibility"])

    def test_series_and_commercial_defaults_do_not_leak_or_create_preferences(self):
        self.player.load(self.target())
        self.change("aid", 2)
        self.change("sub-visibility", False)
        # Changing channels before the next poll still captures the last choice.
        self.player.load(self.target(series="Other Show"))
        self.assertEqual(self.player.test_values["aid"], 1)
        self.assertTrue(self.player.test_values["sub-visibility"])
        self.player.load(self.target("commercial.mp4", series=None))
        self.change("aid", None)
        self.player.poll_action()
        self.player.load(self.target("two.mkv"))
        self.assertEqual(self.player.test_values["aid"], 7)
        self.assertFalse(self.player.test_values["sub-visibility"])
        self.assertEqual(set(self.player._preferences().series), {"Gundam Wing"})

    def test_unavailable_track_fallback_and_automatic_changes_do_not_overwrite_preference(self):
        self.player.load(self.target())
        self.change("aid", 2)
        self.player.poll_action()
        expected = self.player._preferences().get("Gundam Wing")
        self.player.load(self.target("missing.mkv"))
        self.assertEqual(self.player.test_values["aid"], 1)
        self.change("aid", 1)  # A late mpv notification of the automatic default.
        self.player.poll_action()
        self.assertEqual(self.player._preferences().get("Gundam Wing"), expected)
        self.player.load(self.target("two.mkv"))
        self.assertEqual(self.player.test_values["aid"], 7)

    def test_last_change_is_captured_on_stop_and_explicit_visibility_can_be_reenabled(self):
        self.player.load(self.target())
        self.change("sub-visibility", False)
        self.player.poll_action()
        self.player.load(self.target("two.mkv"))
        self.change("sub-visibility", True)
        self.change("aid", 8)
        self.player.process = Mock()
        self.player.process.poll.return_value = None
        self.player.stop()
        saved = SeriesTrackPreferences(self.root / "state/series-track-preferences.json").get("Gundam Wing")
        self.assertTrue(saved["subtitle_visible"])
        self.assertEqual(saved["audio"]["language"], "ja")

    def test_quick_cycle_back_to_default_is_an_explicit_new_preference(self):
        self.player.load(self.target())
        self.change("aid", 2)
        self.player.poll_action()
        self.player.load(self.target("missing.mkv"))
        # A user can deliberately choose the fallback track by cycling off
        # and on again faster than the playback controller polls.
        self.change("aid", None)
        self.change("aid", 1)
        self.player.poll_action()
        self.assertEqual(self.player._preferences().get("Gundam Wing")["audio"]["language"], "ja")

    def test_invalid_state_or_save_failure_cannot_stop_playback(self):
        path = self.root / "state/series-track-preferences.json"
        path.parent.mkdir()
        path.write_text("broken", encoding="utf-8")
        with patch("builtins.print"):
            self.player.load(self.target())
        self.assertFalse(self.player.test_values["pause"])
        path.unlink()
        self.player.load(self.target())
        self.change("aid", 2)
        with patch("retrotv.track_preferences.atomic_json", side_effect=OSError("Disk full")), patch("builtins.print"):
            self.player.poll_action()
        self.assertFalse(self.player.test_values["pause"])


if __name__ == "__main__":
    unittest.main()
