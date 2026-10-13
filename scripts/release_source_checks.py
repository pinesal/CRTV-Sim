"""Validation for recovered inputs beyond the Windows PE import graph."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tomllib
import zipfile


def verify_cargo(record, package, source, cache):
    from windows_toolchain import EvidenceError, check_hash, safe_relative, tar_bytes
    path = cache / safe_relative(record['filename'])
    check_hash(path, record['sha256'])
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            safe_relative(name.rstrip('/'))
        upstream = z.read('Cargo.lock.upstream')
        effective = z.read('Cargo.lock')
        if hashlib.sha256(upstream).hexdigest() != record['upstream_lock_sha256'] or hashlib.sha256(effective).hexdigest() != record['effective_lock_sha256']:
            raise EvidenceError('Cargo lock changed: ' + record['package'])
        # Recover the original lock from the exact supplier source, rather than
        # trusting a separately downloaded lock or a successful vendor command.
        filename = next(n for n in __import__('windows_toolchain').tar_names(source)
                        if n.endswith(('.tar.gz','.tar.xz')))
        with tarfile.open(fileobj=io.BytesIO(tar_bytes(source, filename)), mode='r:*') as t:
            original = t.extractfile(record['lock_member']).read()
        if original != upstream:
            raise EvidenceError('Cargo lock does not belong to the binary source: ' + record['package'])
        before = tomllib.loads(upstream.decode())['package']
        after = tomllib.loads(effective.decode())['package']
        if record['transformation'] is None:
            if upstream != effective:
                raise EvidenceError('Unexpected Cargo resolution change')
        elif record['transformation'] == 'cargo update -p windows-sys@0.61.2 --precise 0.60.2' and package['name'].endswith('-librsvg'):
            # No unconstrained update of other libraries is accepted. The only
            # added packages are the target imports of windows-sys 0.60.2.
            unchanged = lambda rows: sorted((p for p in rows if not p['name'].startswith('windows')), key=lambda p:(p['name'],p['version']))
            if unchanged(before) != unchanged(after):
                raise EvidenceError('librsvg update changed unrelated crates')
            if any(p['name']=='windows-sys' and p['version']=='0.61.2' for p in after):
                raise EvidenceError('librsvg recipe adjustment was not applied')
        else:
            raise EvidenceError('Unreviewed Cargo lock transformation')
        expected = {p['name']+'-'+p['version']:p for p in after if p.get('source')}
        manifests = json.loads(z.read('CRATE-MANIFEST.json'))
        if manifests != record['crates'] or set(manifests) != set(expected):
            raise EvidenceError('Vendored crate set differs from exact lock')
        for key, p in expected.items():
            if p['source'] != 'registry+https://github.com/rust-lang/crates.io-index':
                raise EvidenceError('Unverified git crate: '+key)
            item = manifests[key]
            raw = z.read('crates/'+key+'.crate')
            if hashlib.sha256(raw).hexdigest() != p['checksum']:
                raise EvidenceError('Original crate archive differs from lock: '+key)
            root = 'vendor/'+safe_relative(item['folder'])+'/'
            checks = json.loads(z.read(root+'.cargo-checksum.json'))
            if checks['package'] != p['checksum']:
                raise EvidenceError('Vendor package checksum differs from lock: '+key)
            for member, digest in checks['files'].items():
                if hashlib.sha256(z.read(root+safe_relative(member))).hexdigest() != digest:
                    raise EvidenceError('Vendor source corruption: '+key+'/'+member)
            if not item['notices'] and not item['supplemental']:
                raise EvidenceError('Missing crate license: '+key)
            for name in item['notices']:
                z.read(root+safe_relative(name))
            for name, proof in (item.get('supplemental') or {}).get('files',{}).items():
                content=z.read('supplemental-notices/'+key+'/'+safe_relative(name))
                if hashlib.sha256(content).hexdigest()!=proof['sha256']:
                    raise EvidenceError('Changed supplemental crate notice: '+key)


def verify_inputs(lock, cache, packages):
    from windows_toolchain import EvidenceError, check_hash, fetch, fields, source_check, tar_bytes
    proof=lock.get('source_inputs')
    if not proof:
        return ['Exact static/header, Cargo and upstream-notice source records are missing']
    problems=[]
    import importlib.util
    specification = importlib.util.spec_from_file_location('incorporated_sources', Path(__file__).with_name('collect-incorporated-sources.py'))
    collector = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(collector)
    required, _ = collector.required_inputs(lock, cache)
    supplied = {p['binary']['filename']: p for p in proof['packages']}
    for filename, consumers in required.items():
        if filename not in supplied:
            problems.append('Missing exact incorporated source: ' + filename)
        elif not {c['consumer'] for c in consumers} <= {c['consumer'] for c in supplied[filename]['consumers']}:
            problems.append('Missing incorporated-source consumer mapping: ' + filename)
    for p in proof['packages']:
        binary,source=fetch(p['binary'],cache),fetch(p['source'],cache)
        problems.extend(source_check(p,source,binary))
        for consumer in p['consumers']:
            if consumer['consumer'] not in packages:
                raise EvidenceError('Unknown incorporated-source consumer')
            build=fields(tar_bytes(packages[consumer['consumer']][1],'.BUILDINFO').decode())
            installed=p['binary']['filename'].removesuffix('.pkg.tar.zst')
            if installed not in build['installed']:
                raise EvidenceError('Static/header source version differs from build: '+p['name'])
    cargo={r['package']:r for r in proof['cargo']}
    for name,(p,binary,source) in packages.items():
        recipe=tar_bytes(source,p['base']+'/PKGBUILD').decode()
        import re
        if re.search(r'cargo\s+(fetch|update|build|cbuild|capi)',recipe):
            if name not in cargo:
                problems.append(name+': matching locked Cargo vendor source is missing')
            else:
                verify_cargo(cargo[name],p,source,cache)
        notices=proof['upstream_notices'].get(name,{})
        if not p['notices'] and not notices:
            problems.append(name+': upstream binary-distribution notices are missing')
        for member,record in notices.items():
            check_hash(cache/'upstream-notices'/name/member,record['sha256'])
            if record['source_sha256']!=p['source']['sha256']:
                raise EvidenceError('Notice is not tied to the exact source: '+name)
    return problems


def stage_inputs(lock, cache, output):
    from windows_toolchain import check_hash, safe_relative, tar_bytes, write_json
    proof=lock['source_inputs']
    for name,records in proof['upstream_notices'].items():
        for member,record in records.items():
            source=cache/'upstream-notices'/name/safe_relative(member)
            check_hash(source,record['sha256'])
            destination=output/'licenses/upstream'/name/member
            destination.parent.mkdir(parents=True,exist_ok=True)
            destination.write_bytes(source.read_bytes())
    for p in proof['packages']:
        for name in p.get('notices',[]):
            target=output/'licenses/incorporated'/p['name']/p['version']/safe_relative(name)
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(tar_bytes(cache/p['binary']['filename'],name))
    for record in proof['cargo']:
        with zipfile.ZipFile(cache/record['filename']) as z:
            for key,p in record['crates'].items():
                root='vendor/'+p['folder']+'/'
                for member in p['notices']:
                    target=output/'licenses/rust'/key/safe_relative(member)
                    target.parent.mkdir(parents=True,exist_ok=True)
                    target.write_bytes(z.read(root+member))
                for member in (p.get('supplemental') or {}).get('files',{}):
                    target=output/'licenses/rust'/key/safe_relative(member)
                    target.parent.mkdir(parents=True,exist_ok=True)
                    target.write_bytes(z.read('supplemental-notices/'+key+'/'+member))
        write_json(output/'licenses/rust'/record['package']/'CRATE-MANIFEST.json',record['crates'])
