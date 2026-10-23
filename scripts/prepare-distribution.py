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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windows_toolchain import EvidenceError, frozen_inventory, sha256, verify_output, verify_source_companions, write_json

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from retrotv.media_folders import ensure_channel_scaffold
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
    ensure_channel_scaffold(root / 'shared', dayparts=SCOPES[:5], holidays=HOLIDAYS, shared=True)
    ensure_channel_scaffold(root / '_channel_template', dayparts=SCOPES[:5], holidays=HOLIDAYS)


def prepare(output, *, internal=False):
    media_evidence = verify_output(output)
    if media_evidence['evidence']['problems'] and not internal:
        raise EvidenceError('Public defaults blocked: media evidence is incomplete; use --internal only for development')
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
    shutil.copy2(PROJECT / 'CHANGELOG.md', output / 'CHANGELOG.md')
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
        'ffmpeg-COPYING.GPLv3.txt',
        'ffmpeg-COPYING.GPLv2.txt',
        'ffmpeg-COPYING.LGPLv2.1.txt',
        'ffmpeg-COPYING.LGPLv3.txt',
    }
    present_notices = {notice.name for notice in public_notices}
    missing_notices = required_notices - present_notices
    if missing_notices:
        raise ValueError(f'Missing bundled media-tool notices: {", ".join(sorted(missing_notices))}')
    for notice in public_notices:
        if notice.is_file():
            shutil.copy2(notice, notices / notice.name)
    shutil.copy2(PROJECT / 'LICENSE', output / 'LICENSE')
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
        if result.returncode != 0:
            raise EvidenceError(f'Bundled media executable failed: {relative}: {result.returncode}')
        if '--enable-nonfree' in result.stdout + result.stderr:
            raise EvidenceError(f'Nonredistributable FFmpeg configuration: {relative}')
        inventory['tools'][relative] = {'sha256': sha256_file(path), 'version_and_build': result.stdout + result.stderr}
    for path in sorted((output / 'tools').rglob('*.dll')):
        relative = path.relative_to(output).as_posix()
        inventory['tools'][relative] = {'sha256': sha256_file(path)}
    write(output / 'BUILD-INVENTORY.json', json.dumps(inventory, indent=2))
    frozen = frozen_inventory(output)
    manifest = {'schema': 1, 'mode': 'INTERNAL' if internal else 'public candidate',
                'media': media_evidence, 'frozen': frozen,
                'problems': sorted(set(media_evidence['evidence']['problems'] + frozen['problems']))}
    write_json(output / 'THIRD-PARTY-MANIFEST.json', manifest)
    if manifest['problems'] and not internal:
        raise EvidenceError('Public packaging blocked:\n- ' + '\n- '.join(manifest['problems']))
    if not internal:
        (output / 'INTERNAL-BUILD-NOT-FOR-DISTRIBUTION.txt').unlink(missing_ok=True)
        from release_reuse import source_reference
        reference = source_reference(output)
        if reference:
            write_json(output / 'SOURCE-DOWNLOADS.json', reference)
        else:
            (output / 'SOURCE-DOWNLOADS.json').unlink(missing_ok=True)
    if internal:
        (output / 'SOURCE-DOWNLOADS.json').unlink(missing_ok=True)
        write(output / 'INTERNAL-BUILD-NOT-FOR-DISTRIBUTION.txt',
              'Internal test candidate. Public distribution is blocked.\n\n' + '\n'.join(manifest['problems']) + '\n')
    # Only files prepared in this fresh build are eligible for archiving. This
    # also detects files introduced after preparation, not only user-data/.
    approved = {p.relative_to(output).as_posix(): sha256(p) for p in output.rglob('*') if p.is_file() and p.name != 'PACKAGE-FILES.json'}
    write_json(output / 'PACKAGE-FILES.json', approved)


def archive(output, version, *, internal=False):
    verify_output(output)
    manifest = json.loads((output / 'THIRD-PARTY-MANIFEST.json').read_text(encoding='utf-8'))
    if not internal and (manifest['problems'] or manifest['mode'] == 'INTERNAL'):
        raise EvidenceError('An internal/incomplete candidate cannot be packaged as a public release')
    approved = json.loads((output / 'PACKAGE-FILES.json').read_text(encoding='utf-8'))
    actual = {p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file() and p.name != 'PACKAGE-FILES.json'}
    if actual != set(approved):
        raise EvidenceError(f'Unapproved/missing package files: {sorted(actual ^ set(approved))}')
    for relative, checksum in approved.items():
        if sha256(output / relative) != checksum:
            raise EvidenceError(f'Package file changed after validation: {relative}')
        parts = Path(relative).parts
        if parts[0] in ('user-data', '.git', '.env', 'config') or relative.lower().endswith(('.sqlite3', '.sqlite3-wal', '.sqlite3-shm', '.key', '.pfx')):
            raise EvidenceError(f'Private/development file cannot be distributed: {relative}')
    label = version + ('-INTERNAL' if internal else '')
    reference = None
    if not internal and (output / 'SOURCE-DOWNLOADS.json').is_file():
        from release_reuse import validated_reference, supplement
        reference = validated_reference(output)
        source_files = [supplement(output, version, reference)]
    else:
        source_path = output.parent / f'CRTV-Sim-{label}-ThirdParty-Sources.zip'
        if not source_path.is_file():
            raise EvidenceError('Matching ThirdParty-Sources.zip is required before packaging the application')
        with zipfile.ZipFile(source_path) as source_zip:
            if source_zip.testzip() is not None:
                raise EvidenceError('Invalid source ZIP')
            source_manifest = json.loads(source_zip.read('SOURCE-MANIFEST.json'))
            if source_manifest['media'] != manifest['media']:
                raise EvidenceError('Binary/source mapping differs between application and source package')
            for name, record in source_manifest['sources'].items():
                with source_zip.open(name) as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != record['sha256']:
                        raise EvidenceError(f'Source archive checksum mismatch: {name}')
        source_files = [source_path, *verify_source_companions(source_path, source_manifest)]
    if any(path.is_file() for path in (output.parent / 'Media').rglob('*')):
        raise ValueError('The distribution Media scaffold contains files. Refusing to package potential personal media.')
    archive_path = output.parent / f'CRTV-Sim-{label}-Windows.zip'
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for tree in (output, output.parent / 'Media'):
            for path in sorted([tree, *tree.rglob('*')]):
                name = path.relative_to(output.parent).as_posix()
                if path.is_dir():
                    archive.writestr(name + '/', b'')
                else:
                    archive.write(path, name)
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None:
            raise EvidenceError('Windows ZIP integrity failure')
        names = archive.namelist()
        assert 'Media/_channel_template/specials/holidays/halloween/over-night/episodes/' in names
        assert not any(name.startswith(output.name + '/user-data/') for name in names)
    with archive_path.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    write(archive_path.with_suffix('.zip.sha256'), checksum + '  ' + archive_path.name + '\n')
    write(output.parent / f'CRTV-Sim-{label}-SHA256SUMS.txt',
          checksum + '  ' + archive_path.name + '\n' +
          ''.join(sha256(path)+'  '+path.name+'\n' for path in source_files))
    if not internal:
        from release_reuse import write_plan
        write_plan(output, version, [archive_path, *source_files], reference['sources'] if reference else ())
    print(f'Archive ready: {archive_path}; SHA256 {checksum}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    parser.add_argument('--archive', action='store_true')
    parser.add_argument('--internal', action='store_true', help='Explicitly label a development package; never public-ready')
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(PROJECT / 'dist'):
        raise ValueError('Output must be under project dist')
    if args.archive:
        import tomllib
        archive(output, tomllib.loads((PROJECT / 'pyproject.toml').read_text())['project']['version'], internal=args.internal)
    else:
        prepare(output, internal=args.internal)
