"""Recover actual upstream notices absent from supplier binary packages."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

from windows_toolchain import LOCK, fields, safe_relative, tar_bytes, tar_names, write_json


def notice(name):
    return Path(name).name.lower().startswith(('license', 'licence', 'copying', 'copyright', 'notice', 'authors', 'patents'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache',type=Path,required=True)
    args=parser.parse_args()
    lock=json.loads(LOCK.read_text(encoding='utf-8'));records={}
    for p in lock['packages']:
        if p['notices']:
            continue
        source=args.cache/p['source']['filename'];members=tar_names(source);base=p['base']
        out=args.cache/'upstream-notices'/p['name'];out.mkdir(parents=True,exist_ok=True)
        found={}
        def save(name,data,basis):
            safe_relative(name)
            path=out/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
            found[name]={'sha256':hashlib.sha256(data).hexdigest(),'source_sha256':p['source']['sha256'],'source_member':basis}
        for name in members:
            if name.endswith(('.tar.gz','.tar.xz','.tar.bz2','.tgz')):
                payload=tar_bytes(source,name)
                with tarfile.open(fileobj=io.BytesIO(payload),mode='r:*') as archive:
                    for member in archive.getmembers():
                        if member.isfile() and notice(member.name):
                            save(member.name,archive.extractfile(member).read(),name+'!/'+member.name)
        src=fields(tar_bytes(source,base+'/.SRCINFO').decode())
        for item in src.get('source',[]):
            url=item.split('::')[-1]
            if not url.startswith('git+'):
                continue
            git_name=item.split('::')[0] if '::' in item else url.split('#')[0].rsplit('/',1)[-1].removesuffix('.git')
            root=base+'/'+git_name;reference=url.partition('#')[2].partition('=')[2]
            with tempfile.TemporaryDirectory(dir=args.cache) as temp:
                subprocess.run(['tar.exe','-xf',str(source.resolve()),'-C',temp,root+'/'],check=True)
                git=['git','--git-dir='+str(Path(temp)/root)]
                paths=subprocess.check_output([*git,'ls-tree','-r','--name-only',reference]).decode().splitlines()
                for path in paths:
                    if notice(path):
                        save(path,subprocess.check_output([*git,'show',reference+':'+path]),root+'@'+reference+':'+path)
        if not found:
            raise RuntimeError('No recovered upstream notices: '+p['name'])
        records[p['name']]=found
        print('Recovered',p['name'],len(found),flush=True)
    write_json(args.cache/'upstream-notices.json',records)


if __name__=='__main__':main()
