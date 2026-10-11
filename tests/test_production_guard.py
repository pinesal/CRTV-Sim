import json
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from retrotv.config import AppConfig
from retrotv.production_guard import (
    MARKER_NAME,
    format_conflicts,
    mark_production_volumes,
    production_conflicts,
)


class ProductionGuardTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / ".test-tmp"
        root.mkdir(exist_ok=True)
        self.temporary = root / f"production-guard-{uuid.uuid4().hex}"
        self.temporary.mkdir()
        self.volume = self.temporary / "volume"
        self.volume.mkdir()
        self.config = AppConfig(
            profile_path=self.temporary / "development.toml",
            project_root=self.temporary,
            data={
                "profile": "windows-development",
                "application": {"database_path": str(self.volume / "data/retrotv.sqlite3")},
                "storage_roots": {
                    "media": str(self.volume / "media"),
                    "state": str(self.volume / "state"),
                },
                "internet_media": {"cache_directory": "internet-cache"},
            },
        )

    def tearDown(self):
        shutil.rmtree(self.temporary, ignore_errors=True)

    def test_portable_marks_volume_and_development_finds_every_writable_path(self):
        (self.volume / "RetroTV").mkdir()
        with patch("retrotv.production_guard.volume_root", return_value=self.volume):
            failures = mark_production_volumes(
                self.config, portable_root=self.volume / "RetroTV"
            )
            conflicts = production_conflicts(self.config)
        self.assertEqual(failures, [])
        marker = json.loads((self.volume / MARKER_NAME).read_text(encoding="utf-8"))
        self.assertEqual(marker["environment"], "production")
        self.assertEqual(len(conflicts), 4)
        warning = format_conflicts(conflicts)
        self.assertIn("development build", warning)
        self.assertIn(str(self.volume / "media"), warning)

    def test_non_development_profile_does_not_warn(self):
        portable = AppConfig(
            self.config.profile_path,
            self.config.project_root,
            {**self.config.data, "profile": "windows-portable"},
        )
        self.assertEqual(production_conflicts(portable), [])

    def test_portable_folder_marker_protects_descendant_paths(self):
        portable_root = self.volume / "RetroTV-Portable"
        portable_root.mkdir()
        guarded = AppConfig(
            self.config.profile_path,
            self.config.project_root,
            {
                **self.config.data,
                "application": {
                    "database_path": str(
                        portable_root / "user-data/database/retrotv.sqlite3"
                    )
                },
                "storage_roots": {
                    "state": str(portable_root / "user-data/state"),
                },
                "internet_media": {},
            },
        )
        root_marker = portable_root / MARKER_NAME
        root_marker.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "product": "RetroTV",
                    "environment": "production",
                }
            ),
            encoding="utf-8",
        )

        conflicts = production_conflicts(guarded)

        self.assertEqual(len(conflicts), 2)
        self.assertTrue(all(item.marker_path == root_marker for item in conflicts))

    def test_marker_failures_do_not_prevent_portable_fallback(self):
        portable_root = self.volume / "RetroTV-Portable"
        portable_root.mkdir()
        real_atomic_write = __import__(
            "retrotv.production_guard", fromlist=["_write_json_atomic"]
        )._write_json_atomic

        def deny_volume_root(path, payload):
            if path.parent == self.volume:
                raise PermissionError("drive root is protected")
            real_atomic_write(path, payload)

        with (
            patch("retrotv.production_guard.volume_root", return_value=self.volume),
            patch(
                "retrotv.production_guard._write_json_atomic",
                side_effect=deny_volume_root,
            ),
        ):
            failures = mark_production_volumes(
                self.config, portable_root=portable_root
            )

        self.assertEqual(len(failures), 1)
        self.assertTrue((portable_root / MARKER_NAME).is_file())


if __name__ == "__main__":
    unittest.main()
