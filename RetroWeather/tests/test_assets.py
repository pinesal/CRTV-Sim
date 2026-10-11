from __future__ import annotations

from pathlib import Path

from _support import WorkspaceTestCase
from retroweather.assets import scan_asset_library


def valid_probe(path: Path) -> dict:
    if path.suffix.lower() in {".mp3", ".wav"}:
        return {"valid": True, "duration_seconds": 42.25, "codec": "fixture-audio", "format": "fixture"}
    if path.name.startswith("bad"):
        return {"valid": False, "error": "fixture corruption"}
    duration = 8.0 if path.suffix.lower() == ".mp4" else None
    return {
        "valid": True,
        "width": 640,
        "height": 480,
        "duration_seconds": duration,
        "codec": "fixture-video",
        "format": "fixture",
        "pixel_format": "rgba" if path.suffix.lower() == ".png" else "yuv420p",
    }


class AssetLibraryTests(WorkspaceTestCase):
    def test_inventory_classifies_supported_assets_and_metadata(self) -> None:
        media = self.temporary / "retroweather-media"
        files = (
            media / "graphics/conditions/rainy/day/rain.webp",
            media / "graphics/conditions/rainy/night/rain.mp4",
            media / "graphics/holidays/halloween/overlays/pumpkin.png",
            media / "music/default/theme.mp3",
        )
        for path in files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")

        result = scan_asset_library(media, valid_probe)

        self.assertEqual(result["summary"]["total"], 4)
        self.assertEqual(result["summary"]["valid"], 4)
        self.assertEqual(result["summary"]["still_images"], 2)
        self.assertEqual(result["summary"]["motion_backgrounds"], 1)
        self.assertEqual(result["summary"]["music"], 1)
        self.assertEqual(result["summary"]["holiday_assets"], 1)
        holiday = next(item for item in result["assets"] if item["role"].startswith("holiday_"))
        self.assertTrue(holiday["has_alpha"])
        self.assertTrue(holiday["usable_now"])
        motion = next(item for item in result["assets"] if item["media_type"] == "motion")
        self.assertTrue(motion["usable_now"])
        music = next(item for item in result["assets"] if item["media_type"] == "audio")
        self.assertEqual(music["duration_seconds"], 42.25)
        self.assertEqual(music["role"], "default_music")
        self.assertTrue(music["eligible"])
        self.assertTrue(music["usable_now"])

    def test_new_files_appear_without_restart_and_invalid_files_are_visible(self) -> None:
        media = self.temporary / "retroweather-media"
        folder = media / "graphics/conditions/sunny/day"
        folder.mkdir(parents=True)
        first = folder / "sun.png"
        first.write_bytes(b"fixture")
        self.assertEqual(scan_asset_library(media, valid_probe)["summary"]["total"], 1)

        bad = folder / "bad.webp"
        bad.write_bytes(b"broken")
        refreshed = scan_asset_library(media, valid_probe)

        self.assertEqual(refreshed["summary"]["total"], 2)
        self.assertEqual(refreshed["summary"]["invalid"], 1)
        invalid = next(item for item in refreshed["assets"] if item["name"] == "bad.webp")
        self.assertFalse(invalid["eligible"])
        self.assertEqual(invalid["error"], "fixture corruption")

    def test_wrong_file_type_and_unsupported_files_are_not_eligible(self) -> None:
        media = self.temporary / "retroweather-media"
        wrong = media / "music/default/poster.jpg"
        wrong.parent.mkdir(parents=True)
        wrong.write_bytes(b"fixture")
        (wrong.parent / "readme.txt").write_text("notes", encoding="utf-8")

        result = scan_asset_library(media, valid_probe)

        self.assertEqual(result["summary"]["unsupported_files"], 1)
        self.assertFalse(result["assets"][0]["valid"])
        self.assertFalse(result["assets"][0]["eligible"])
        self.assertIn("does not match", result["assets"][0]["error"])

    def test_opaque_holiday_artwork_is_rejected(self) -> None:
        media = self.temporary / "retroweather-media"
        overlay = media / "graphics/holidays/halloween/overlays/opaque.jpg"
        overlay.parent.mkdir(parents=True)
        overlay.write_bytes(b"fixture")

        result = scan_asset_library(media, valid_probe)

        asset = result["assets"][0]
        self.assertFalse(asset["valid"])
        self.assertFalse(asset["eligible"])
        self.assertIn("alpha channel", asset["error"])
