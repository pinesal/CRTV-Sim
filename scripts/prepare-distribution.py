"""Prepare clean public defaults, templates, notices and a verifiable ZIP.

This script never reads an installed profile or media library.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SCOPES = ('morning', 'afternoon', 'evening', 'night', 'over-night', 'daytime', 'all-day')
TYPES = ('episodes', 'shorts', 'movies', 'blocks', 'commercials', 'bumpers', 'promos', 'filler', 'station-ids')
HOLIDAYS = ('halloween', 'christmas')


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding='utf-8')


def sha256_file(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def scaffold(root):
    for channel in ('shared', '_channel_template'):
        base = root / channel
        regular_types = TYPES if channel != 'shared' else ('episodes', 'commercials', 'bumpers', 'promos', 'filler')
        holiday_types = TYPES if channel != 'shared' else tuple(value for value in TYPES if value != 'blocks')
        for scope in SCOPES:
            for category in regular_types:
                (base / scope / category).mkdir(parents=True, exist_ok=True)
            for purpose in ('up-next', 'interstitials'):
                (base / 'graphics' / scope / purpose).mkdir(parents=True, exist_ok=True)
                for holiday in HOLIDAYS:
                    (base / 'graphics' / scope / 'holidays' / holiday / purpose).mkdir(parents=True, exist_ok=True)
        for holiday in HOLIDAYS:
            for scope in SCOPES:
                for category in holiday_types:
                    (base / 'specials/holidays' / holiday / scope / category).mkdir(parents=True, exist_ok=True)
            (base / 'specials/holidays' / holiday / 'specials/weekly').mkdir(parents=True, exist_ok=True)
            for purpose in ('up-next', 'interstitials'):
                (base / 'graphics/holidays' / holiday / purpose).mkdir(parents=True, exist_ok=True)
        (base / 'specials/weekly').mkdir(parents=True, exist_ok=True)
        for purpose in ('up-next', 'interstitials'):
            (base / 'graphics' / purpose).mkdir(parents=True, exist_ok=True)


def prepare(output):
    defaults = output / 'portable-defaults/user-data'
    # No production profiles, provider customization or fixed TV blocks.
    # Always start from the source defaults. Repackaging an existing output
    # must not append duplicate sections or feature-enable keys.
    base = (PROJECT / 'config/base.example.toml').read_text(encoding='utf-8')
    for before, after in {
        'database_path = "./data/retrotv.sqlite3"': 'database_path = "./user-data/database/retrotv.sqlite3"',
        'ffmpeg = "ffmpeg"': 'ffmpeg = "./tools/ffmpeg/ffmpeg.exe"',
        'ffprobe = "ffprobe"': 'ffprobe = "./tools/ffmpeg/ffprobe.exe"',
        'normalized = "./data/normalized"': 'normalized = "./user-data/normalized"',
        'previews = "./data/previews"': 'previews = "./user-data/previews"',
        'originals_backup = "./data/originals-backup"': 'originals_backup = "./user-data/originals-backup"',
        'output = "./data/generated/guide.mp4"': 'output = "./user-data/generated/guide.mp4"',
        'root = "./RetroWeather/synthetic-channels/weather"': 'root = "./user-data/RetroWeather/synthetic-channels/weather"',
        'companion_home = "./RetroWeather"': 'companion_home = "./user-data/RetroWeather"',
        'output_dir = "./data/generated/announcements"': 'output_dir = "./user-data/generated/announcements"',
        'output_dir = "./data/generated/graphics"': 'output_dir = "./user-data/generated/graphics"',
        'mpv = "mpv"': 'mpv = "./tools/mpv/mpv.exe"',
    }.items():
        base = base.replace(before, after)
    sections = re.split(r'(?m)(?=^\[)', base)
    base = ''.join(section for section in sections if not section.startswith((
        '[seasonal.windows.', '[announcements.channels.', '[daypart_branding.', '[guide.channel_numbers]')))
    base = base.replace('channels = ["fox", "nickelodeon", "cartoon-network", "mtv", "discovery"]', 'channels = []')
    base = base.replace('"night", "daytime"', '"night", "over-night", "daytime"')
    base = base.replace('startup_channel = "fox"', 'startup_channel = "__guide__"')
    base = base.replace('osd_font = "VCR OSD Mono"', 'osd_font = "DejaVu Sans Mono"')
    base = base.replace('[normalization]\n', '[normalization]\nenabled = false\n')
    base = base.replace('[audio_normalization]\n', '[audio_normalization]\nenabled = false\n')
    base += '\n[commercial_tails]\nenabled = false\n'
    base = re.sub(r'(\[synthetic_channels.weather\]\s*)enabled = true', r'\1enabled = false', base)
    import tomllib
    tomllib.loads(base)
    write(defaults / 'config/base.example.toml', base)
    profile = '''extends = "base.example.toml"
profile = "windows-portable"
[storage_roots]
media = "../Media"
state = "./user-data/state"
normalized = "./user-data/normalized"
previews = "./user-data/previews"
originals_backup = "./user-data/originals-backup"
[platform]
name = "windows"
player_ipc = "windows"
service = "foreground"
input_adapters = ["keyboard"]
[player]
mpv = "./tools/mpv/mpv.exe"
backend = "mpv"
'''
    for slug, start, end in (('morning','06:00','12:00'),('afternoon','12:00','16:00'),('evening','16:00','20:00'),('night','20:00','00:00'),('over-night','00:00','06:00')):
        profile += f'\n[[scheduling.dayparts]]\nslug = "{slug}"\nstart = "{start}"\nend = "{end}"\nboundary_policy = "must_fit"\n'
    write(defaults / 'config/portable.windows.toml', profile)
    weather = defaults / 'RetroWeather'
    if weather.exists():
        shutil.rmtree(weather)
    write(weather / 'config/settings.json', (PROJECT / 'RetroWeather/config/settings.example.json').read_text(encoding='utf-8'))
    for name in ('data', 'retroweather-media/graphics', 'retroweather-media/music', 'synthetic-channels/weather'):
        (weather / name).mkdir(parents=True, exist_ok=True)
    fonts = output / 'assets/fonts'
    shutil.rmtree(fonts)
    shutil.copytree(PROJECT / 'assets/public-fonts', fonts)
    # Download packages must not silently import an unrelated sibling build.
    (output / 'previous-build.path').unlink(missing_ok=True)
    media = output.parent / 'Media'
    if media.exists():
        if not media.resolve().is_relative_to(PROJECT / 'dist') or any(p.is_file() for p in media.rglob('*')):
            raise ValueError('Refusing to replace a Media scaffold containing files or outside dist')
        # This is the build-owned, verified empty scaffold, never a user library.
        shutil.rmtree(media)
    scaffold(media)
    shutil.copy2(PROJECT / 'docs/DISTRIBUTION.md', output / 'README-PORTABLE.txt')
    shutil.copy2(PROJECT / 'docs/PRIVACY-AND-SERVICES.md', output / 'PRIVACY-AND-SERVICES.md')
    shutil.copy2(PROJECT / 'docs/THIRD-PARTY.md', output / 'THIRD-PARTY.md')
    shutil.copy2(PROJECT / 'docs/BUNDLED-SOFTWARE-RELEASE-CHECKLIST.md', output / 'BUNDLED-SOFTWARE-RELEASE-CHECKLIST.md')
    notices = output / 'licenses'
    notices.mkdir(exist_ok=True)
    public_notices = list((PROJECT / 'assets/public-notices').glob('*'))
    required_notices = {
        'mpv-Copyright.txt',
        'mpv-LICENSE.GPL.txt',
        'Vulkan-Loader-LICENSE.txt',
    }
    present_notices = {notice.name for notice in public_notices}
    missing_notices = required_notices - present_notices
    if missing_notices:
        raise ValueError(f'Missing bundled media-tool notices: {", ".join(sorted(missing_notices))}')
    for notice in public_notices:
        shutil.copy2(notice, notices / notice.name)
    inventory = {'python': sys.version, 'platform': platform.platform(), 'packages': {}, 'tools': {}}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get('Name', 'unknown')
        inventory['packages'][name] = distribution.version
        for file in distribution.files or []:
            if any(part.lower().startswith(('license', 'copying', 'notice')) for part in Path(str(file)).parts):
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    destination = notices / name / Path(str(file)).name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.exists():
        shutil.copy2(python_license, notices / 'Python-LICENSE.txt')
    for relative in ('tools/ffmpeg/ffmpeg.exe', 'tools/ffmpeg/ffprobe.exe', 'tools/mpv/mpv.exe'):
        path = output / relative
        result = subprocess.run([str(path), '-version' if 'ffm' in relative or 'ffprobe' in relative else '--version'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
        inventory['tools'][relative] = {'sha256': sha256_file(path), 'version_and_build': result.stdout + result.stderr}
    for path in sorted((output / 'tools').rglob('*.dll')):
        relative = path.relative_to(output).as_posix()
        inventory['tools'][relative] = {'sha256': sha256_file(path)}
    write(output / 'BUILD-INVENTORY.json', json.dumps(inventory, indent=2))


def archive(output, version):
    if any(path.is_file() for path in (output.parent / 'Media').rglob('*')):
        raise ValueError('The distribution Media scaffold contains files. Refusing to package potential personal media.')
    archive_path = output.parent / f'CRTV-Sim-{version}-Windows.zip'
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for tree in (output, output.parent / 'Media'):
            for path in sorted([tree, *tree.rglob('*')]):
                name = path.relative_to(output.parent).as_posix()
                if path.is_dir():
                    archive.writestr(name + '/', b'')
                else:
                    archive.write(path, name)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert 'Media/_channel_template/specials/holidays/halloween/over-night/episodes/' in names
        assert not any(name.startswith(output.name + '/user-data/') for name in names)
    with archive_path.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    write(archive_path.with_suffix('.zip.sha256'), checksum + '  ' + archive_path.name + '\n')
    print(f'Archive ready: {archive_path}; SHA256 {checksum}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    parser.add_argument('--archive', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(PROJECT / 'dist'):
        raise ValueError('Output must be under project dist')
    if args.archive:
        import tomllib
        archive(output, tomllib.loads((PROJECT / 'pyproject.toml').read_text())['project']['version'])
    else:
        prepare(output)
