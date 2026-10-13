"""Seal previously completed cargo vendor --locked output and upstream locks.

Crate code is verified against Cargo's per-file checksums and the registry
archive digest in the lock. Supplemental notices must be fetched separately
from a pinned upstream commit when the published crate omits its license.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tomllib
import zipfile

from windows_toolchain import LOCK, EvidenceError, check_hash, sha256, write_json

PROJECTS = {
    'rav1e': ('rav1e/rav1e-0.8.1', 'Cargo.lock'),
    'libdovi': ('libdovi/dovi_tool-libdovi-3.4.0/dolby_vision', 'Cargo.lock'),
    'librsvg': ('librsvg/librsvg-2.63.2', 'Cargo.lock.upstream'),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--cargo-home', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    args = parser.parse_args()
    records = []
    for name, (directory, original) in PROJECTS.items():
        root = args.work / directory
        vendor = args.work / (name + '-vendor')
        effective = tomllib.loads((root / 'Cargo.lock').read_text(encoding='utf-8'))
        expected = {(p['name'], p['version']): p for p in effective['package'] if p.get('source')}
        crates, members = {}, {}
        for folder in sorted(vendor.iterdir()):
            if not folder.is_dir() or not (folder / 'Cargo.toml').is_file():
                raise EvidenceError('Incomplete vendor directory: ' + str(folder))
            metadata = tomllib.loads((folder / 'Cargo.toml').read_text(encoding='utf-8'))['package']
            identity = (metadata['name'], metadata['version'])
            p = expected[identity]
            if p['source'] != 'registry+https://github.com/rust-lang/crates.io-index':
                raise EvidenceError('Unaccounted git crate: ' + str(identity))
            checks = json.loads((folder / '.cargo-checksum.json').read_text(encoding='utf-8'))
            if checks['package'] != p['checksum']:
                raise EvidenceError('Vendor differs from original lock: ' + str(identity))
            archives = list((args.cargo_home / 'registry/cache').glob('*/' + p['name'] + '-' + p['version'] + '.crate'))
            if len(archives) != 1:
                raise EvidenceError('Missing original registry archive: ' + str(identity))
            check_hash(archives[0], p['checksum'])
            for relative, digest in checks['files'].items():
                check_hash(folder / relative, digest)
            actual = {f.relative_to(folder).as_posix() for f in folder.rglob('*') if f.is_file()}
            if actual != set(checks['files']) | {'.cargo-checksum.json'}:
                raise EvidenceError('Unaccounted vendor files: ' + str(identity))
            notices = [f.relative_to(folder).as_posix() for f in folder.rglob('*') if f.is_file()
                       and f.name.lower().startswith(('license', 'licence', 'copying', 'copyright', 'notice'))]
            supplemental = args.work / 'supplemental-notices' / (p['name'] + '-' + p['version'])
            proof = None
            if supplemental.is_dir():
                proof = json.loads((supplemental / 'provenance.json').read_text(encoding='utf-8'))
                for filename, record in proof['files'].items():
                    check_hash(supplemental / filename, record['sha256'])
                    members['supplemental-notices/' + supplemental.name + '/' + filename] = supplemental / filename
                members['supplemental-notices/' + supplemental.name + '/provenance.json'] = supplemental / 'provenance.json'
            if not notices and not proof:
                raise EvidenceError('Missing crate license text: ' + str(identity))
            key = p['name'] + '-' + p['version']
            crates[key] = {'name': p['name'], 'version': p['version'], 'checksum': p['checksum'],
                           'folder': folder.name, 'license': metadata.get('license'),
                           'repository': metadata.get('repository'), 'notices': notices,
                           'supplemental': proof}
            members['crates/' + archives[0].name] = archives[0]
            for file in folder.rglob('*'):
                if file.is_file():
                    members['vendor/' + folder.name + '/' + file.relative_to(folder).as_posix()] = file
        if set((p['name'], p['version']) for p in crates.values()) != set(expected):
            raise EvidenceError('Vendor does not cover the complete lock: ' + name)
        target = args.cache / (name + '-locked-vendor.zip')
        config = '[source.crates-io]\nreplace-with = "vendored-sources"\n[source.vendored-sources]\ndirectory = "vendor"\n'
        with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=6, strict_timestamps=False) as z:
            for relative, file in sorted(members.items()):
                z.write(file, relative)
            z.write(root / original, 'Cargo.lock.upstream')
            z.write(root / 'Cargo.lock', 'Cargo.lock')
            z.writestr('config.toml', config)
            z.writestr('CRATE-MANIFEST.json', json.dumps(crates, indent=2))
        records.append({'package': 'mingw-w64-ucrt-x86_64-' + name,
                        'filename': target.name, 'sha256': sha256(target),
                        'upstream_lock_sha256': sha256(root / original),
                        'effective_lock_sha256': sha256(root / 'Cargo.lock'),
                        'lock_member': directory.removeprefix(name + '/') + '/' + original.replace('.upstream', ''),
                        'transformation': 'cargo update -p windows-sys@0.61.2 --precise 0.60.2' if name == 'librsvg' else None,
                        'crates': crates})
        print(name, len(crates), target.stat().st_size, flush=True)
    write_json(args.cache / 'cargo-sources.json', records)


if __name__ == '__main__':
    main()
