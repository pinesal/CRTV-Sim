import subprocess
import os
import shutil
import uuid
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from retrotv.config import AppConfig
from retrotv.playback import FfplayPlayer, MpvIpcPlayer, PlaybackTarget, create_player


class PlayerLaunchTests(unittest.TestCase):
    def config(self, player):
        root = Path(__file__).resolve().parents[1]
        return AppConfig(
            profile_path=root / "test.toml",
            project_root=root,
            data={
                "application": {"database_path": "unused.sqlite3"},
                "storage_roots": {"media": "unused"},
                "player": player,
            },
        )

    def test_auto_backend_resolves_bundled_mpv_relative_to_project(self):
        config = self.config({"backend": "auto", "mpv": "./tools/mpv.exe"})
        with patch.object(Path, "is_file", return_value=True):
            player = create_player(config, "auto")
        self.assertIsInstance(player, MpvIpcPlayer)
        self.assertTrue(Path(player.executable).is_absolute())

    def test_crt_safe_osd_margins_have_inset_defaults(self):
        player = MpvIpcPlayer(self.config({"mpv": "mpv"}))
        self.assertEqual(player.osd_margin_x, 48)
        self.assertEqual(player.osd_margin_y, 36)

    def test_mpv_uses_current_focus_option(self):
        player = MpvIpcPlayer(self.config({"mpv": "mpv", "foreground_on_start": True}))
        player._input_config = MagicMock(return_value="input.conf")
        process = MagicMock()
        process.poll.return_value = None
        process.pid = 1234
        connection = MagicMock()
        connection.readline.return_value = b""
        with (
            patch("retrotv.playback.subprocess.Popen", return_value=process) as popen,
            patch("retrotv.playback._open_windows_pipe", return_value=connection),
            patch("retrotv.playback._focus_windows_process_window") as focus,
            patch.object(player, "_command", return_value=None),
        ):
            player.start()
        args = popen.call_args.args[0]
        self.assertIn("--focus-on=open", args)
        self.assertNotIn("--focus-on-open=yes", args)
        focus.assert_not_called()

    def test_ffplay_uses_no_console_and_redirected_streams(self):
        config = self.config({"ffplay": "ffplay", "fullscreen": False})
        player = FfplayPlayer(config)
        target = PlaybackTarget(
            entry_id=1,
            asset_id=1,
            channel_slug="fox",
            title="Test",
            entry_type="episode",
            representation="source",
            path=Path("test.mp4"),
            starts_at=None,
            ends_at=None,
            media_offset_seconds=0.0,
            remaining_seconds=10.0,
        )
        with patch("retrotv.playback.subprocess.Popen") as popen:
            player.load(target)
        options = popen.call_args.kwargs
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["stdout"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertEqual(
            options["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )

    def test_portable_runtime_forces_bundled_mpv_even_if_ffplay_is_selected(self):
        config = self.config({"backend": "ffplay", "ffplay": "ffplay"})
        root = Path(__file__).resolve().parents[1] / ".test-tmp" / f"portable-player-{uuid.uuid4().hex}"
        try:
            bundled = root / "tools" / "mpv" / "mpv.exe"
            bundled.parent.mkdir(parents=True)
            bundled.write_bytes(b"mpv")
            with patch.dict(os.environ, {"RETROTV_PORTABLE_ROOT": str(root)}):
                player = create_player(config, "ffplay")
        finally:
            shutil.rmtree(root, ignore_errors=True)
        self.assertIsInstance(player, MpvIpcPlayer)
        self.assertEqual(Path(player.executable), bundled.resolve())


if __name__ == "__main__":
    unittest.main()
