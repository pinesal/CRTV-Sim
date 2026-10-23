"""Hash-bound reuse of published source archives and optional media downloads."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import shutil
from pathlib import Path
import sys
from urllib.request import Request, urlopen
import zipfile

from windows_toolchain import EvidenceError, sha256, verify_output, verify_source_companions, write_json

PROJECT = Path(__file__).resolve().parents[1]
REGISTRY = PROJECT / 'packaging/reusable-release-assets.json'
IGNORED_PACKAGES = {'retrotv', 'retroweather', 'pip', 'setuptools'}


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def python_sources():
    records = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get('Name', 'unknown')
        if name.lower() in IGNORED_PACKAGES:
            continue
        for member in distribution.files or []:
            relative = Path(str(member))
            if relative.is_absolute() or '..' in relative.parts or relative.suffix == '.pyc':
                continue
            path = Path(distribution.locate_file(member))
            if path.is_file():
                records[name + '/' + relative.as_posix()] = sha256(path)
    return identity(records)


def compatibility(output, project=PROJECT):
    manifest = json.loads((output / 'THIRD-PARTY-MANIFEST.json').read_text(encoding='utf-8'))
    if manifest['mode'] != 'public candidate' or manifest['problems']:
        raise EvidenceError('Source reuse requires complete public packaging evidence')
    native = {}
    for name, item in manifest['frozen']['native_binaries'].items():
        if name in {'CRTV-Sim.exe', 'CRTV-Sim-Safe-Stop.exe'}:
            continue
        path = (output / name).resolve()
        if not path.is_relative_to(output.resolve()) or sha256(path) != item['sha256']:
            raise EvidenceError('Changed frozen dependency: ' + name)
        native[name] = item['sha256']
    return {
        'media': identity(manifest['media']), 'native': identity(native),
        'python_sources': python_sources(),
        'locks': {name:sha256(project / name) for name in (
            'packaging/windows-media.lock.json', 'packaging/microsoft-runtime.lock.json',
            'requirements.lock', 'requirements-build.lock')},
    }


def asset_metadata(repository, tag):
    request = Request(f'https://api.github.com/repos/{repository}/releases/tags/{tag}',
                      headers={'User-Agent':'CRTV-Sim-source-reuse', 'Accept':'application/vnd.github+json'})
    with urlopen(request, timeout=30) as response:
        release = json.load(response)
    if release['draft']:
        raise EvidenceError('Reused assets must already be publicly downloadable')
    return {a['name']:a for a in release['assets']}


def verify_remote(records, repository):
    releases = {}
    for record in records:
        tag = record['tag']
        if not tag or '/' in tag or '..' in tag or Path(record['name']).name != record['name']:
            raise EvidenceError('Invalid source release tag')
        if tag not in releases:
            releases[tag] = asset_metadata(repository, tag)
        asset = releases[tag].get(record['name'])
        expected_url = f"https://github.com/{repository}/releases/download/{tag}/{record['name']}"
        if (not asset or record['url'] != expected_url or asset['browser_download_url'] != expected_url
                or asset.get('digest') != 'sha256:' + record['sha256'] or asset['size'] != record['size']):
            raise EvidenceError('Published reusable asset is missing or changed: ' + record['name'])


def source_reference(output, registry_path=REGISTRY):
    if not registry_path.is_file():
        return None
    registry = json.loads(registry_path.read_text(encoding='utf-8'))
    if registry.get('schema') != 1:
        raise EvidenceError('Unsupported reusable-asset registry')
    current = compatibility(output)
    for bundle in registry['source_bundles']:
        if current == bundle['compatibility']:
            return {'schema':1, 'repository':registry['repository'],
                    'compatibility':bundle['compatibility'], 'sources':bundle['assets']}
    return None


def validated_reference(output, registry_path=REGISTRY):
    stored = json.loads((output / 'SOURCE-DOWNLOADS.json').read_text(encoding='utf-8'))
    expected = source_reference(output, registry_path)
    if expected is None or stored != expected:
        raise EvidenceError('Reused source references do not match current dependencies and registry')
    verify_remote(stored['sources'], stored['repository'])
    return stored


def supplement(output, version, reference):
    """Keep current recipes/notices available without copying upstream code again."""
    target = output.parent / f'CRTV-Sim-{version}-Build-Materials.zip'
    paths = [*sorted((PROJECT / 'scripts').glob('*.py')), *sorted((PROJECT / 'scripts').glob('*.ps1')),
             *sorted((PROJECT / 'packaging').glob('*.json')),
             PROJECT / 'requirements.lock', PROJECT / 'requirements-build.lock',
             PROJECT / 'docs/THIRD-PARTY.md', PROJECT / 'docs/BUNDLED-SOFTWARE-RELEASE-CHECKLIST.md']
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, 'release-recipes/' + path.relative_to(PROJECT).as_posix())
        for path in sorted((output / 'licenses').rglob('*')):
            if path.is_file():
                archive.write(path, path.relative_to(output).as_posix())
        archive.write(output / 'MICROSOFT-RUNTIME.json', 'MICROSOFT-RUNTIME.json')
        archive.writestr('SOURCE-DOWNLOADS.json', json.dumps(reference, indent=2))
        archive.writestr('README.txt', 'Current build recipes and notices. Upstream sources are the unchanged, '
                         'hash-verified downloads listed in SOURCE-DOWNLOADS.json. Retain their availability '
                         'and link them beside the Windows download. This supplement does not replace them.\n')
    return target


def write_plan(output, version, uploads, reused_sources=(), registry_path=REGISTRY):
    registry = json.loads(registry_path.read_text(encoding='utf-8')) if registry_path.is_file() else None
    uploads = list(uploads)
    reused = list(reused_sources)
    for record in registry['media_packs'] if registry else []:
        if Path(record['name']).name != record['name']:
            raise EvidenceError('Invalid media-pack filename')
        local = PROJECT / 'dist' / record['name']
        if local.is_file() and sha256(local) != record['sha256']:
            sidecar = local.with_suffix('.zip.sha256')
            if not sidecar.is_file() or sidecar.read_text().split()[0].lower() != sha256(local):
                raise EvidenceError('Changed media pack needs a matching checksum: ' + record['name'])
            with zipfile.ZipFile(local) as archive:
                if archive.testzip() is not None:
                    raise EvidenceError('Changed media pack is corrupt: ' + record['name'])
            destination = output.parent / local.name
            if destination.resolve() != local.resolve():
                if destination.exists() and sha256(destination) != sha256(local):
                    raise EvidenceError('Choose a fresh candidate directory for changed media')
                shutil.copy2(local, destination)
            uploads.append(destination)
        else:
            reused.append(record)
    if reused:
        verify_remote(reused, registry['repository'])
    records = [{'name':p.name, 'sha256':sha256(p), 'size':p.stat().st_size} for p in uploads]
    names = [r['name'] for r in records + reused]
    if len(names) != len(set(names)):
        raise EvidenceError('Release downloads must have unique names')
    checksum = output.parent / f'CRTV-Sim-{version}-SHA256SUMS.txt'
    checksum.write_text(''.join(r['sha256'] + '  ' + r['name'] + '\n' for r in records + reused), encoding='utf-8')
    records.append({'name':checksum.name, 'sha256':sha256(checksum), 'size':checksum.stat().st_size})
    plan = {'schema':1, 'version':version, 'repository':registry['repository'] if registry else 'pinesal/CRTV-Sim',
            'output':output.name, 'upload':records, 'reuse':reused}
    write_json(output.parent / 'RELEASE-DOWNLOADS.json', plan)
    return plan


def register(output, tag, repository, media, destination=REGISTRY):
    """Bootstrap from an existing complete public release; no upload or edits."""
    verify_output(output)
    native_compat = compatibility(output)
    archive_path = output.parent / f'CRTV-Sim-{tag.removeprefix("v")}-ThirdParty-Sources.zip'
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read('SOURCE-MANIFEST.json'))
        if manifest['media'] != json.loads((output / 'THIRD-PARTY-MANIFEST.json').read_text())['media']:
            raise EvidenceError('Baseline binary/source mapping differs')
        for name, record in manifest['sources'].items():
            with archive.open(name) as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != record['sha256']:
                    raise EvidenceError('Baseline source is corrupt: ' + name)
        python = {name.removeprefix('python-packages/'):hashlib.sha256(archive.read(name)).hexdigest()
                  for name in archive.namelist() if name.startswith('python-packages/') and not name.endswith('/')}
        if identity(python) != native_compat['python_sources']:
            raise EvidenceError('Installed Python sources differ from baseline archive')
    companions = verify_source_companions(archive_path, manifest)

    def record(path):
        return {'name':path.name, 'tag':tag,
                'url':f'https://github.com/{repository}/releases/download/{tag}/{path.name}',
                'sha256':sha256(path), 'size':path.stat().st_size}

    sources = [record(p) for p in (archive_path, *companions)]
    packs = [record(p) for p in media]
    verify_remote(sources + packs, repository)
    result = json.loads(destination.read_text()) if destination.is_file() else {
        'schema':1, 'repository':repository, 'source_bundles':[], 'media_packs':[]}
    if result['repository'] != repository:
        raise EvidenceError('Cannot mix repositories in the reusable-download registry')
    if not any(b['compatibility'] == native_compat for b in result['source_bundles']):
        result['source_bundles'].append({'compatibility':native_compat, 'assets':sources})
    if packs:
        result['media_packs'] = packs
    write_json(destination, result)
    return result


def register_media(tag, repository, media, destination=REGISTRY):
    registry = json.loads(destination.read_text(encoding='utf-8'))
    if registry['repository'] != repository:
        raise EvidenceError('Media repository differs from registry')
    records = [{'name':p.name,'tag':tag,'url':f'https://github.com/{repository}/releases/download/{tag}/{p.name}',
                'sha256':sha256(p),'size':p.stat().st_size} for p in media]
    verify_remote(records, repository)
    current = {r['name']:r for r in registry['media_packs']}
    current.update({r['name']:r for r in records})
    registry['media_packs'] = list(current.values())
    write_json(destination, registry)
    return registry


def input_fingerprint(*, frozen=False, project=PROJECT):
    files = {}
    for directory in ('src', 'RetroWeather/src'):
        for path in (project / directory).rglob('*.py'):
            files[path.relative_to(project).as_posix()] = sha256(path)
    names = ['pyproject.toml', 'requirements.lock', 'requirements-build.lock',
             'scripts/build-portable-windows.ps1']
    if not frozen:
        names += ['README.md', 'CHANGELOG.md', 'RetroWeather/config/settings.example.json']
        for directory in ('assets', 'migrations', 'packaging', 'scripts'):
            for path in (project / directory).rglob('*'):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                    files[path.relative_to(project).as_posix()] = sha256(path)
        for path in (project / 'config').glob('*.example.toml'):
            files[path.relative_to(project).as_posix()] = sha256(path)
        names += ['docs/DISTRIBUTION.md', 'docs/THIRD-PARTY.md', 'docs/PRIVACY-AND-SERVICES.md',
                  'docs/BUNDLED-SOFTWARE-RELEASE-CHECKLIST.md']
        import tomllib
        version = tomllib.loads((project / 'pyproject.toml').read_text())['project']['version']
        names.append(f'docs/RELEASE_{version}.md')
    for name in names:
        files[name] = sha256(project / name)
    # Cover actual package bytes and native interpreter inputs, not just versions.
    native = {}
    prefix = Path(sys.base_prefix)
    for directory in (prefix, prefix / 'DLLs', prefix / 'Library/bin'):
        for path in directory.glob('*.dll'):
            native[path.relative_to(prefix).as_posix()] = sha256(path)
    native['python.exe'] = sha256(prefix / 'python.exe')
    return identity({'schema':1, 'files':files, 'python':sys.version,
                     'python_sources':python_sources(), 'native':native})


def frozen_files(output):
    if (output / 'user-data').exists():
        raise EvidenceError('A live/personal installation cannot be a frozen-build cache')
    paths = [output / 'CRTV-Sim.exe', output / 'CRTV-Sim-Safe-Stop.exe',
             *(p for p in (output / '_internal').rglob('*') if p.is_file())]
    result = {}
    for path in paths:
        if not path.resolve().is_relative_to(output.resolve()):
            raise EvidenceError('Frozen cache path escapes its build directory')
        result[path.relative_to(output).as_posix()] = sha256(path)
    return result


def record_frozen(output, fingerprint):
    if fingerprint != input_fingerprint(frozen=True):
        raise EvidenceError('Frozen build inputs changed during compilation')
    write_json(output / 'FROZEN-BUILD.json', {'schema':1, 'inputs':fingerprint, 'files':frozen_files(output)})


def check_frozen(output, fingerprint):
    receipt = json.loads((output / 'FROZEN-BUILD.json').read_text(encoding='utf-8'))
    if receipt.get('schema') != 1 or receipt['inputs'] != fingerprint or receipt['files'] != frozen_files(output):
        raise EvidenceError('Frozen cache inputs or compiled files changed')


def public_manifest(output):
    manifest = json.loads((output / 'THIRD-PARTY-MANIFEST.json').read_text(encoding='utf-8'))
    if (manifest.get('mode') != 'public candidate' or manifest.get('problems') != []
            or (output / 'INTERNAL-BUILD-NOT-FOR-DISTRIBUTION.txt').exists()):
        raise EvidenceError('Internal/incomplete packages cannot receive public release validation')
    return manifest


def read_log(path):
    data = path.read_bytes()
    return data.decode('utf-16' if data.startswith((b'\xff\xfe',b'\xfe\xff')) else 'utf-8-sig',errors='replace').replace('\r\n','\n')


def remember_frozen(output):
    check_frozen(output,input_fingerprint(frozen=True))
    write_json(PROJECT / '.build/last-frozen-build.json', {'output':str(output)})


def record_validation(output, fingerprint, version, media_log, smoke_log):
    if fingerprint != input_fingerprint():
        raise EvidenceError('Release inputs changed during packaging/testing')
    public_manifest(output)
    verify_output(output)
    checks = {}
    for name,path,markers in (
        ('media',media_log,('PASS: isolated media PATH;', 'PASS: real Weather two-minute render/publication/seeking;')),
        ('zip',smoke_log,('Frozen setup committed before scanning;',
                          'Frozen workshop scrub returned an exact frame without video encoding',
                          'Frozen channel scan, schedule creation, Guide listings/rendering, episode playback and Guide tuning passed',
                          'Legacy restart without marker, unavailable library and custom dimensions preserved; Safe Stop passed'))):
        value = read_log(path)
        if 'Traceback' in value or any(marker not in value for marker in markers):
            raise EvidenceError('Required packaged checks have not passed: ' + name)
        checks[name] = sha256(path)
    plan_path = output.parent / 'RELEASE-DOWNLOADS.json'
    record = {'schema':1, 'version':version, 'inputs':fingerprint,
              'media_checks':True, 'zip_smoke_checks':True, 'checks':checks, 'plan_sha256':sha256(plan_path)}
    write_json(output.parent / 'RELEASE-VALIDATION.json', record)
    remember_frozen(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('register', 'register-media', 'inspect', 'fingerprint', 'record-frozen',
                                         'check-frozen', 'record-validation', 'find-frozen', 'remember-frozen'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--tag')
    parser.add_argument('--repository', default='pinesal/CRTV-Sim')
    parser.add_argument('--media', type=Path, nargs='*', default=[])
    parser.add_argument('--fingerprint')
    parser.add_argument('--frozen', action='store_true')
    parser.add_argument('--version')
    parser.add_argument('--media-log', type=Path)
    parser.add_argument('--smoke-log', type=Path)
    args = parser.parse_args()
    if args.action == 'fingerprint':
        print(input_fingerprint(frozen=args.frozen));sys.exit(0)
    if args.action == 'register-media':
        if not args.tag or not args.media:
            parser.error('register-media requires --tag and --media')
        print(json.dumps(register_media(args.tag, args.repository, args.media),indent=2));sys.exit(0)
    if args.action == 'find-frozen':
        try:
            cache = json.loads((PROJECT / '.build/last-frozen-build.json').read_text())
            candidate = Path(cache['output'])
            check_frozen(candidate, args.fingerprint)
            print(candidate)
        except (OSError, ValueError, KeyError, EvidenceError):
            pass
        sys.exit(0)
    if not args.output:
        parser.error('This action requires --output')
    if args.action == 'check-frozen':
        try:
            check_frozen(args.output.resolve(), args.fingerprint)
        except (OSError, ValueError, KeyError, EvidenceError) as exc:
            print('Cannot reuse frozen build: ' + str(exc), file=sys.stderr);sys.exit(2)
        sys.exit(0)
    if args.action == 'record-frozen':
        record_frozen(args.output.resolve(), args.fingerprint);sys.exit(0)
    if args.action == 'remember-frozen':
        remember_frozen(args.output.resolve());sys.exit(0)
    if args.action == 'record-validation':
        if not args.media_log or not args.smoke_log:
            parser.error('record-validation requires --media-log and --smoke-log')
        record_validation(args.output.resolve(), args.fingerprint, args.version,args.media_log,args.smoke_log);sys.exit(0)
    if args.action == 'register':
        if not args.tag:
            parser.error('register requires --tag')
        result = register(args.output.resolve(), args.tag, args.repository, args.media)
    else:
        result = source_reference(args.output.resolve())
    print(json.dumps(result, indent=2))
