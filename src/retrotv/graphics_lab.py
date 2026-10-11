"""Local, source-only experiment lab for AI channel graphics.

This is deliberately separate from the Dashboard's production graphic flow.
It gives a designer a repeatable way to compare text generation, reference-image
editing, and constrained template editing before a recipe is adopted by CRTV Sim.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import secrets
import shutil
import subprocess
import traceback
import urllib.error
import urllib.request
import uuid
import zlib
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from retrotv.ai_announcements import OpenAIHTTPClient, OpenAIProviderError


LAB_METHODS = {
    "production-baseline", "composition-blueprint", "template-reference", "locked-template",
}
TEMPLATE_SETS = {"composition-blueprint", "master-only", "master-guide", "complete"}
GRAPHIC_PURPOSES = {"up-next", "interstitials"}
MAX_REQUEST_BYTES = 7 * 1024 * 1024
MAX_REFERENCE_BYTES = 5 * 1024 * 1024
_RUN_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-f0-9]{12}$")


def default_art_direction(channel_name: str) -> str:
    return (
        f"Create a distinct, polished 1990s television station identity for {channel_name}. "
        "Use bold broadcast-era geometry, textured gradients, and memorable perimeter details. "
        "The station should feel specific and intentional rather than like a generic retro poster."
    )


def build_prompt(
    *, method: str, template_set: str, channel_name: str, art_direction: str,
    has_logo_reference: bool, purpose: str = "up-next",
) -> str:
    """Build an inspectable prompt with production constraints kept explicit."""
    # Channel display names are friendly labels (for example, "Pbs"), while a
    # broadcast wordmark needs the canonical all-caps identity ("PBS").
    channel = channel_name.strip().upper() or "THE STATION"
    direction = art_direction.strip() or default_art_direction(channel)
    generated_logo = (
        f'Create one polished, legible station logo that reads exactly "{channel}". '
        "This is the only readable text permitted in the artwork. Integrate it into the requested "
        "station identity rather than placing it in an empty badge or generic frame. "
        if not has_logo_reference
        else "Do not generate any logo or readable text; CRTV Sim will composite the exact supplied logo locally. "
    )
    if purpose == "up-next":
        fixed = (
            "This is a 4:3 CRTV Sim Up Next background, not a poster. The final application "
            "will draw its own heading, three programme listings, and tagline over the centre. "
            f"Do not generate schedules, ratings, or UI controls. {generated_logo}"
            "Keep the central text-safe rectangle (x=56–584, y=92–408 on a "
            "640×480 canvas) quiet, dark, and high contrast. Keep essential details at least 8 "
            "pixels from the edge for CRT overscan."
        )
    elif purpose == "interstitials":
        logo_space = (
            "CRTV Sim will composite the exact supplied station logo at the centre after generation. "
            "Leave that area visually calm but fully illustrated; do not draw an empty badge, "
            "circle, plaque, frame, silhouette, blackout area, halo, or placeholder behind it. "
            if has_logo_reference
            else "Place the generated station logo prominently near the centre and integrate it "
            "with the scene. Do not reserve or draw an empty centre badge, circle, plaque, frame, "
            "silhouette, blackout area, halo, or placeholder. "
        )
        fixed = (
            "This is a 4:3 CRTV Sim station interstitial that holds or loops while a channel has "
            "a brief programming gap. There is no schedule-text overlay, so use the full frame "
            f"for a distinctive station identity. Do not generate ratings or UI controls. {generated_logo}"
            f"{logo_space}Keep essential details at least 8 "
            "pixels from the edge for CRT overscan."
        )
    else:
        raise ValueError("Unknown graphics purpose")
    if method == "production-baseline":
        reference = (
            "Design freely from the written direction. This deliberately reproduces the current "
            "text-only production approach for comparison."
        )
    elif method == "composition-blueprint":
        placement = "the top-centre logo zone and the location and clear readability of the central programme-text area"
        if purpose == "interstitials":
            placement = "only the 4:3 canvas; the deliberately blank reference contains no logo shape or placeholder"
        reference = (
            "Reference image 1 is a deliberately plain, monochrome composition blueprint. "
            "It is a spatial guide only, not art direction. Start a complete visual redesign: "
            "replace every color, shape, border, texture, motif, and decorative detail from "
            f"the blueprint. Preserve only the 4:3 canvas and {placement}."
        )
    elif method == "template-reference":
        reference = (
            "Reference image 1 is the existing 4:3 Up Next layout. Preserve its composition: "
            "the upper plaque is reserved for a station logo and the central panel is reserved "
            "for dynamic programme text. Restyle its palette, frame, and perimeter decoration "
            "without moving or filling those reserved areas."
        )
    elif method == "locked-template":
        reference = (
            "Reference image 1 is the existing 4:3 Up Next layout. The supplied alpha mask "
            "marks only the upper and perimeter artwork that may change. Preserve every opaque "
            "area exactly, especially the central text panel and its geometry."
        )
    else:
        raise ValueError("Unknown graphics-lab method")
    if method == "production-baseline":
        template_context = "No template images are included in this baseline request."
    elif method == "composition-blueprint":
        template_context = (
            "Do not imitate the standard CRTV Sim template, art-deco framing, teal-and-purple "
            "palette, gold stripes, corner shapes, or any other existing channel graphic. Use "
            "the creative direction as the sole art direction. The transparency mask allows all "
            "pixels to change; the composition constraints are the only fixed rules."
        )
    elif template_set == "master-only":
        template_context = "Only the high-resolution master is included for this comparison."
    elif template_set == "master-guide":
        template_context = (
            "Reference image 2 is the labeled layout guide. Treat its colored boxes and labels "
            "as non-visible production instructions; do not reproduce them in the result."
        )
    elif template_set == "complete":
        template_context = (
            "Reference image 2 is the ordinary final-resolution 640×480 background template. "
            "Reference image 3 is the labeled layout guide. Treat its colored boxes and labels "
            "as non-visible production instructions; do not reproduce them in the result. "
            "Reference image 4 is a verified final Up Next example that demonstrates the real "
            "schedule text placement. Do not copy its wording or make any text."
        )
    else:
        raise ValueError("Unknown template reference set")
    logo = (
        " Reference image 2 is a station-logo reference. Use its non-textual visual character "
        "only; CRTV Sim will composite the exact supplied logo after generation."
        if has_logo_reference
        else ""
    )
    return f"{direction}\n\n{fixed}\n\n{reference}\n\n{template_context}{logo}"


def _png_rgba(width: int, height: int, pixels: bytes) -> bytes:
    if len(pixels) != width * height * 4:
        raise ValueError("Invalid PNG pixel buffer")
    raw = b"".join(
        b"\x00" + pixels[row * width * 4:(row + 1) * width * 4]
        for row in range(height)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            len(data).to_bytes(4, "big") + kind + data
            + zlib.crc32(kind + data).to_bytes(4, "big")
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00")
        + chunk(b"IDAT", zlib.compress(raw, level=9))
        + chunk(b"IEND", b"")
    )


def write_layout_mask(path: Path, *, width: int = 1448, height: int = 1086) -> None:
    """Write an alpha mask that exposes only top/perimeter station artwork.

    Transparent pixels are editable. The central programme panel remains opaque,
    which gives template-edit experiments a strong visual constraint.
    """
    pixels = bytearray(width * height * 4)
    for y in range(height):
        y480 = y * 480 / height
        for x in range(width):
            x640 = x * 640 / width
            editable = y480 < 92 or y480 > 408 or x640 < 56 or x640 > 584
            alpha = 0 if editable else 255
            offset = (y * width + x) * 4
            pixels[offset:offset + 4] = b"\xff\xff\xff" + bytes((alpha,))
    path.write_bytes(_png_rgba(width, height, bytes(pixels)))


def write_composition_blueprint(path: Path, *, width: int = 640, height: int = 480) -> None:
    """Create an unbranded spatial guide without template artwork or colors."""
    pixels = bytearray(width * height * 4)
    for y in range(height):
        for x in range(width):
            color = (25, 30, 39)
            if 220 <= x < 420 and 12 <= y < 80:
                color = (74, 80, 91)
            if 56 <= x < 584 and 92 <= y < 408:
                color = (17, 22, 30)
            border = (
                (220 <= x < 420 and y in {12, 79})
                or (12 <= y < 80 and x in {220, 419})
                or (56 <= x < 584 and y in {92, 407})
                or (92 <= y < 408 and x in {56, 583})
            )
            if border:
                color = (142, 149, 162)
            offset = (y * width + x) * 4
            pixels[offset:offset + 4] = bytes((*color, 255))
    path.write_bytes(_png_rgba(width, height, bytes(pixels)))


def write_interstitial_blueprint(path: Path, *, width: int = 640, height: int = 480) -> None:
    """Create a deliberately featureless edit source for station interstitial art."""
    # The image edit API requires an image source.  Unlike an Up Next graphic, an
    # interstitial has no fixed text panel or logo plaque, so visual guide marks
    # became literal circles/frames in generated station art.  A uniform source
    # gives the model no shape to preserve while the prompt carries the purpose.
    path.write_bytes(_png_rgba(width, height, b"\x80\x80\x80\xff" * (width * height)))


def write_fully_editable_mask(path: Path, *, width: int = 640, height: int = 480) -> None:
    """Write a transparent alpha mask that permits a complete visual redesign."""
    path.write_bytes(_png_rgba(width, height, b"\xff\xff\xff\x00" * (width * height)))


def png_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()[:24]
    except OSError:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


class GraphicsLab:
    def __init__(self, config):
        self.config = config
        self.root = config.storage_root("state") / "graphics-lab"
        self.root.mkdir(parents=True, exist_ok=True)
        self.template = config.project_root / "assets" / "templates" / "up-next-background-template-master.png"
        self.background_template = config.project_root / "assets" / "templates" / "up-next-background-template.png"
        self.layout_example = config.project_root / "assets" / "templates" / "up-next-layout-example.png"
        self.layout_guide = config.project_root / "assets" / "templates" / "up-next-layout-guide.svg"
        self.font = config.project_root / "assets" / "fonts" / "VCR_OSD_MONO_1.001.ttf"
        if not self.template.is_file():
            raise RuntimeError(f"Missing Up Next template: {self.template}")
        self.mask = self.root / "up-next-edit-mask.png"
        if not self.mask.is_file():
            write_layout_mask(self.mask)
        self.composition_blueprint = self.root / "up-next-composition-blueprint.png"
        if not self.composition_blueprint.is_file():
            write_composition_blueprint(self.composition_blueprint)
        self.interstitial_blueprint = self.root / "interstitial-composition-blueprint-v2.png"
        if not self.interstitial_blueprint.is_file():
            write_interstitial_blueprint(self.interstitial_blueprint)
        self.fully_editable_mask = self.root / "up-next-fully-editable-mask.png"
        if not self.fully_editable_mask.is_file():
            write_fully_editable_mask(self.fully_editable_mask)
        self.raster_layout_guide = self.root / "up-next-layout-guide.png"
        if not self.raster_layout_guide.is_file():
            self._render_layout_guide()

    @property
    def default_model(self) -> str:
        return str(
            self.config.data.get("graphics", {}).get(
                "ai_image_lab_model", "gpt-image-2.5-flare"
            )
        )

    def list_runs(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for metadata in sorted(self.root.glob("*/metadata.json"), reverse=True):
            try:
                value = json.loads(metadata.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and _RUN_ID.fullmatch(str(value.get("id", ""))):
                results.append(value)
        return results[:100]

    def _render_layout_guide(self) -> None:
        """Rasterize the SVG guide into a reference image accepted by image edits.

        FFmpeg builds distributed with CRTV Sim do not decode SVG. The transparent
        regions and guide lines here intentionally mirror the source SVG; text
        labels are provided in the request prompt so the model never mistakes
        guide lettering for production artwork.
        """
        completed = subprocess.run(
            [str(self.config.ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(self.background_template), "-vf",
             "drawbox=x=220:y=12:w=200:h=68:color=yellow@0.20:t=fill,"
             "drawbox=x=220:y=12:w=200:h=68:color=yellow@0.95:t=2,"
             "drawbox=x=56:y=92:w=528:h=316:color=green@0.12:t=fill,"
             "drawbox=x=56:y=92:w=528:h=316:color=green@0.95:t=2,"
             "drawbox=x=8:y=8:w=624:h=464:color=white@0.75:t=1,"
             "drawbox=x=64:y=112:w=512:h=1:color=red@0.55:t=fill,"
             "drawbox=x=64:y=188:w=512:h=1:color=red@0.55:t=fill,"
             "drawbox=x=64:y=252:w=512:h=1:color=red@0.55:t=fill,"
             "drawbox=x=64:y=316:w=512:h=1:color=red@0.55:t=fill,"
             "drawbox=x=64:y=390:w=512:h=1:color=red@0.55:t=fill",
             "-frames:v", "1", str(self.raster_layout_guide)],
            capture_output=True, text=True, timeout=90, check=False,
        )
        if completed.returncode != 0 or not self.raster_layout_guide.is_file():
            detail = (completed.stderr or completed.stdout or "layout-guide rendering failed").strip()[-1000:]
            raise RuntimeError(f"Could not prepare the raster layout guide: {detail}")

    def _decode_logo(self, payload: dict[str, Any], run: Path) -> Path | None:
        logo = payload.get("logo")
        if not isinstance(logo, dict):
            return None
        data_url = str(logo.get("data_url", ""))
        match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=\s]+)", data_url)
        if not match:
            raise ValueError("Station-logo reference must be a PNG, JPEG, or WebP image")
        raw = base64.b64decode(match.group(2), validate=True)
        if not raw or len(raw) > MAX_REFERENCE_BYTES:
            raise ValueError("Station-logo reference must be between 1 byte and 5 MiB")
        extension = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}[match.group(1)]
        logo_path = run / f"station-logo{extension}"
        logo_path.write_bytes(raw)
        return logo_path

    @staticmethod
    def _json_error(exc: urllib.error.HTTPError) -> OpenAIProviderError:
        detail = exc.read().decode("utf-8", errors="replace")[-1500:]
        try:
            message = json.loads(detail).get("error", {}).get("message")
        except json.JSONDecodeError:
            message = None
        return OpenAIProviderError(message or f"OpenAI request failed with HTTP {exc.code}")

    @staticmethod
    def _multipart(
        fields: dict[str, str], files: list[tuple[str, Path, str]],
    ) -> tuple[bytes, str]:
        boundary = "----CRTVSimGraphicsLab" + secrets.token_hex(16)
        parts: list[bytes] = []
        for name, value in fields.items():
            parts.extend([
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"), b"\r\n",
            ])
        for name, path, content_type in files:
            safe_name = path.name.replace('"', "")
            parts.extend([
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"; filename="{safe_name}"\r\n'.encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                path.read_bytes(), b"\r\n",
            ])
        parts.append(f"--{boundary}--\r\n".encode())
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"

    def _request_multipart(
        self, client: OpenAIHTTPClient, path: str,
        fields: dict[str, str], files: list[tuple[str, Path, str]],
    ) -> dict[str, Any]:
        body, content_type = self._multipart(fields, files)
        request = urllib.request.Request(
            f"{client.base_url}/{path.lstrip('/')}", data=body,
            headers={
                "Authorization": f"Bearer {client.api_key}",
                "Content-Type": content_type,
                "User-Agent": "CRTV-Sim-Graphics-Lab/1.0",
            }, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=client.timeout_seconds, context=client.ssl_context) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise self._json_error(exc) from exc
        except urllib.error.URLError as exc:
            raise OpenAIProviderError(
                f"Unable to reach OpenAI using {client.ssl_backend} TLS trust: {exc.reason}"
            ) from exc

    def _prepare_background(self, source: Path, target: Path) -> None:
        completed = subprocess.run(
            [str(self.config.ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(source), "-vf", "scale=640:480", "-frames:v", "1", str(target)],
            capture_output=True, text=True, timeout=90, check=False,
        )
        if completed.returncode != 0 or not target.is_file():
            detail = (completed.stderr or completed.stdout or "image preparation failed").strip()[-1000:]
            raise RuntimeError(f"Could not prepare the 640×480 preview: {detail}")

    def _composite_logo(self, background: Path, logo: Path, target: Path, purpose: str) -> None:
        if purpose == "interstitials":
            logo_filter = (
                "[1:v]scale=300:150:force_original_aspect_ratio=decrease[logo];"
                "[0:v][logo]overlay=(W-w)/2:(H-h)/2:format=auto"
            )
        else:
            logo_filter = (
                "[1:v]scale=200:68:force_original_aspect_ratio=decrease[logo];"
                "[0:v][logo]overlay=(W-w)/2:12:format=auto"
            )
        completed = subprocess.run(
            [str(self.config.ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(background), "-i", str(logo), "-filter_complex",
             logo_filter,
             "-frames:v", "1", str(target)],
            capture_output=True, text=True, timeout=90, check=False,
        )
        if completed.returncode != 0 or not target.is_file():
            detail = (completed.stderr or completed.stdout or "logo compositing failed").strip()[-1000:]
            raise RuntimeError(f"Could not composite the exact station logo: {detail}")

    @staticmethod
    def _ass_escape(value: str) -> str:
        return value.replace("\\", r"\\").replace("{", "(").replace("}", ")").replace("\n", " ")

    def _schedule_preview(self, background: Path, target: Path, channel_name: str, run: Path) -> None:
        if not self.font.is_file():
            shutil.copy2(background, target)
            return
        shutil.copy2(self.font, run / self.font.name)
        heading = f"COMING UP NEXT ON {channel_name.upper()}..."
        lines = ["RETRO SHOW  -  8:00 PM", "ANOTHER SHOW  -  8:30 PM", "LATE NIGHT MOVIE  -  9:00 PM"]
        events = [f"Dialogue: 0,0:00:00.00,0:00:01.00,Header,,0,0,0,,{{\\fs{max(16, min(28, 830 // max(1, len(heading))))}\\pos(320,112)}}{self._ass_escape(heading)}"]
        for index, line in enumerate(lines):
            events.append(
                f"Dialogue: 0,0:00:00.00,0:00:01.00,Listing,,0,0,0,,{{\\fs{max(16, min(24, 800 // max(1, len(line))))}\\pos(320,{188 + index * 64})}}{self._ass_escape(line)}"
            )
        events.append("Dialogue: 0,0:00:00.00,0:00:01.00,Tagline,,0,0,0,,{\\fs20\\pos(320,390)}Your CRTV Sim station")
        font_name = "VCR OSD Mono"
        ass = run / "schedule-preview.ass"
        ass.write_text("\n".join([
            "[Script Info]", "ScriptType: v4.00+", "PlayResX: 640", "PlayResY: 480", "WrapStyle: 2", "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: Header,{font_name},28,&H00FFFFFF,&H00FFFFFF,&H00202020,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,5,64,64,20,1",
            f"Style: Listing,{font_name},24,&H00FFFFFF,&H00FFFFFF,&H00202020,&H80000000,0,0,0,0,100,100,0,0,1,2,1,5,64,64,20,1",
            f"Style: Tagline,{font_name},20,&H00FFFFFF,&H00FFFFFF,&H00202020,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,5,64,64,20,1", "",
            "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text", *events, "",
        ]), encoding="utf-8-sig")
        completed = subprocess.run(
            [str(self.config.ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(background), "-vf", "ass=schedule-preview.ass:fontsdir=.",
             "-frames:v", "1", str(target)],
            cwd=run, capture_output=True, text=True, timeout=90, check=False,
        )
        if completed.returncode != 0 or not target.is_file():
            shutil.copy2(background, target)

    def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        method = str(payload.get("method", "")).strip()
        if method not in LAB_METHODS:
            raise ValueError("Choose a supported generation method")
        purpose = str(payload.get("purpose", "up-next")).strip()
        if purpose not in GRAPHIC_PURPOSES:
            raise ValueError("Choose Up Next or station interstitial")
        template_set = str(payload.get("template_set", "complete")).strip()
        if template_set not in TEMPLATE_SETS:
            raise ValueError("Choose a supported template reference set")
        if purpose == "interstitials" and method != "production-baseline":
            method = "composition-blueprint"
        if method == "composition-blueprint":
            template_set = "composition-blueprint"
        elif template_set == "composition-blueprint":
            template_set = "complete"
        channel_name = str(payload.get("channel_name", "")).strip()
        if not channel_name or len(channel_name) > 80:
            raise ValueError("Station name must contain 1–80 characters")
        art_direction = str(payload.get("art_direction", "")).strip()
        if len(art_direction) > 4000:
            raise ValueError("Art direction is limited to 4,000 characters")
        model = str(payload.get("model", self.default_model)).strip()
        if not model or len(model) > 100:
            raise ValueError("Choose a valid image model")
        quality = str(payload.get("quality", "low")).strip().casefold()
        if quality not in {"low", "medium", "high", "auto"}:
            raise ValueError("Choose low, medium, high, or auto quality")
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:12]
        run = self.root / run_id
        # Windows maps the POSIX ``mode`` argument to an ACL. A restrictive
        # mode can prevent FFmpeg, which runs as a child process, from reading
        # the temporary reference/output files. Use the normal inherited ACL.
        run.mkdir()
        logo = self._decode_logo(payload, run)
        prompt = build_prompt(
            method=method, template_set=template_set, channel_name=channel_name, art_direction=art_direction,
            has_logo_reference=logo is not None, purpose=purpose,
        )
        client = OpenAIHTTPClient()
        if not client.available:
            raise ValueError("OpenAI API key is not configured; add it in Settings")
        if method == "production-baseline":
            response = client.request_json("images/generations", {
                "model": model, "prompt": prompt, "size": "1024x1024", "quality": quality,
                "output_format": "png", "background": "opaque", "n": 1,
            })
        else:
            fields = {
                "model": model, "prompt": prompt, "quality": quality,
                "output_format": "png", "background": "opaque", "size": "auto",
            }
            if model == "gpt-image-1":
                fields["input_fidelity"] = "high"
            if method == "composition-blueprint":
                blueprint = (
                    self.composition_blueprint if purpose == "up-next"
                    else self.interstitial_blueprint
                )
                files = [("image[]", blueprint, "image/png")]
            else:
                files = [("image[]", self.template, "image/png")]
                if template_set == "complete":
                    files.append(("image[]", self.background_template, "image/png"))
                if template_set in {"master-guide", "complete"}:
                    files.append(("image[]", self.raster_layout_guide, "image/png"))
                if template_set == "complete":
                    files.append(("image[]", self.layout_example, "image/png"))
            if logo is not None:
                files.append(("image[]", logo, mimetypes.guess_type(logo.name)[0] or "image/png"))
            if method == "composition-blueprint":
                files.append(("mask", self.fully_editable_mask, "image/png"))
            elif method == "locked-template":
                files.append(("mask", self.mask, "image/png"))
            response = self._request_multipart(client, "images/edits", fields, files)
        data = response.get("data") if isinstance(response, dict) else None
        encoded = data[0].get("b64_json") if isinstance(data, list) and data and isinstance(data[0], dict) else None
        if not isinstance(encoded, str):
            raise RuntimeError("OpenAI did not return a PNG image")
        source = run / "model-output.png"
        source.write_bytes(base64.b64decode(encoded, validate=True))
        prepared = run / "background-640x480.png"
        self._prepare_background(source, prepared)
        final = prepared
        if logo is not None and bool(payload.get("composite_logo")):
            final = run / "background-with-exact-logo.png"
            self._composite_logo(prepared, logo, final, purpose)
        schedule_preview = run / "schedule-preview.png"
        if purpose == "up-next":
            self._schedule_preview(final, schedule_preview, channel_name, run)
        else:
            shutil.copy2(final, schedule_preview)
        result = {
            "id": run_id,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "method": method,
            "template_set": template_set,
            "purpose": purpose,
            "channel_name": channel_name,
            "model": model,
            "quality": quality,
            "prompt": prompt,
            "logo_reference": logo.name if logo else None,
            "exact_logo_composited": bool(logo is not None and payload.get("composite_logo")),
            "source_dimensions": png_dimensions(source),
            "output_dimensions": png_dimensions(final),
            "background_url": f"/runs/{run_id}/{final.name}",
            "schedule_preview_url": f"/runs/{run_id}/{schedule_preview.name}",
            "preview_label": "With actual Up Next text" if purpose == "up-next" else "Interstitial playback frame",
            "source_url": f"/runs/{run_id}/{source.name}",
        }
        (run / "metadata.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result


_HTML = r"""<!doctype html><meta charset="utf-8"><title>CRTV Sim Graphics Lab</title>
<style>
body{margin:0;background:#0b1018;color:#e6edf7;font:15px/1.45 system-ui,sans-serif}main{max-width:1320px;margin:auto;padding:28px}.panel{background:#111a26;border:1px solid #2c3b50;border-radius:10px;padding:18px;margin:0 0 18px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}.results{display:grid;grid-template-columns:repeat(auto-fit,minmax(390px,1fr));gap:16px}.result{border:1px solid #2c3b50;border-radius:9px;padding:12px;background:#0f1722}.comparison{display:grid;grid-template-columns:1fr 1fr;gap:8px}.comparison img{width:100%;image-rendering:auto;background:#05070a}label{display:grid;gap:5px;color:#b8c7dc}input,textarea,select,button{font:inherit;padding:9px;border-radius:6px;border:1px solid #354960;background:#0a111a;color:#e6edf7}textarea{min-height:150px;resize:vertical}.wide{grid-column:1/-1}.actions{display:flex;gap:9px;align-items:center;flex-wrap:wrap}button{background:#174b38;border-color:#348965;cursor:pointer}button:disabled{opacity:.55;cursor:wait}.muted{color:#a8b7c9}.error{color:#ff9d9d}.tag{display:inline-block;background:#202e40;border-radius:99px;padding:2px 8px;margin:0 5px 5px 0;font-size:12px}details{margin-top:9px}code{white-space:pre-wrap;font-size:12px;display:block;max-height:240px;overflow:auto;background:#080d14;padding:10px}@media(max-width:700px){main{padding:14px}.comparison{grid-template-columns:1fr}}
</style><main>
<h1>CRTV Sim Graphics Lab</h1>
<p class="muted">Source-only experiment workspace. Runs never enter channel graphics or the media library. Compare the art-only background with the actual Up Next text layout before adopting a recipe.</p>
<section class="panel"><div class="grid">
<label>Station identity<input id="channel" value="PBS"></label>
<label>Graphic purpose<select id="purpose"><option value="up-next" selected>Up Next background</option><option value="interstitials">Station interstitial</option></select></label>
<label>Method<select id="method"><option value="production-baseline">Current text-only baseline</option><option value="composition-blueprint" selected>Composition guide, fresh art — recommended</option><option value="template-reference">Edit the original 4:3 template — control</option><option value="locked-template">Original template + locked centre — control</option></select></label>
<label>Template references<select id="template-set"><option value="composition-blueprint" selected>Neutral composition blueprint only</option><option value="master-only">Original master only</option><option value="master-guide">Original master + layout guide</option><option value="complete">Original template set + finished example</option></select></label>
<label>Image model<input id="model" value="__MODEL__"></label>
<label>Quality<select id="quality"><option value="low" selected>Low — inexpensive test</option><option value="medium">Medium</option><option value="high">High</option><option value="auto">Auto</option></select></label>
<label>Station logo reference <input id="logo" type="file" accept="image/png,image/jpeg,image/webp"></label>
<label><span>Exact logo treatment</span><span><input id="composite" type="checkbox" checked> Composite the supplied logo locally after generation</span></label>
<label class="wide">Creative direction<textarea id="direction">__DIRECTION__</textarea></label>
</div><div class="actions"><button id="generate">Generate one comparison</button><span id="message" class="muted">Each click makes one billed image request using the configured API key.</span></div>
<details><summary>What each method tests</summary><p><strong>Composition guide, fresh art</strong> is the recommended test. For Up Next it fixes only the logo and programme-text positions; for an interstitial it reserves only a centred station-logo zone and allows the rest of the 4:3 frame to be redesigned. It specifically forbids the current art-deco shapes, teal-and-purple palette, and gold stripes. <strong>Original template</strong> and <strong>locked template</strong> remain Up Next comparison controls so we can measure whether the actual templates cause style carry-over. The reference-set selector applies only to those controls. If you supply a real logo, the lab can use it as a visual reference and then place that exact file in the appropriate logo zone after generation.</p></details></section>
<section id="results" class="results"></section></main>
<script>
const $=id=>document.getElementById(id);let logoData=null;
function esc(v){const s=String(v??'');const d=document.createElement('div');d.textContent=s;return d.innerHTML}
async function readLogo(){const f=$('logo').files[0];if(!f)return null;if(f.size>5*1024*1024)throw Error('Logo reference must be 5 MiB or smaller');return {name:f.name,data_url:await new Promise((ok,no)=>{const r=new FileReader();r.onload=()=>ok(r.result);r.onerror=no;r.readAsDataURL(f)})}}
function card(r){return `<article class="result"><div><span class="tag">${esc(r.purpose||'up-next')}</span><span class="tag">${esc(r.method)}</span><span class="tag">${esc(r.template_set||'master-only')}</span><span class="tag">${esc(r.model)}</span><span class="tag">${esc(r.quality)}</span>${r.exact_logo_composited?'<span class="tag">exact logo overlay</span>':''}</div><p><strong>${esc(r.channel_name)}</strong> · ${esc(r.created_at)}<br><span class="muted">Model ${esc((r.source_dimensions||[]).join('×'))} → CRTV Sim ${esc((r.output_dimensions||[]).join('×'))}</span></p><div class="comparison"><figure><figcaption>Background</figcaption><img src="${r.background_url}" alt="Generated station background"></figure><figure><figcaption>${esc(r.preview_label||'With actual Up Next text')}</figcaption><img src="${r.schedule_preview_url}" alt="CRTV Sim playback preview"></figure></div><details><summary>Exact request</summary><code>${esc(r.prompt)}</code></details></article>`}
async function load(){const r=await fetch('/api/runs');const data=await r.json();$('results').innerHTML=(data.runs||[]).map(card).join('')||'<p class="muted">No experiments yet.</p>'}
function syncTemplateSet(){const interstitial=$('purpose').value==='interstitials';if(interstitial)$('method').value='composition-blueprint';$('method').disabled=interstitial;const blueprint=interstitial||$('method').value==='composition-blueprint';$('template-set').disabled=blueprint;if(blueprint)$('template-set').value='composition-blueprint';else if($('template-set').value==='composition-blueprint')$('template-set').value='complete'}$('method').addEventListener('change',syncTemplateSet);$('purpose').addEventListener('change',syncTemplateSet);syncTemplateSet();
$('generate').onclick=async()=>{const b=$('generate'),m=$('message');try{b.disabled=true;m.className='muted';m.textContent='Generating…';const logo=await readLogo();const r=await fetch('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({purpose:$('purpose').value,method:$('method').value,template_set:$('template-set').value,channel_name:$('channel').value,model:$('model').value,quality:$('quality').value,art_direction:$('direction').value,logo,composite_logo:$('composite').checked})});const data=await r.json();if(!r.ok)throw Error(data.error||'Generation failed');m.textContent='Saved comparison '+data.id;await load()}catch(e){m.className='error';m.textContent=e.message}finally{b.disabled=false}};load();
</script>"""


def make_handler(config) -> type[BaseHTTPRequestHandler]:
    lab = GraphicsLab(config)

    class Handler(BaseHTTPRequestHandler):
        server_version = "CRTV-Sim-Graphics-Lab/1.0"

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

        def _json(self, value: dict[str, Any], status: int = 200) -> None:
            raw = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _file(self, path: Path, content_type: str) -> None:
            try:
                raw = path.read_bytes()
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/":
                raw = _HTML.replace("__MODEL__", lab.default_model).replace(
                    "__DIRECTION__", default_art_direction("PBS")
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if path == "/api/runs":
                self._json({"runs": lab.list_runs()})
                return
            if path == "/template/background":
                self._file(lab.template, "image/png")
                return
            if path == "/template/guide":
                self._file(lab.layout_guide, "image/svg+xml")
                return
            match = re.fullmatch(r"/runs/([^/]+)/([A-Za-z0-9_.-]+)", path)
            if match and _RUN_ID.fullmatch(match.group(1)):
                candidate = (lab.root / match.group(1) / match.group(2)).resolve()
                root = (lab.root / match.group(1)).resolve()
                if candidate.parent == root and candidate.suffix.casefold() == ".png":
                    self._file(candidate, "image/png")
                    return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] != "/api/generate":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_REQUEST_BYTES:
                    raise ValueError("Graphics-lab request must be between 1 byte and 7 MiB")
                value = json.loads(self.rfile.read(size))
                if not isinstance(value, dict):
                    raise ValueError("Graphics-lab request must be a JSON object")
                self._json(lab.generate(value), HTTPStatus.CREATED)
            except (ValueError, RuntimeError, OpenAIProviderError, OSError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:  # Keep browser diagnostics actionable.
                traceback.print_exc()
                self._json({"error": f"Graphics Lab internal error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    return Handler


def serve_graphics_lab(config, *, host: str = "127.0.0.1", port: int = 8786) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(config))
    url_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    print(f"CRTV Sim Graphics Lab: http://{url_host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
