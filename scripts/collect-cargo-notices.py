"""Recover omitted crate notices from the commit recorded in each crate.

Never treats the license of a compiler or an unrelated recipe as the license
of a library. No missing notice is replaced with invented attribution.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import ssl
import io
import tarfile
import urllib.error
import urllib.request

import truststore

WORK = Path(__file__).resolve().parents[1] / '.build/third-party-research/rust'


def collect(pair):
    key, p = pair
    directory = WORK / 'supplemental-notices' / key
    directory.mkdir(parents=True, exist_ok=True)
    repository = p['repository'].replace('http://', 'https://').removesuffix('.git').rstrip('/')
    if not repository.startswith('https://github.com/'):
        raise RuntimeError('Unexpected repository: ' + repository)
    slug = '/'.join(repository.removeprefix('https://github.com/').split('/')[:2])
    commit = p['vcs']['git']['sha1'] if p.get('vcs') else None
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    def get(url):
        request = urllib.request.Request(url, headers={'User-Agent': 'CRTV-Sim-source-audit'})
        try:
            return urllib.request.urlopen(request, context=ctx, timeout=45).read()
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 422):
                return None
            raise
    if not commit:
        # winapi's target package is released from the parent winapi 0.3.9 tree.
        # Its actual source headers explicitly name the same dual license.
        tags = ['0.3.9'] if p['name'].startswith('winapi-') else ['v'+p['version'],p['version']]
        if p['name'] == 'difflib' and p['version'] == '0.4.0':
            # This commit's Cargo.toml and all src/*.rs were compared with the
            # published crate; the crate's include rule omitted LICENSE.
            tags = ['f035fb8e656f27119e23eca9d5b996df14f3885e']
        for tag in tags:
            data = get('https://api.github.com/repos/'+slug+'/commits/'+tag)
            if data:
                commit = json.loads(data)['sha']; break
        if not commit:
            raise RuntimeError('No recorded commit or matching release tag: ' + key)
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise RuntimeError('Invalid commit: ' + key)
    records = {}
    for filename in ['LICENSE', 'LICENSE-MIT', 'LICENSE-APACHE', 'LICENSE.md', 'LICENSE.txt',
                     'LICENSE-MIT.txt', 'LICENSE-APACHE.txt', 'COPYING', 'LICENSE-2.0.txt']:
        url = 'https://raw.githubusercontent.com/'+slug+'/'+commit+'/'+filename
        data = get(url)
        if data and (b'License' in data or b'LICENSE' in data or b'licens' in data.lower()):
            (directory/filename).write_bytes(data)
            records[filename] = {'url': url, 'sha256': hashlib.sha256(data).hexdigest()}
    if not records:
        url = 'https://codeload.github.com/'+slug+'/tar.gz/'+commit
        snapshot = get(url)
        if snapshot:
            with tarfile.open(fileobj=io.BytesIO(snapshot), mode='r:gz') as archive:
                for member in archive.getmembers():
                    if member.isfile() and (Path(member.name).name.lower().startswith(('license','licence','copying','copyright')) or '/licenses/' in member.name.lower()):
                        data = archive.extractfile(member).read()
                        filename = member.name.split('/',1)[1].replace('/','__')
                        (directory/filename).write_bytes(data)
                        records[filename] = {'url':url,'member':member.name,'snapshot_sha256':hashlib.sha256(snapshot).hexdigest(),'sha256':hashlib.sha256(data).hexdigest()}
    basis = 'upstream notice from recorded commit'
    if not records:
        # A valid SPDX identifier in the publisher's Cargo.toml is itself an
        # explicit license declaration. Some projects never supplied a notice
        # file. Supply the standard terms, preserving all actual copyright and
        # author information in the original source; do not invent a copyright
        # holder or pretend this text came from the upstream source snapshot.
        selected = 'Apache-2.0' if 'Apache-2.0' in p['license'] else 'MIT' if p['license'] == 'MIT' else None
        if not selected:
            raise RuntimeError('No standard terms for explicit declaration: ' + key)
        url = 'https://raw.githubusercontent.com/spdx/license-list-data/v3.27.0/text/'+selected+'.txt'
        data = get(url)
        if not data:
            raise RuntimeError('Standard SPDX license unavailable: ' + selected)
        filename = 'STANDARD-'+selected+'.txt'
        (directory/filename).write_bytes(data)
        records[filename] = {'url':url,'sha256':hashlib.sha256(data).hexdigest()}
        basis = 'publisher Cargo.toml SPDX declaration; canonical SPDX terms, not an upstream notice file'
    proof = {'crate': p['name'], 'version': p['version'], 'declared_license': p['license'],
             'commit': commit, 'commit_basis': 'crate .cargo_vcs_info.json' if p.get('vcs') else 'upstream release tag',
             'notice_basis': basis, 'files': records}
    (directory/'provenance.json').write_text(json.dumps(proof,indent=2)+'\n',encoding='utf-8')
    print('Recovered notices',key,flush=True)


if __name__ == '__main__':
    data=json.loads((WORK/'missing-notices.json').read_text(encoding='utf-8'))
    errors = []
    def run(pair):
        try:
            collect(pair)
        except Exception as exc:
            errors.append(pair[0] + ': ' + str(exc))
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(run,data.items()))
    if errors:
        raise RuntimeError('\n'.join(errors))
