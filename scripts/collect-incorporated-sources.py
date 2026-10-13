"""Recover exact recipe inputs which PE imports cannot describe.

This collects source packages, not installs or replacement DLLs. Ordinary
compilers, documentation generators and build drivers are recorded separately;
their executables are not part of the shipped work. The source set deliberately
includes a conservative superset of libraries which may be statically linked.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
from pathlib import Path
import re

from windows_toolchain import LOCK, fields, tar_bytes, sha256, write_json

PREFIX = 'mingw-w64-ucrt-x86_64-'
# These provide compiled headers, static objects, shader interfaces or embedded
# generated code. Unlike cmake/nasm/pkgconf, their output incorporates material
# beyond merely driving compilation. MinGW CRT/headers also retain the licensing
# distinctions inside their source tree (public domain/MIT/BSD exceptions).
INCORPORATED = {'dlfcn',
                'fast_float', 'xxhash', 'boost', 'ladspa-sdk', 'lv2',
                'vulkan-headers', 'amf-headers', 'ffnvcodec-headers',
                'spirv-headers', 'spirv-tools', 'glslang', 'directx-headers',
                'libclc', 'libelf', 'polly', 'opencl-headers', 'python-glad',
                'winpthreads'}
SYSTEM_SUPPORT = {'crt', 'crt-git', 'headers', 'headers-git', 'windows-default-manifest'}
TOOLS = {'cc', 'gcc', 'gcc-ada', 'clang', 'llvm', 'llvm-tools', 'binutils',
         'cmake', 'meson', 'ninja', 'pkgconf', 'autotools', 'libtool', 'nasm',
         'rust', 'cargo-c', 'cython', 'gperf', 'git', 'perl', 'python', 'ragel',
         'python-jinja', 'python-mako', 'python-yaml', 'python-packaging',
         'python-setuptools', 'python-docutils', 'python-rst2pdf', 'python-sphinx',
         'python-fonttools', 'asciidoctor', 'asciidoc', 'texinfo', 'doxygen',
         'docbook-xsl', 'tools', 'gobject-introspection', 'vala', 'gi-docgen',
         'gtk-doc', 'gettext-tools', 'cppunit', 'mpg123', 'ncurses', 'libxslt',
         'ffmpeg', 'gdk-pixbuf2', 'icu', 'cairo', 'chafa', 'libpng', 'libsndfile',
         'openal', 'portaudio', 'SDL2', 'SDL', 'fluidsynth', 'libffi', 'z3',
         'gmp', 'isl', 'libiconv', 'mpc', 'mpfr', 'zlib', 'zstd', 'readline',
         'l-smash', 'ffms2'}


def required_inputs(lock, cache):
    """Derive required inputs from supplier records, not asserted lock coverage."""
    shipped = {p['name'] for p in lock['packages']}
    wanted, excluded = {}, []
    for p in lock['packages']:
        binary = cache / p['binary']['filename']
        source = cache / p['source']['filename']
        build = fields(tar_bytes(binary, '.BUILDINFO').decode())
        info = fields(tar_bytes(binary, '.PKGINFO').decode())
        srcinfo = fields(tar_bytes(source, p['base'] + '/.SRCINFO').decode())
        installed = build['installed']
        declarations = {re.split('[<>=]', n)[0]: 'declared runtime library (conservative static-source coverage)'
                        for n in info.get('depend', []) if n.startswith(PREFIX)}
        for n in srcinfo.get('makedepends', []):
            if not n.startswith(PREFIX):
                continue
            short = re.split('[<>=]', n)[0].removeprefix(PREFIX)
            if short in INCORPORATED:
                declarations[PREFIX + short] = 'incorporated static/header/generated input'
            elif short in SYSTEM_SUPPORT:
                excluded.append({'consumer': p['name'], 'input': short,
                                 'reason': 'normal compiler/Windows interface support; GPLv3 section 1 System Libraries exception. MinGW notices retained, not licensed as CRTV Sim code.'})
            elif short in TOOLS or PREFIX + short in shipped:
                excluded.append({'consumer': p['name'], 'input': short,
                                 'reason': 'build/test/documentation tool; executable is not distributed or incorporated'})
            else:
                raise RuntimeError(f'Unclassified build dependency: {p["name"]}: {short}')
        # Standard compiler startup/OS interface support is a System Library,
        # not a requirement to reproduce the entire compiler's implementation.
        for short in ('crt', 'crt-git', 'headers', 'headers-git', 'windows-default-manifest'):
            n = PREFIX + short
            if any(re.match(re.escape(n) + r'-\d', v) for v in installed):
                excluded.append({'consumer': p['name'], 'input': next(v for v in installed if re.match(re.escape(n)+r'-\d',v)),
                                 'reason': 'normal compiler/Windows interface support; GPLv3 section 1 System Libraries exception; original MinGW notices retained'})
        for name, reason in declarations.items():
            if name in {PREFIX+'cc-libs', PREFIX+'gcc-libs', PREFIX+'omp'}:
                excluded.append({'consumer': p['name'], 'input': name,
                                 'reason': 'normal compiler runtime; GPLv3 System Libraries/GCC Runtime Library Exception. Shipped runtime DLLs have their own exact source records.'})
                continue
            if name in shipped:
                if reason.startswith('declared runtime'):
                    continue  # shared DLL sources already paired with shipped bytes
            matches = [v for v in installed if re.match(re.escape(name) + r'-\d', v)]
            if not matches and name == PREFIX + 'vulkan':
                continue  # a virtual dependency; loader/headers are explicit
            if not matches and name == PREFIX + 'libmodplug':
                matches = [v for v in installed if re.match(re.escape(PREFIX+'libopenmpt-modplug')+r'-\d',v)]
            if not matches and name == PREFIX + 'omp':
                matches = [v for v in installed if re.match(re.escape(PREFIX+'libgomp')+r'-\d',v)]
                if not matches:
                    matches = [v for v in installed if re.match(re.escape(PREFIX+'gcc-libs')+r'-\d',v)]
            if not matches and name == PREFIX + 'cc-libs':
                matches = [v for v in installed if re.match(re.escape(PREFIX+'gcc-libs')+r'-\d',v)]
            if len(matches) != 1:
                raise RuntimeError(f'Cannot identify exact build input: {p["name"]}: {name}: {matches}')
            filename = matches[0] + '.pkg.tar.zst'
            if any(q['binary']['filename'] == filename for q in lock['packages']):
                continue
            wanted.setdefault(filename, []).append({'consumer': p['name'], 'reason': reason})
    return wanted, excluded


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, required=True)
    args = parser.parse_args()
    cache = args.cache.resolve()
    spec = importlib.util.spec_from_file_location('acquisition', Path(__file__).with_name('acquire-windows-media.py'))
    acquisition = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(acquisition)
    lock = json.loads(LOCK.read_text(encoding='utf-8'))
    wanted, excluded = required_inputs(lock, cache)
    def collect(pair):
        filename, consumers = pair
        print('Fetching exact input', filename, flush=True)
        binary = acquisition.download('https://repo.msys2.org/mingw/ucrt64/' + filename, cache / filename)
        info = fields(tar_bytes(binary, '.PKGINFO').decode())
        build = fields(tar_bytes(binary, '.BUILDINFO').decode())
        base, version, name = info['pkgbase'][0], info['pkgver'][0], info['pkgname'][0]
        source_name = base + '-' + version + '.src.tar.zst'
        source = acquisition.download('https://repo.msys2.org/mingw/sources/' + source_name, cache / source_name)
        recipe = tar_bytes(source, base + '/PKGBUILD')
        if hashlib.sha256(recipe).hexdigest() != build['pkgbuild_sha256sum'][0]:
            raise RuntimeError('Recipe differs from exact build record: ' + filename)
        result = {'name': name, 'base': base, 'version': version,
                  'licenses': info.get('license', []), 'consumers': consumers,
                  'provenance': build['packager'][0], 'recipe_sha256': build['pkgbuild_sha256sum'][0],
                  'binary': {'filename': filename, 'url': 'https://repo.msys2.org/mingw/ucrt64/'+filename, 'sha256': sha256(binary)},
                  'source': {'filename': source_name, 'url': 'https://repo.msys2.org/mingw/sources/'+source_name, 'sha256': sha256(source)}}
        write_json(cache / (filename + '.source-record.json'), result)
        print('Recovered', source_name, flush=True)
        return result
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(collect, sorted(wanted.items())))
    write_json(cache / 'incorporated-inputs.json', {'packages': records, 'excluded_tools': excluded})
    print(f'Recovered {len(records)} exact inputs')


if __name__ == '__main__':
    main()
