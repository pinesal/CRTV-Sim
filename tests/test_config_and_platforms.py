import tempfile
import unittest
from pathlib import Path

from retrotv.config import load_config
from retrotv.platforms import adapter_for


class ConfigAndPlatformTests(unittest.TestCase):
    def test_windows_example_resolves_external_media_root(self):
        project = Path(__file__).resolve().parents[1]
        config = load_config(project / "config" / "development.windows.example.toml")
        self.assertEqual(config.profile_name, "windows-development")
        self.assertEqual(config.storage_root("media"), Path("F:/Videos/media"))
        self.assertTrue(config.database_path.is_absolute())

    def test_default_scanner_accepts_wmv_media(self):
        project = Path(__file__).resolve().parents[1]
        config = load_config(project / "config" / "development.windows.example.toml")
        self.assertIn(".wmv", config.data["scanner"]["video_extensions"])

    def test_logical_paths_are_platform_neutral(self):
        windows = adapter_for("windows")
        linux = adapter_for("linux")
        source = r"fox\night\episodes\Show\episode.mkv"
        self.assertEqual(
            windows.logical_path(source),
            "fox/night/episodes/Show/episode.mkv",
        )
        self.assertEqual(windows.logical_path(source), linux.logical_path(source))

    def test_logical_paths_reject_parent_escape(self):
        with self.assertRaises(ValueError):
            adapter_for("linux").logical_path("../outside/movie.mkv")


if __name__ == "__main__":
    unittest.main()
