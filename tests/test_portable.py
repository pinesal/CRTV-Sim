import os
import json
import re
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from retrotv import portable
from retrotv.branding import PORTABLE_EXE, portable_executable


class PortableLauncherTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / ".test-tmp"
        root.mkdir(exist_ok=True)
        self.temporary = root / f"portable-{uuid.uuid4().hex}"
        (self.temporary / "portable-defaults" / "user-data" / "config").mkdir(
            parents=True
        )
        (self.temporary / "portable-defaults" / "user-data" / "config" / "x.txt").write_text(
            "default", encoding="utf-8"
        )

    def tearDown(self):
        shutil.rmtree(self.temporary, ignore_errors=True)

    def test_seed_copies_missing_defaults_without_overwriting_user_data(self):
        destination = self.temporary / "user-data" / "config"
        destination.mkdir(parents=True)
        (destination / "x.txt").write_text("custom", encoding="utf-8")
        portable._seed_user_data(self.temporary)
        self.assertEqual((destination / "x.txt").read_text(encoding="utf-8"), "custom")

    def test_rebranded_launcher_is_preferred_and_legacy_build_still_recognized(self):
        self.assertIsNone(portable_executable(self.temporary))
        legacy = self.temporary / "RetroTV.exe"
        legacy.write_bytes(b"old-build")
        self.assertEqual(portable_executable(self.temporary), legacy)
        current = self.temporary / PORTABLE_EXE
        current.write_bytes(b"new-build")
        self.assertEqual(portable_executable(self.temporary), current)

    def test_previous_build_can_use_rebranded_launcher_without_renaming_profile(self):
        previous = self.temporary / "previous"
        (previous / "user-data/database").mkdir(parents=True)
        (previous / PORTABLE_EXE).write_bytes(b"new-build")
        database = previous / "user-data/database/retrotv.sqlite3"
        database.write_bytes(b"retained database")
        (self.temporary / "previous-build.path").write_text("previous\n", encoding="utf-8")
        portable._import_previous_user_data(self.temporary)
        self.assertEqual(
            (self.temporary / "user-data/database/retrotv.sqlite3").read_bytes(),
            b"retained database",
        )

    def test_internet_media_build_clones_previous_user_data_once(self):
        previous = self.temporary.parent / f"previous-{uuid.uuid4().hex}"
        try:
            (previous / "user-data" / "database").mkdir(parents=True)
            (previous / "RetroTV.exe").write_bytes(b"old-build")
            old_database = previous / "user-data" / "database" / "retrotv.sqlite3"
            old_database.write_bytes(b"original-database")
            (self.temporary / "previous-build.path").write_text(
                str(previous), encoding="utf-8"
            )
            portable._import_previous_user_data(self.temporary)
            copied = self.temporary / "user-data" / "database" / "retrotv.sqlite3"
            self.assertEqual(copied.read_bytes(), b"original-database")
            old_database.write_bytes(b"later-change")
            portable._import_previous_user_data(self.temporary)
            self.assertEqual(copied.read_bytes(), b"original-database")
        finally:
            shutil.rmtree(previous, ignore_errors=True)

    def test_in_place_install_does_not_copy_user_data_onto_itself(self):
        (self.temporary / "RetroTV.exe").write_bytes(b"current-build")
        (self.temporary / "previous-build.path").write_text(".\n", encoding="utf-8")
        existing = self.temporary / "user-data" / "config" / "custom.txt"
        existing.parent.mkdir(parents=True)
        existing.write_text("preserve me", encoding="utf-8")

        portable._import_previous_user_data(self.temporary)

        self.assertEqual(existing.read_text(encoding="utf-8"), "preserve me")
        self.assertTrue((self.temporary / "user-data" / ".internet-media-imported").is_file())
        self.assertIsNone(portable._previous_build_root(self.temporary))

    def test_import_uses_newest_previous_build_with_user_data(self):
        older = self.temporary.parent / f"older-{uuid.uuid4().hex}"
        newer = self.temporary.parent / f"newer-{uuid.uuid4().hex}"
        try:
            older.mkdir()
            newer.mkdir()
            (older / "RetroTV.exe").write_bytes(b"older")
            (newer / "RetroTV.exe").write_bytes(b"newer")
            (older / "user-data").mkdir()
            (older / "user-data" / "settings.txt").write_text("retained", encoding="utf-8")
            (self.temporary / "previous-build.path").write_text(
                f"{newer}\n{older}\n", encoding="utf-8"
            )
            portable._import_previous_user_data(self.temporary)
            self.assertEqual(
                (self.temporary / "user-data" / "settings.txt").read_text(encoding="utf-8"),
                "retained",
            )
        finally:
            shutil.rmtree(older, ignore_errors=True)
            shutil.rmtree(newer, ignore_errors=True)

    def test_windows_build_deliberately_excludes_user_data(self):
        project = Path(__file__).resolve().parents[1]
        script = (project / "scripts" / "build-portable-windows.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("$packagedUserData = Join-Path $outputRoot 'user-data'", script)
        self.assertIn("Remove-Item -LiteralPath $resolvedUserData -Recurse -Force", script)
        self.assertNotIn("Copying the current database and generated state", script)
        self.assertNotIn("data\\retrotv.sqlite3') -Destination $portableDatabase", script)
        self.assertIn("--name CRTV-Sim-Safe-Stop", script)
        self.assertIn("CRTV-Sim-Safe-Stop.exe", script)
        self.assertNotIn("development.windows.local.settings.json", script)

    def test_portable_build_and_rollback_helpers_use_project_version(self):
        project = Path(__file__).resolve().parents[1]
        metadata = (project / "pyproject.toml").read_text(encoding="utf-8")
        version = re.search(r'^version = "([^"]+)"$', metadata, re.MULTILINE).group(1)
        build_name = f"CRTV-Sim-Portable-{version}"
        rollback_name = f"Rollback-CRTV-Sim-{version}.ps1"
        build_script = (project / "scripts" / "build-portable-windows.ps1").read_text(
            encoding="utf-8"
        )
        rollback_script = (project / "scripts" / "rollback-internet-media.ps1").read_text(
            encoding="utf-8"
        )
        rollback_command = (project / "scripts" / "rollback-internet-media.cmd").read_text(
            encoding="utf-8"
        )
        self.assertIn(build_name, build_script)
        self.assertIn(build_name, rollback_script)
        self.assertIn(rollback_name, rollback_command)

    def test_production_marker_failure_is_logged_without_modal_warning(self):
        source = (Path(portable.__file__)).read_text(encoding="utf-8")
        self.assertNotIn("def _show_warning", source)
        self.assertIn("file=sys.stderr", source)

    def test_production_drive_marking_uses_daemon_worker(self):
        config = object()
        with patch("retrotv.portable.threading.Thread") as thread:
            portable._start_production_marking(config, self.temporary)

        kwargs = thread.call_args.kwargs
        self.assertIs(kwargs["target"], portable._mark_production_storage)
        self.assertEqual(kwargs["args"], (config, self.temporary))
        self.assertTrue(kwargs["daemon"])
        thread.return_value.start.assert_called_once_with()

    def test_managed_base_defaults_preserve_legacy_inherited_preferences(self):
        defaults = self.temporary / "portable-defaults" / "user-data" / "config"
        (defaults / "base.example.toml").write_text(
            "schedule_version = 22\n", encoding="utf-8"
        )
        destination = self.temporary / "user-data" / "config"
        destination.mkdir(parents=True)
        (destination / "base.example.toml").write_text(
            "schedule_version = 17\n", encoding="utf-8"
        )
        settings = destination / "portable.windows.settings.json"
        settings.write_text('{"custom": true}', encoding="utf-8")

        portable._refresh_managed_defaults(self.temporary)

        self.assertEqual(
            (destination / "base.example.toml").read_text(encoding="utf-8"),
            "schedule_version = 17\n",
        )
        self.assertEqual(settings.read_text(encoding="utf-8"), '{"custom": true}')

    def test_environment_can_select_portable_root(self):
        with patch.dict(os.environ, {"RETROTV_PORTABLE_ROOT": str(self.temporary)}):
            self.assertEqual(portable.portable_root(), self.temporary.resolve())

    def test_legacy_profile_paths_migrate_but_dashboard_paths_are_preserved(self):
        config = self.temporary / "user-data" / "config"
        config.mkdir(parents=True)
        profile = config / "portable.windows.toml"
        profile.write_text(
            'profile = "windows-development"\n'
            '[storage_roots]\nmedia = "D:/Videos/media"\n'
            'originals_backup = "E:/Videos/retrotv-originals-backup"\n'
            '[player]\nmpv = "./tools/mpv.exe"\nbackend = "mpv"\n',
            encoding="utf-8",
        )
        settings = config / "portable.windows.settings.json"
        settings.write_text(
            json.dumps({
                "storage_roots": {
                    "media": "R:\\Videos\\media",
                    "originals_backup": "R:\\Videos\\retrotv-originals-backup",
                },
                "player": {"mpv": "tools/mpv.exe"},
            }),
            encoding="utf-8",
        )
        portable._migrate_portable_paths(self.temporary)
        migrated = profile.read_text(encoding="utf-8")
        self.assertIn('media = "D:/Videos/media"', migrated)
        self.assertIn(
            'originals_backup = "E:/Videos/retrotv-originals-backup"', migrated
        )
        self.assertIn('profile = "windows-portable"', migrated)
        self.assertIn('mpv = "./tools/mpv/mpv.exe"', migrated)
        overlay = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(overlay["storage_roots"]["media"], "R:\\Videos\\media")
        self.assertEqual(
            overlay["storage_roots"]["originals_backup"],
            "R:\\Videos\\retrotv-originals-backup",
        )
        self.assertEqual(overlay["player"]["mpv"], "./tools/mpv/mpv.exe")


if __name__ == "__main__":
    unittest.main()
