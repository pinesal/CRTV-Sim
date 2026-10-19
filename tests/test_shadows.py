import hashlib
import json
import shutil
import unittest
import uuid
from pathlib import Path

from retrotv.config import AppConfig
from retrotv.db import apply_migrations, connect_database
from retrotv.probe import DecodeResult, ProbeResult
from retrotv.scanner import LibraryScanner
from retrotv.scheduling import ScheduleBuilder
from retrotv.shadows import ShadowService


class ShadowInspector:
    def probe(self, path: Path) -> ProbeResult:
        return ProbeResult(
            True,
            {
                "container_format": "mp4", "duration_seconds": 1320,
                "bit_rate": 500000, "video_codec": "h264", "audio_codec": "aac",
                "width": 640, "height": 480, "frame_rate": "30/1",
                "sample_aspect_ratio": "1:1", "display_aspect_ratio": "4:3",
                "pixel_format": "yuv420p", "field_order": "progressive",
                "audio_sample_rate": 48000, "audio_channels": 2,
            },
            raw_json="{}",
        )

    def sample(self, path: Path, start_seconds: float) -> DecodeResult:
        return DecodeResult(True)

    def fingerprint(self, path: Path) -> tuple[str, str]:
        return hashlib.sha256(path.read_bytes()).hexdigest(), "test-sha256"


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(__file__).resolve().parents[1]
        root = self.project / ".test-tmp"
        root.mkdir(exist_ok=True)
        self.temporary = root / f"shadows-{uuid.uuid4().hex}"
        self.media = self.temporary / "media"
        self.media.mkdir(parents=True)
        (self.media / "nickelodeon").mkdir()
        for number in (1, 2):
            path = self.media / f"fox/evening/episodes/The Simpsons/Season 1/S01E0{number}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"episode-{number}".encode())
        self.config = AppConfig(
            profile_path=self.temporary / "test.toml",
            project_root=self.project,
            data={
                "profile": "test",
                "application": {"database_path": str(self.temporary / "db.sqlite3")},
                "storage_roots": {"media": str(self.media)},
                "scanner": {"video_extensions": [".mp4"]},
                "library": {"channels": ["fox", "nickelodeon"], "holidays": []},
                "guide": {"channel_number": 2},
                "scheduling": {
                    "dayparts": [
                        {"slug": "morning", "start": "06:00", "end": "12:00"},
                        {"slug": "afternoon", "start": "12:00", "end": "16:00"},
                        {"slug": "evening", "start": "16:00", "end": "20:00"},
                        {"slug": "night", "start": "20:00", "end": "06:00"},
                    ]
                },
            },
        )
        self.connection = connect_database(self.config.database_path)
        apply_migrations(self.connection, self.project / "migrations")
        LibraryScanner(
            self.connection, self.config, inspector=ShadowInspector()
        ).scan()

    def tearDown(self):
        self.connection.close()
        shutil.rmtree(self.temporary, ignore_errors=True)

    def test_folder_creation_preserves_tree_and_adds_channel_eligibility(self):
        service = ShadowService(self.connection, self.config)
        result = service.create(
            folder="fox/evening/episodes/The Simpsons",
            channel="nickelodeon", scope="daypart", value="night",
        )
        self.assertEqual(len(result["created"]), 2)
        shadow = self.media / (
            "nickelodeon/night/episodes/The Simpsons/Season 1/S01E01.mp4.shadow"
        )
        self.assertTrue(shadow.is_file())
        payload = json.loads(shadow.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["target"],
            "fox/evening/episodes/The Simpsons/Season 1/S01E01.mp4",
        )
        candidates = ScheduleBuilder(self.connection, self.config)._candidates(
            "nickelodeon", "night", ("episode",)
        )
        self.assertEqual(len(candidates), 2)
        self.assertTrue(all(item.series == "The Simpsons" for item in candidates))
        self.assertTrue(all(item.placement_scope == "night" for item in candidates))
        self.assertTrue(all(item.placement_origin == "shadow" for item in candidates))
        scopes = ScheduleBuilder(self.connection, self.config)._recurring_source_scopes(
            "nickelodeon"
        )
        self.assertEqual(
            [(item.series, item.scope, item.dayparts) for item in scopes],
            [("The Simpsons", "night", frozenset({"night"}))],
        )

    def test_same_pool_shadow_does_not_multiply_candidate(self):
        asset_id = int(self.connection.execute(
            "SELECT id FROM assets ORDER BY id LIMIT 1"
        ).fetchone()["id"])
        service = ShadowService(self.connection, self.config)
        service.create(
            asset_id=asset_id, channel="fox", scope="daypart", value="evening"
        )
        candidates = ScheduleBuilder(self.connection, self.config)._candidates(
            "fox", "evening", ("episode",)
        )
        self.assertEqual(len(candidates), 2)

    def test_shadow_reuses_human_named_channel_folder(self):
        human_folder = self.media / "Cartoon Network"
        human_folder.mkdir()
        self.config.data["library"]["channels"].append("cartoon-network")
        LibraryScanner(
            self.connection, self.config, inspector=ShadowInspector()
        ).scan()
        asset_id = int(self.connection.execute(
            "SELECT id FROM assets WHERE channel_slug='fox' ORDER BY id LIMIT 1"
        ).fetchone()["id"])

        result = ShadowService(self.connection, self.config).create(
            asset_id=asset_id,
            channel="cartoon-network",
            scope="daypart",
            value="night",
        )

        self.assertEqual(len(result["created"]), 1)
        self.assertTrue(
            (human_folder / "night" / "episodes").is_dir()
        )
        self.assertFalse((self.media / "cartoon-network").exists())

    def test_invalid_target_enters_shadow_review(self):
        path = self.media / "nickelodeon/night/episodes/Test/missing.mp4.shadow"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"version": 1, "target": "fox/night/episodes/missing.mp4"}),
            encoding="utf-8",
        )
        result = ShadowService(self.connection, self.config).refresh()
        self.assertEqual(result.review, 1)
        row = self.connection.execute(
            "SELECT status,detail FROM shadow_files WHERE shadow_relative_path LIKE '%missing.mp4.shadow'"
        ).fetchone()
        self.assertEqual(row["status"], "review")
        self.assertIn("not found", row["detail"])

    def test_delete_one_broken_shadow_from_bulk_creation_preserves_other_placements(self):
        service = ShadowService(self.connection, self.config)
        for channel in ("nickelodeon", "fox"):
            service.create(
                folder="fox/evening/episodes/The Simpsons",
                channel=channel, scope="daypart", value="night",
            )
        original = self.media / "fox/evening/episodes/The Simpsons/Season 1/S01E01.mp4"
        original.unlink()
        LibraryScanner(
            self.connection, self.config, inspector=ShadowInspector()
        ).scan()
        service.refresh()
        items = service.inventory()["items"]
        self.assertEqual(len(items), 4)
        self.assertEqual(sum(item["status"] == "review" for item in items), 2)
        selected = next(
            item for item in items
            if item["status"] == "review" and item["channel_slug"] == "nickelodeon"
        )
        other_contents = {
            item["shadow_relative_path"]:
                (self.media / item["shadow_relative_path"]).read_bytes()
            for item in items if item["id"] != selected["id"]
        }
        remaining_original = original.with_name("S01E02.mp4")
        original_bytes = remaining_original.read_bytes()

        preview = service.deletion_preview([selected["id"]])
        self.assertEqual(preview["count"], 1)
        self.assertTrue((self.media / selected["shadow_relative_path"]).is_file())
        result = service.delete(preview["shadow_ids"], preview["token"])

        self.assertEqual(result["deleted"], [selected["shadow_relative_path"]])
        self.assertEqual(result["failed"], [])
        self.assertFalse((self.media / selected["shadow_relative_path"]).exists())
        self.assertEqual(len(service.inventory()["items"]), 3)
        self.assertEqual(service.inventory()["summary"]["review"], 1)
        for relative, contents in other_contents.items():
            self.assertEqual((self.media / relative).read_bytes(), contents)
        self.assertEqual(remaining_original.read_bytes(), original_bytes)

    def test_previewed_shadow_deletion_removes_only_sidecars(self):
        service = ShadowService(self.connection, self.config)
        created = service.create(
            folder="fox/evening/episodes/The Simpsons",
            channel="nickelodeon", scope="daypart", value="night",
        )
        self.assertEqual(len(created["created"]), 2)
        ids = [
            int(row["id"])
            for row in self.connection.execute(
                "SELECT id FROM shadow_files ORDER BY id"
            )
        ]
        preview = service.deletion_preview(ids)
        self.assertEqual(preview["count"], 2)
        changed_path = self.media / str(created["created"][0])
        changed_path.write_text(
            changed_path.read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "changed"):
            service.delete(ids, preview["token"])
        preview = service.deletion_preview(ids)
        with self.assertRaisesRegex(ValueError, "changed"):
            service.delete(ids, "wrong-token")

        result = service.delete(ids, preview["token"])
        self.assertEqual(len(result["deleted"]), 2)
        self.assertEqual(result["failed"], [])
        self.assertTrue(result["original_media_untouched"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM shadow_files").fetchone()[0],
            0,
        )
        self.assertTrue(
            (self.media / "fox/evening/episodes/The Simpsons/Season 1/S01E01.mp4").is_file()
        )


if __name__ == "__main__":
    unittest.main()
