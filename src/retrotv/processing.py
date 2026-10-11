"""Shared processing preferences, including compatibility defaults for old profiles."""
PRESETS = {"480p": (640, 480), "720p": (1280, 720),
           "1080p": (1920, 1080), "4K": (3840, 2160)}


def enabled(config, section):
    # Old profiles predate master switches; their existing behavior is retained.
    return bool(config.data.get(section, {}).get("enabled", True))


def require_enabled(config, section):
    if not enabled(config, section):
        name = {"normalization": "Video normalization", "audio_normalization": "Audio normalization",
                "commercial_tails": "Commercial black-tail trimming"}.get(section, section)
        raise ValueError(f"{name} is disabled. Enable it in Settings to use this action.")


def resolution_preset(data):
    settings = data.get("normalization", {})
    size = (settings.get("canvas_width", 640), settings.get("canvas_height", 480))
    return next((name for name, dimensions in PRESETS.items() if size == dimensions), "Custom")
