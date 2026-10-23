"""Publish a validated download plan; unchanged assets remain at their existing URLs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote
from urllib.request import Request, urlopen

from release_reuse import PROJECT, input_fingerprint, verify_remote, public_manifest, read_log
from windows_toolchain import EvidenceError, sha256


def checked_plan(folder):
    plan_path = folder / 'RELEASE-DOWNLOADS.json'
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    proof = json.loads((folder / 'RELEASE-VALIDATION.json').read_text(encoding='utf-8'))
    if Path(plan['output']).name != plan['output']:
        raise EvidenceError('Invalid candidate directory')
    public_manifest(folder / plan['output'])
    if (plan.get('schema') != 1 or proof.get('schema') != 1 or proof['version'] != plan['version']
            or proof['plan_sha256'] != sha256(plan_path) or proof['inputs'] != input_fingerprint()
            or proof.get('media_checks') is not True or proof.get('zip_smoke_checks') is not True
            or set(proof.get('checks',{})) != {'media','zip'}
            or any(not re.fullmatch('[0-9a-f]{64}',v) for v in proof.get('checks',{}).values())):
        raise EvidenceError('Release inputs/plan changed or required packaged checks have not passed')
    names = [a['name'] for a in plan['upload'] + plan['reuse']]
    if len(names) != len(set(names)):
        raise EvidenceError('Duplicate release download name')
    for asset in plan['upload']:
        path = folder / asset['name']
        if (path.parent != folder or path.name != asset['name'] or not path.resolve().is_relative_to(folder.resolve())
                or sha256(path) != asset['sha256'] or path.stat().st_size != asset['size']):
            raise EvidenceError('Release download is missing, modified or outside its directory: ' + asset['name'])
    verify_remote(plan['reuse'], plan['repository'])
    return plan


def release_body(plan, body):
    # The plan is authoritative: avoid old release-note links promising copies
    # of source/media downloads that are intentionally not being uploaded again.
    body = body.split('\n## Downloads\n')[0]
    repository, tag = plan['repository'], 'v' + plan['version']
    body += '\n## Downloads\n\nOnly the Windows ZIP is needed to run CRTV Sim. '
    body += 'Sources, build materials, checksums and media packs are optional downloads.\n\n'
    for asset in plan['upload'] + plan['reuse']:
        url = asset.get('url') or f'https://github.com/{repository}/releases/download/{tag}/{asset["name"]}'
        body += f'- [{asset["name"]}]({url})' + (' — reused unchanged download' if asset.get('url') else '') + '\n'
    body += f'\n[Changelog](https://github.com/{repository}/blob/{tag}/CHANGELOG.md) · '
    body += f'[Updating an installation](https://github.com/{repository}/blob/{tag}/docs/DISTRIBUTION.md)\n'
    body = body.replace('(../CHANGELOG.md)',f'(https://github.com/{repository}/blob/{tag}/CHANGELOG.md)')
    body = body.replace('(GETTING_STARTED.md)',f'(https://github.com/{repository}/blob/{tag}/docs/GETTING_STARTED.md)')
    return body


class GitHub:
    def __init__(self, repository):
        self.base = 'https://api.github.com/repos/' + repository
        result = subprocess.run(['git','credential','fill'], input='protocol=https\nhost=github.com\n\n',
                                text=True,capture_output=True,check=True,
                                env={**os.environ,'GIT_TERMINAL_PROMPT':'0','GCM_INTERACTIVE':'never'})
        token = dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)['password']
        self.headers = {'Authorization':'Bearer ' + token,'Accept':'application/vnd.github+json',
                        'User-Agent':'CRTV-Sim-release','X-GitHub-Api-Version':'2022-11-28'}

    def api(self, path='', method='GET', value=None):
        data = json.dumps(value).encode() if value is not None else None
        with urlopen(Request(self.base + path,data=data,headers={**self.headers,'Content-Type':'application/json'},
                             method=method),timeout=90) as response:
            raw = response.read()
        return json.loads(raw) if raw else None

    def upload(self, release, path):
        with path.open('rb') as stream:
            request = Request(release['upload_url'].split('{')[0] + '?name=' + quote(path.name),data=stream,
                              headers={**self.headers,'Content-Type':'application/octet-stream',
                                       'Content-Length':str(path.stat().st_size)},method='POST')
            with urlopen(request,timeout=180) as response:
                return json.load(response)


def publish(folder, plan, body, github):
    # Caller must push committed/tagged sources; uploading never compiles them.
    if subprocess.run(['git','diff','--quiet','HEAD'],cwd=PROJECT).returncode:
        raise EvidenceError('Commit release changes before publishing')
    tag = 'v' + plan['version']
    head = subprocess.check_output(['git','rev-parse','HEAD'],cwd=PROJECT,text=True).strip()
    if subprocess.check_output(['git','rev-parse',tag + '^{commit}'],cwd=PROJECT,text=True).strip() != head:
        raise EvidenceError('Tag the committed release source before publishing')
    ref = github.api('/git/ref/tags/' + tag)['object']
    if ref['type'] == 'tag':
        ref = github.api('/git/tags/' + ref['sha'])['object']
    if ref['sha'] != head:
        raise EvidenceError('Push the matching release tag before publishing')
    visibility = github.api()['visibility']
    release = next((r for r in github.api('/releases?per_page=100') if r['tag_name'] == tag),None)
    if release is None:
        release = github.api('/releases','POST',{'tag_name':tag,'target_commitish':head,
                              'name':f'CRTV Sim {plan["version"]} — Public Beta',
                              'body':body,'draft':True,'prerelease':True})
    existing = {a['name']:a for a in github.api(f'/releases/{release["id"]}/assets?per_page=100')}
    for asset in plan['upload']:
        item = existing.get(asset['name'])
        if not item:
            if not release['draft']:
                raise EvidenceError('Published release differs; use a new version instead of replacing it')
            print('Uploading:',asset['name'],flush=True)
            item = github.upload(release,folder / asset['name'])
        if item.get('digest') != 'sha256:' + asset['sha256'] or item['size'] != asset['size']:
            raise EvidenceError('Uploaded asset identity differs: ' + asset['name'])
    # Recheck both local and linked downloads before making the draft public.
    checked_plan(folder)
    actual = {a['name']:a for a in github.api(f'/releases/{release["id"]}/assets?per_page=100')}
    if set(actual) != {a['name'] for a in plan['upload']}:
        raise EvidenceError('Release contains unexpected or missing assets')
    for asset in plan['upload']:
        item = actual[asset['name']]
        if item.get('digest') != 'sha256:' + asset['sha256'] or item['size'] != asset['size']:
            raise EvidenceError('Release asset changed before publication: ' + asset['name'])
    for asset in plan['reuse']:
        print('Reusing:',asset['url'],flush=True)
    if release['draft']:
        release = github.api('/releases/' + str(release['id']),'PATCH',{'body':body,'draft':False,'prerelease':True})
    if github.api()['visibility'] != visibility:
        raise EvidenceError('Repository visibility changed during publication')
    print('Release ready:',release['html_url'],flush=True)
    return release


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('--publish', action='store_true', help='Explicitly upload and publish; default is a read-only plan')
    parser.add_argument('--test-log', type=Path, action='append', default=[])
    args = parser.parse_args()
    folder = args.folder.resolve()
    plan = checked_plan(folder)
    body = release_body(plan,(PROJECT / f'docs/RELEASE_{plan["version"]}.md').read_text(encoding='utf-8'))
    if args.publish:
        if len(args.test_log) < 2:
            parser.error('--publish requires passed application/Weather regression logs via --test-log')
        for path in args.test_log:
            value = read_log(path)
            if not re.search(r'^OK(?: \(skipped=\d+\))?\s*$',value,re.M) or re.search(r'^FAILED',value,re.M):
                raise EvidenceError('Regression log has not passed: ' + str(path))
        publish(folder,plan,body,GitHub(plan['repository']))
    else:
        print(json.dumps(plan,indent=2))
        print('\nRelease notes preview:\n' + body)
