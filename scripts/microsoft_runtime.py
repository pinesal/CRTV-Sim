"""App-local Microsoft runtime redistribution with explicit licensed origin.

The Conda recipe's BSD license is not a redistribution grant for Microsoft
DLLs. Require a licensed Visual Studio product and use an unmodified retail
runtime from Microsoft's signed redistributable, never debug_nonredist.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import ssl
import urllib.request

from windows_toolchain import PROJECT, EvidenceError, check_hash, sha256, write_json


def licensed_visual_studio():
    root=Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)'))/'Microsoft Visual Studio/Installer/vswhere.exe'
    if not root.is_file():
        return []
    result=subprocess.run([str(root),'-products','Microsoft.VisualStudio.Product.Community',
                           'Microsoft.VisualStudio.Product.Professional','Microsoft.VisualStudio.Product.Enterprise',
                           '-format','json','-utf8'],capture_output=True,check=True)
    return [p for p in json.loads(result.stdout.decode('utf-8-sig'))
            if p.get('isComplete') and p.get('isLaunchable')
            and p.get('installationVersion', '').startswith('18.') and not p.get('isPrerelease')]


def retail_runtime(record):
    """Extract pinned Microsoft cabinets as data; never run its installer."""
    cache = PROJECT/'.build/third-party-research/microsoft'
    cache.mkdir(parents=True, exist_ok=True)
    bundle = cache/'vc_redist.x64.exe'
    if not bundle.is_file():
        import truststore
        with urllib.request.urlopen(record['bundle']['url'], context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), timeout=120) as response:
            bundle.write_bytes(response.read())
    check_hash(bundle, record['bundle']['sha256'])
    data = bundle.read_bytes()
    outer = record['bundle']['cabinet']
    cab = cache/'pinned-runtime.cab'
    cab.write_bytes(data[outer['offset']:outer['offset']+outer['size']])
    check_hash(cab, outer['sha256'])
    expand = Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/expand.exe'
    unpack = cache/'pinned-payload'
    unpack.mkdir(exist_ok=True)
    subprocess.run([str(expand), '-F:'+outer['member'], str(cab), str(unpack)], check=True, capture_output=True)
    payload = unpack/outer['member']
    check_hash(payload, outer['member_sha256'])
    target = cache/'retail-runtime'
    target.mkdir(exist_ok=True)
    subprocess.run([str(expand), '-F:vcruntime140.dll_amd64', str(payload), str(target)], check=True, capture_output=True)
    runtime = target/'vcruntime140.dll'
    runtime.write_bytes((target/'vcruntime140.dll_amd64').read_bytes())
    check_hash(runtime, record['sha256'])
    return runtime


def stage(output, *, internal=False):
    record=json.loads((PROJECT/'packaging/microsoft-runtime.lock.json').read_text(encoding='utf-8'))
    installations=licensed_visual_studio()
    if not installations and not internal:
        raise EvidenceError('This runtime grant requires a valid Visual Studio Community/Professional/Enterprise 2026 license. Install the chosen free Community edition and accept its terms; the existing 2022 Build Tools alone are insufficient.')
    source=retail_runtime(record)
    # Check the OS's Authenticode verification in addition to pinned byte hash.
    shell=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    for signed in (source, PROJECT/'.build/third-party-research/microsoft/vc_redist.x64.exe'):
        command="(Get-AuthenticodeSignature -LiteralPath '"+str(signed).replace("'","''")+"').Status.ToString()"
        # A PowerShell 7 caller's module path can shadow the Windows PowerShell
        # security module with an incompatible edition. Use the OS's own module.
        env = {**os.environ, 'PSModulePath': str(shell.parent/'Modules')}
        result=subprocess.check_output([str(shell),'-NoProfile','-Command',command],text=True,env=env).strip()
        if result!='Valid':
            raise EvidenceError('Microsoft retail signature did not validate: '+signed.name+': '+result)
    target=output/'_internal/VCRUNTIME140.dll'
    if not target.is_file():
        raise EvidenceError('Expected frozen VCRUNTIME140.dll is missing')
    shutil.copy2(source,target)
    check_hash(target,record['sha256'])
    for filename,item in record['notices'].items():
        path=PROJECT/'assets/public-notices'/filename
        check_hash(path,item['sha256'])
        dest=output/'licenses/microsoft'/filename
        dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,dest)
    write_json(output/'MICROSOFT-RUNTIME.json',{
        'version':record['version'],'sha256':record['sha256'],
        'license_grant':record['license_grant'], 'notices':record['notices'],
        'bundle':record['bundle'],
        'licensed_build_version':installations[0]['installationVersion'] if installations else None,
        'licensed_build_product':installations[0]['productId'] if installations else None,
        'problems':[] if installations else ['Microsoft runtime: licensed Visual Studio product is not installed; Community license acceptance remains required']})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--internal',action='store_true')
    args=parser.parse_args()
    if not args.output.resolve().is_relative_to(PROJECT/'dist'):
        raise EvidenceError('Runtime staging must stay inside dist')
    stage(args.output,internal=args.internal)
