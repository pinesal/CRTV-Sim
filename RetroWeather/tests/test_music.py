from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

from _support import WorkspaceTestCase
from retroweather.rendering.music import (
    MusicAsset,
    build_music_catalog,
    select_music,
    soundtrack_filter,
)
from retroweather.rendering.static import _apply_soundtrack, _probe_video, _validate_probe


class MusicRenderingTests(WorkspaceTestCase):
    def test_validated_catalog_and_condition_default_precedence(self) -> None:
        media = self.temporary / "media"
        inventory = {
            "assets": [
                {
                    "relative_path": "music/default/default.mp3", "role": "default_music",
                    "media_type": "audio", "eligible": True, "duration_seconds": 90.0,
                },
                {
                    "relative_path": "music/conditions/rainy/rain.mp3", "role": "condition_music",
                    "media_type": "audio", "eligible": True, "duration_seconds": 60.0,
                },
                {
                    "relative_path": "music/conditions/rainy/broken.mp3", "role": "condition_music",
                    "media_type": "audio", "eligible": False, "duration_seconds": 60.0,
                },
            ]
        }
        with patch("retroweather.rendering.music.scan_asset_library", return_value=inventory):
            catalog = build_music_catalog(media)

        rainy = select_music(media, ("sunny", "rainy"), "generation", catalog)
        fallback = select_music(media, ("sunny",), "generation", catalog)

        self.assertEqual(rainy.path.name, "rain.mp3")
        self.assertEqual(rainy.source, "rainy")
        self.assertEqual(fallback.path.name, "default.mp3")
        self.assertEqual(
            rainy,
            select_music(media, ("rainy",), "generation", catalog),
        )
        self.assertIsNone(select_music(media, ("sunny",), "generation", {}))

    def test_crossfade_normalization_volume_and_whole_loop_fades(self) -> None:
        input_count, stream_loop, graph, strategy = soundtrack_filter(
            96.588,
            120.0,
            {
                "volume": 0.28, "target_lufs": -18,
                "fade_in_seconds": 1.5, "fade_out_seconds": 2.0,
                "crossfade_seconds": 1.0,
            },
        )

        self.assertEqual(input_count, 2)
        self.assertFalse(stream_loop)
        self.assertEqual(strategy, "crossfade-2x")
        self.assertIn("[1:a][2:a]acrossfade", graph)
        self.assertIn("acrossfade=d=1.000", graph)
        self.assertIn("loudnorm=I=-18.0", graph)
        self.assertIn("volume=0.2800", graph)
        self.assertIn("afade=t=in:st=0:d=1.500", graph)
        self.assertIn("afade=t=out:st=118.000:d=2.000", graph)
        self.assertIn("atrim=duration=120.000", graph)

    def test_real_music_mix_and_missing_music_both_produce_valid_aac(self) -> None:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("FFmpeg and FFprobe are required for audio integration")
        silent = self.temporary / "silent.mp4"
        track = self.temporary / "track.wav"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=blue:s=640x480:r=30:d=3",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", str(silent),
            ],
            check=True,
        )
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.4",
                "-c:a", "pcm_s16le", str(track),
            ],
            check=True,
        )
        mixed = self.temporary / "mixed.mp4"
        strategy = _apply_soundtrack(
            silent,
            mixed,
            MusicAsset(track, "default", 1.4),
            3.0,
            {"volume": 0.3, "fade_in_seconds": 0.2, "fade_out_seconds": 0.2},
            subprocess.run,
        )

        self.assertEqual(strategy, "stream-loop")
        self.assertAlmostEqual(_validate_probe(_probe_video(mixed, subprocess.run), 3.0), 3.0, delta=0.15)
        measured = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(mixed), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, check=True,
        )
        self.assertNotIn("mean_volume: -inf", measured.stderr)

        silent_source = self.temporary / "silent-source.mp4"
        shutil.copy2(mixed, silent_source)
        silent_output = self.temporary / "silence-fallback.mp4"
        self.assertEqual(
            _apply_soundtrack(silent_source, silent_output, None, 3.0, {}, subprocess.run),
            "silence",
        )
        self.assertAlmostEqual(
            _validate_probe(_probe_video(silent_output, subprocess.run), 3.0), 3.0, delta=0.15
        )
