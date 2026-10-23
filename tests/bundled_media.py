"""Find current portable tools without depending on a deleted test release."""
import os
from pathlib import Path


def bundled_tools(project):
    selected = os.environ.get('CRTV_SIM_TEST_BUILD')
    if selected:
        return Path(selected).resolve() / 'tools'
    dist = project / 'dist'
    builds = [*dist.glob('CRTV-Sim-Portable*'),
              *dist.glob('release-candidate-*/CRTV-Sim-Portable*')]
    for build in sorted(builds, key=lambda path: path.stat().st_mtime_ns, reverse=True):
        tools = build / 'tools'
        if (tools / 'ffmpeg/ffmpeg.exe').is_file() and (tools / 'mpv/mpv.exe').is_file():
            return tools
    return dist / 'unavailable-bundled-tools'
