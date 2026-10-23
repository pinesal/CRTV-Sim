from __future__ import annotations

import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from retrotv.graphics_lab import (
    GraphicsLab,
    build_prompt,
    png_dimensions,
    write_composition_blueprint,
    write_layout_mask,
)


def _mask_alpha(path: Path, width: int, x: int, y: int) -> int:
    data = path.read_bytes()
    cursor = 8
    compressed = bytearray()
    while cursor < len(data):
        length = int.from_bytes(data[cursor:cursor + 4], "big")
        kind = data[cursor + 4:cursor + 8]
        chunk = data[cursor + 8:cursor + 8 + length]
        cursor += 12 + length
        if kind == b"IDAT":
            compressed.extend(chunk)
    raw = zlib.decompress(bytes(compressed))
    stride = width * 4 + 1
    return raw[y * stride + 1 + x * 4 + 3]


class GraphicsLabTests(unittest.TestCase):
    def test_unbranded_prompt_never_automatically_includes_station_identity(self):
        for purpose in ('up-next','interstitials'):
            for direction in ('', 'An abstract newsroom in blue and silver.'):
                with self.subTest(purpose=purpose, direction=direction):
                    prompt = build_prompt(method='composition-blueprint', template_set='composition-blueprint',
                        channel_name='Cbs News', art_direction=direction, has_logo_reference=False,
                        purpose=purpose, include_station_name=False)
                    self.assertNotIn('cbs', prompt.lower())
                    self.assertIn('unbranded background artwork', prompt)
                    self.assertNotIn('Place the generated station logo', prompt)
                    self.assertNotIn('station logo that reads exactly', prompt)
                    if purpose == 'up-next':
                        self.assertIn('central text-safe rectangle', prompt)
                    if direction:
                        self.assertIn(direction, prompt)

    def test_lab_creates_the_interstitial_blueprint_at_startup(self):
        root = Path.cwd() / ".test-tmp" / "graphics-lab-initialization"
        root.mkdir(parents=True, exist_ok=True)
        config = SimpleNamespace(
            project_root=Path.cwd(),
            data={"graphics": {}},
            ffmpeg="ffmpeg",
            storage_root=lambda _name: root,
        )
        with mock.patch.object(GraphicsLab, "_render_layout_guide"):
            lab = GraphicsLab(config)

        self.assertTrue(lab.composition_blueprint.is_file())
        self.assertTrue(lab.interstitial_blueprint.is_file())

    def test_template_prompt_preserves_the_real_text_panel(self):
        prompt = build_prompt(
            method="locked-template",
            template_set="complete",
            channel_name="PBS",
            art_direction="Blue-and-gold public television identity.",
            has_logo_reference=True,
        )
        self.assertIn("central text-safe rectangle", prompt)
        self.assertIn("alpha mask", prompt)
        self.assertIn("verified final Up Next example", prompt)
        self.assertIn("exact supplied logo", prompt)
        self.assertNotIn("invented logos", prompt.split("Do not generate", 1)[0])

    def test_mask_keeps_centre_opaque_and_perimeter_editable(self):
        path = Path.cwd() / ".test-tmp" / "graphics-lab-mask.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        self.addCleanup(path.unlink, missing_ok=True)
        write_layout_mask(path, width=640, height=480)
        self.assertEqual(png_dimensions(path), (640, 480))
        self.assertEqual(_mask_alpha(path, 640, 320, 240), 255)
        self.assertEqual(_mask_alpha(path, 640, 20, 240), 0)
        self.assertEqual(_mask_alpha(path, 640, 320, 40), 0)

    def test_complete_reference_prompt_labels_the_template_roles(self):
        prompt = build_prompt(
            method="template-reference",
            template_set="complete",
            channel_name="PBS",
            art_direction="Test direction.",
            has_logo_reference=False,
        )
        self.assertIn("Reference image 2 is the ordinary final-resolution", prompt)
        self.assertIn("Reference image 3 is the labeled layout guide", prompt)
        self.assertIn("Reference image 4 is a verified final Up Next example", prompt)

    def test_blueprint_prompt_requires_a_complete_visual_redesign(self):
        prompt = build_prompt(
            method="composition-blueprint",
            template_set="composition-blueprint",
            channel_name="PBS",
            art_direction="Public-television identity with paper-cut shapes.",
            has_logo_reference=False,
        )
        self.assertIn("complete visual redesign", prompt)
        self.assertIn("teal-and-purple palette", prompt)
        self.assertIn("sole art direction", prompt)
        path = Path.cwd() / ".test-tmp" / "graphics-lab-blueprint.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        self.addCleanup(path.unlink, missing_ok=True)
        write_composition_blueprint(path)
        self.assertEqual(png_dimensions(path), (640, 480))

    def test_interstitial_prompt_uses_the_full_frame_without_schedule_text(self):
        prompt = build_prompt(
            method="composition-blueprint",
            template_set="composition-blueprint",
            channel_name="PBS",
            art_direction="Bright paper-cut station card.",
            has_logo_reference=False,
            purpose="interstitials",
        )
        self.assertIn("station interstitial", prompt)
        self.assertIn("use the full frame", prompt)
        self.assertIn("Do not reserve or draw an empty centre badge", prompt)
        self.assertIn("deliberately blank reference contains no logo shape", prompt)
        self.assertIn('station logo that reads exactly "PBS"', prompt)

    def test_logo_reference_keeps_the_supplied_logo_as_the_source_of_truth(self):
        prompt = build_prompt(
            method="composition-blueprint",
            template_set="composition-blueprint",
            channel_name="PBS",
            art_direction="Test direction.",
            has_logo_reference=True,
            purpose="interstitials",
        )
        self.assertIn("Do not generate any logo or readable text", prompt)
        self.assertNotIn('station logo that reads exactly "PBS"', prompt)

    def test_generated_station_wordmark_uses_an_uppercase_channel_identity(self):
        prompt = build_prompt(
            method="composition-blueprint",
            template_set="composition-blueprint",
            channel_name="Pbs",
            art_direction="Test direction.",
            has_logo_reference=False,
            purpose="up-next",
        )
        self.assertIn('station logo that reads exactly "PBS"', prompt)
        self.assertNotIn('station logo that reads exactly "Pbs"', prompt)
