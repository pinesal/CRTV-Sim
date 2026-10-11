"""Weather artwork management and the shared CRTV Sim holiday calendar."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from retrotv.ai_announcements import OpenAIHTTPClient
from retrotv.credentials import configured_openai_api_key
from retrotv.onboarding import atomic_json
from retroweather.assets import MOTION_EXTENSIONS, STILL_EXTENSIONS, scan_asset_library, _probe_with_ffprobe
from retroweather.rendering.themes import THEMES


MAX_UPLOAD_BYTES = 40 * 1024 * 1024


def weather_home(config) -> Path:
    raw = str(config.data.get("synthetic_channels", {}).get("weather", {}).get("companion_home", "./RetroWeather"))
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    return (path if path.is_absolute() else config.project_root / path).resolve()


def holiday_calendar(config) -> dict:
    result = {}
    for slug, value in config.data.get("seasonal", {}).get("holidays", {}).items():
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(slug)) or not isinstance(value, dict):
            raise ValueError("Invalid configured holiday")
        result[slug] = {key: value[key] for key in ("start", "end", "enabled", "priority") if key in value}
    return result


def sync_weather_holidays(config) -> bool:
    """Write an atomic sidecar, leaving RetroWeather's own settings untouched."""
    target = weather_home(config) / "config/crtvsim-holidays.json"
    value = {"schema_version": 1, "holidays": holiday_calendar(config)}
    if target.is_file():
        try:
            if json.loads(target.read_text(encoding="utf-8")) == value:
                return False
        except (OSError, ValueError):
            pass
    atomic_json(target, value)
    return True


class WeatherGraphicsService:
    def __init__(self, config):
        self.config = config
        self.root = weather_home(config) / "retroweather-media"

    def slots(self) -> dict[str, dict]:
        result = {}
        for family in sorted(THEMES):
            for variant in ("day", "night"):
                key = f"{family}/{variant}"
                result[key] = {"key": key, "label": f"{family.replace('-', ' ').title()} — {variant}",
                               "folder": f"graphics/conditions/{key}", "overlay": False}
        result["alert"] = {"key": "alert", "label": "Weather alerts", "folder": "graphics/alerts", "overlay": False}
        for slug, holiday in holiday_calendar(self.config).items():
            key = f"holiday/{slug}"
            result[key] = {"key": key, "label": f"{slug.replace('-', ' ').title()} — transparent holiday layer",
                           "folder": f"graphics/holidays/{slug}/overlays", "overlay": True,
                           "start": holiday.get("start"), "end": holiday.get("end"), "enabled": holiday.get("enabled", True)}
        return result

    def slot(self, key: str) -> dict:
        try:
            return self.slots()[key]
        except KeyError as exc:
            raise ValueError("Choose a supported weather type or configured holiday") from exc

    def folder(self, key: str) -> Path:
        path = (self.root / self.slot(key)["folder"]).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("Weather graphics destination leaves the media root")
        return path

    def asset_path(self, key: str, filename: str) -> Path:
        folder = self.folder(key)
        if Path(filename).name != filename or "\\" in filename or filename.startswith("."):
            raise ValueError("Invalid weather artwork filename")
        path = (folder / filename).resolve()
        if not path.is_relative_to(folder) or path.suffix.lower() not in STILL_EXTENSIONS | MOTION_EXTENSIONS:
            raise ValueError("Invalid weather artwork path")
        return path

    def catalog(self) -> dict:
        inventory = scan_asset_library(self.root, probe=self.probe)
        slots = list(self.slots().values())
        for slot in slots:
            slot["assets"] = [asset for asset in inventory["assets"] if asset["folder"] == slot["folder"]]
        return {"slots": slots, "ai_available": bool(configured_openai_api_key()), "media_root": str(self.root)}

    def probe(self, path: Path) -> dict:
        return _probe_with_ffprobe(path, executable=self.config.ffprobe)

    def install(self, key: str, source: Path, *, generated=False) -> Path:
        slot = self.slot(key)
        details = self.probe(source)
        if not details.get("valid"):
            raise ValueError(details.get("error") or "Invalid artwork")
        if slot["overlay"]:
            pixel_format = str(details.get("pixel_format", ""))
            if source.suffix.lower() not in STILL_EXTENSIONS or not (
                pixel_format.startswith(("rgba", "bgra", "argb", "abgr", "yuva", "gbrap")) or pixel_format in {"ya8", "ya16be", "ya16le"}
            ):
                raise ValueError("Holiday layers must be still images with a transparent alpha channel")
            alpha = subprocess.run([self.config.ffmpeg, "-hide_banner", "-i", str(source), "-vf",
                "alphaextract,crop=iw*0.6:ih*0.6,signalstats,metadata=print", "-frames:v", "1", "-f", "null", "-"],
                capture_output=True, text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            average = re.search(r"lavfi.signalstats.YAVG=([0-9.]+)", alpha.stderr)
            if alpha.returncode or not average or float(average[1]) > 25:
                raise ValueError("Holiday layers need a mostly transparent center so forecasts remain visible")
        folder = self.folder(key)
        folder.mkdir(parents=True, exist_ok=True)
        name = "ai-weather.png" if generated else f"uploaded-{uuid.uuid4().hex}{source.suffix.lower()}"
        target = folder / name
        partial = folder / f".{uuid.uuid4().hex}.partial{source.suffix.lower()}"
        try:
            partial.write_bytes(source.read_bytes())
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return target

    def upload(self, payload: dict) -> Path:
        key = str(payload.get("key", ""))
        self.slot(key)
        suffix = Path(str(payload.get("filename", ""))).suffix.lower()
        if suffix not in STILL_EXTENSIONS | MOTION_EXTENSIONS:
            raise ValueError("Choose a supported background image or video")
        content = base64.b64decode(str(payload.get("content_base64", "")), validate=True)
        if not 0 < len(content) <= MAX_UPLOAD_BYTES:
            raise ValueError("Weather artwork must be between 1 byte and 40 MiB")
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".weather-upload-", dir=self.root) as temporary:
            source = Path(temporary) / f"input{suffix}"
            source.write_bytes(content)
            return self.install(key, source)

    def generate(self, payload: dict) -> Path:
        key = str(payload.get("key", ""))
        slot = self.slot(key)
        direction = str(payload.get("art_direction", "")).strip()
        if len(direction) > 1000:
            raise ValueError("Art direction is limited to 1,000 characters")
        if slot["overlay"]:
            prompt = (f"Create a transparent 1990s local television weather holiday border for {slot['label']}. "
                "Decorations only along the outer edges of a 4:3 screen, with a fully transparent large center. "
                "No opaque background, no panels, no text, no logos, no weather symbols in the center. "
                "Real alpha transparency, not a painted checkerboard. Keep at least the central 80% clear.")
        else:
            prompt = (f"Create a polished 1990s local television weather background illustrating {slot['label']}. "
                "4:3 composition. Distinct weather and time of day, restrained contrast behind forecast panels. "
                "No readable text, numbers, logos, charts or baked-in forecast UI. Artwork only.")
        if direction:
            prompt += " Creative direction: " + direction
        client = OpenAIHTTPClient(timeout_seconds=300)
        if not client.available:
            raise ValueError("Add your OpenAI API key in Settings to generate weather graphics")
        response = client.request_json("images/generations", {
            "model": self.config.data.get("graphics", {}).get("ai_image_lab_model", "gpt-image-2.5-flare"),
            "prompt": prompt, "size": "1536x1024", "quality": "low", "output_format": "png",
            "background": "transparent" if slot["overlay"] else "opaque", "n": 1,
        })
        data = response.get("data", []) if isinstance(response, dict) else []
        if not data or not isinstance(data[0], dict) or not isinstance(data[0].get("b64_json"), str):
            raise ValueError("The image provider did not return an image")
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".weather-generation-", dir=self.root) as temporary:
            source, prepared = Path(temporary) / "source.png", Path(temporary) / "prepared.png"
            source.write_bytes(base64.b64decode(data[0]["b64_json"], validate=True))
            result = subprocess.run([self.config.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(source), "-vf", "scale=640:480,format=rgba", "-frames:v", "1", str(prepared)],
                capture_output=True, text=True, timeout=90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode or not prepared.is_file():
                raise ValueError("Could not prepare the generated weather graphic")
            return self.install(key, prepared, generated=True)
