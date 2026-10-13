"""Run real encoder/filter/probe and mpv Windows IPC checks on a candidate.

Uses only disposable synthetic media. No production profile or media library.
"""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from retrotv.config import AppConfig
from retrotv.playback import MpvIpcPlayer


def check_weather_render(build, temporary, ffmpeg, ffprobe, env, motion):
    """Exercise actual Weather commands, publication, and holiday compositing."""
    sys.path.insert(0, str(PROJECT / "RetroWeather/src"))
    from retroweather.config import AppPaths, DEFAULT_SETTINGS, initialize
    from retroweather.models import NormalizedWeatherSnapshot
    from retroweather.planning import SegmentCue, build_forecast_plan
    from retroweather.rendering.static import (
        _probe_video, _render_segment, _validate_probe, build_screens, render_and_publish,
    )

    def runner(command, **kwargs):
        executable = {"ffmpeg": ffmpeg, "ffprobe": ffprobe}[command[0]]
        return subprocess.run(
            [str(executable), *command[1:]], env=env, encoding="utf-8",
            errors="replace", **kwargs,
        )

    paths = AppPaths.discover(temporary / "weather")
    initialize(paths)
    snapshot = NormalizedWeatherSnapshot.load(
        PROJECT / "RetroWeather/tests/fixtures/normalized-weather.json"
    )
    snapshot.save(paths.latest_snapshot)
    font = build / "assets/fonts/DejaVuSansMono.ttf"
    with patch("retroweather.rendering.static._font_paths", return_value=(font, font)):
        # Render the full two-minute, seven-screen broadcast, then run its real
        # format, seeking, manifest, and atomic publication acceptance checks.
        result = render_and_publish(paths, runner=runner, now=snapshot.current.observed_at)
        assert abs(result.duration_seconds - 120) <= 0.15
        assert (paths.output / "current.json").is_file()
        assert result.video.is_file() and result.manifest.is_file()

        # Also exercise the complex graph used by custom holiday artwork.
        overlay = temporary / "holiday-overlay.png"
        runner([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
            "-i", "color=black@0.0:s=640x480,format=rgba,"
            "drawbox=x=0:y=0:w=iw:h=ih:color=orange:t=8",
            "-frames:v", "1", str(overlay),
        ], capture_output=True, text=True, check=True, timeout=120)
        plan = build_forecast_plan(snapshot, DEFAULT_SETTINGS)
        screen = replace(
            build_screens(snapshot, plan)[1], background_asset=motion,
            holiday_overlay=overlay,
        )
        output = temporary / "holiday-weather.mp4"
        _render_segment(
            output, SegmentCue("holiday-check", "Holiday Check", 0.0, 0.75),
            screen, temporary, 99, runner,
        )
        _validate_probe(_probe_video(output, runner), 0.75)
    print("PASS: real Weather two-minute render/publication/seeking; motion background and holiday overlay")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build", type=Path)
    args = parser.parse_args()
    build = args.build.resolve()
    ffmpeg, ffprobe, mpv = [build / path for path in ("tools/ffmpeg/ffmpeg.exe", "tools/ffmpeg/ffprobe.exe", "tools/mpv/mpv.exe")]
    # Prove the selected DLL closure works without an installed media toolchain.
    env = {**os.environ, "PATH": str(Path(os.environ["SystemRoot"]) / "System32")}
    def run(command):
        result = subprocess.run([str(value) for value in command], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        if result.returncode:
            raise RuntimeError(result.stderr[-5000:])
        return result.stdout + result.stderr
    version = run([ffmpeg, "-version"])
    if "--enable-nonfree" in version:
        raise RuntimeError("--enable-nonfree makes this build unsuitable for redistribution")
    encoders = run([ffmpeg, "-hide_banner", "-encoders"])
    filters = run([ffmpeg, "-hide_banner", "-filters"])
    for encoder in ("libx264", "aac", "libmp3lame"):
        assert encoder in encoders, encoder
    for filter_name in ("crop", "scale", "pad", "setsar", "drawtext", "overlay", "loudnorm", "silencedetect", "blackdetect", "select", "showinfo", "concat"):
        assert filter_name in filters, filter_name
    base = PROJECT / ".test-tmp"
    base.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bundled-media-", dir=base) as folder:
        temporary = Path(folder)
        source, corrected, screen = [temporary / name for name in ("source.mp4", "corrected.mkv", "screen.mp4")]
        run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=640x480:r=25:d=4", "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-vf", "fade=t=out:st=2:d=0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", source])
        run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", source, "-vf", "crop=600:440:20:20,scale=320:240,pad=400:300:40:30,setsar=1", "-af", "loudnorm=I=-24:TP=-2:LRA=7", "-c:v", "libx264", "-c:a", "aac", corrected])
        metadata = json.loads(run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", corrected]))
        assert any(stream.get("codec_name") == "h264" and stream["width"] == 400 and stream["height"] == 300 for stream in metadata["streams"])
        analysis = run([ffmpeg, "-hide_banner", "-i", source, "-vf", "blackdetect=d=0.1:pix_th=0.15,select='gt(scene,0.01)',showinfo", "-af", "silencedetect=n=-35dB:d=0.1", "-f", "null", "-"])
        assert "black_start" in analysis
        font = (build / "assets/fonts/DejaVuSansMono.ttf").as_posix().replace(":", r"\:")
        if not (build / "assets/fonts/DejaVuSansMono.ttf").is_file():
            font = next((build / "assets/fonts").glob("*.ttf")).as_posix().replace(":", r"\:")
        run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=navy:s=640x480:r=25:d=1", "-f", "lavfi", "-i", "color=orange:s=40x40:r=25:d=1", "-filter_complex", f"[0:v][1:v]overlay=20:20,drawtext=fontfile='{font}':text='CRTV Sim Guide and Weather':fontsize=24:fontcolor=white", "-c:v", "libx264", "-pix_fmt", "yuv420p", screen])
        check_weather_render(build, temporary, ffmpeg, ffprobe, env, source)
        config = AppConfig(profile_path=temporary / "test.toml", project_root=temporary, data={"storage_roots": {"state": str(temporary / 'state')}, "platform": {"name": "windows"}, "player": {"foreground_on_start": False, "fullscreen": False, "fonts_dir": str(build / "assets/fonts"), "extra_args": ["--no-config", "--vo=null", "--ao=null", "--force-window=no"]}})
        player = MpvIpcPlayer(config, executable=str(mpv))
        original_path = os.environ['PATH']
        try:
            os.environ['PATH'] = env['PATH']
            player.start()
            player._command(["loadfile", str(source), "replace"])
            assert player.file_loaded.wait(10), "mpv did not load the MP4"
            player._command(["seek", 1.5, "absolute+exact"])
            player._command(["set_property", "pause", False])
            time.sleep(0.25)
            position = player._command(["get_property", "time-pos"])
            assert float(position) >= 1.4, position
            player._command(["set_property", "pause", True])
            player.file_loaded.clear()
            player._command(["loadfile", str(corrected), "replace"])
            assert player.file_loaded.wait(10), "mpv did not load the MKV"
        finally:
            player.stop()
            os.environ['PATH'] = original_path
        print("PASS: isolated media PATH; H.264/AAC; probing; crop/scale/aspect; loudness; black/scene/silence analysis; graphic/text/overlay render; MP4/MKV playback; Windows IPC; seek/join-in-progress; pause and stop")


if __name__ == "__main__":
    main()
