"""Pinned Windows media packages and release evidence; never executes PKGBUILDs.

The lock records supplier evidence, not a legal certification. Public packaging
fails on unresolved source inputs. An internal build can collect an explicitly
incomplete candidate for investigation without changing that release gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
LOCK = PROJECT / "packaging/windows-media.lock.json"
CACHE = PROJECT / ".build/windows-media"
# These are Windows OS interfaces, not arbitrary files found in System32.
SYSTEM_DLLS = frozenset("advapi32 avicap32 avrt bcrypt bcryptprimitives cfgmgr32 combase comctl32 comdlg32 crypt32 d2d1 d3d9 d3d11 d3d12 dcomp dhcpcsvc dnsapi dsound dwmapi dwrite dxgi dxva2 gdi32 gdiplus hid imm32 iphlpapi kernel32 ksuser mf mfplat mfreadwrite mfuuid msimg32 msvcrt ncrypt netapi32 normaliz ntdll ole32 oleaut32 opengl32 powrprof propsys psapi rpcrt4 secur32 setupapi shcore shell32 shlwapi ucrtbase user32 userenv usp10 uxtheme version winhttp winmm winspool wintrust wldap32 ws2_32 wsock32 wtsapi32".split())


class EvidenceError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
        raise EvidenceError(f"Unsafe archive path: {value!r}")
    return path.as_posix()


def check_hash(path: Path, expected: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", expected or ""):
        raise EvidenceError(f"Missing SHA-256 for {path.name}")
    if not path.is_file() or sha256(path) != expected:
        raise EvidenceError(f"Missing or incorrect SHA-256: {path.name}")


def fetch(record: dict, cache: Path) -> Path:
    filename = safe_relative(record["filename"])
    if "/" in filename:
        raise EvidenceError(f"Cache filename must be a basename: {filename}")
    path = cache / filename
    cache.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        url = record["url"]
        if not url.startswith("https://repo.msys2.org/"):
            raise EvidenceError(f"Unexpected package origin: {url}")
        import truststore
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        partial = path.with_suffix(path.suffix + ".partial")
        try:
            with urllib.request.urlopen(url, context=context, timeout=120) as response, partial.open("wb") as target:
                shutil.copyfileobj(response, target)
            check_hash(partial, record["sha256"])
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
    check_hash(path, record["sha256"])
    return path


def tar_bytes(path: Path, name: str) -> bytes:
    safe_relative(name)
    result = subprocess.run(["tar.exe", "-xOf", str(path), name], capture_output=True, timeout=180)
    if result.returncode:
        raise EvidenceError(f"Cannot read {name} from {path.name}: {result.stderr.decode(errors='replace')[:200]}")
    return result.stdout


def tar_names(path: Path) -> list[str]:
    result = subprocess.run(["tar.exe", "-tf", str(path)], capture_output=True, timeout=180, check=True)
    names = result.stdout.decode("utf-8").splitlines()
    for name in names:
        safe_relative(name.rstrip("/"))
    return names


def fields(text: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for line in text.splitlines():
        if " = " in line:
            key, value = line.lstrip().split(" = ", 1)
            result.setdefault(key, []).append(value)
    return result


def pe_imports(path: Path) -> list[str]:
    import pefile
    with pefile.PE(str(path), fast_load=True) as binary:
        binary.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"], pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
        return sorted({entry.dll.decode("ascii").lower() for attribute in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT") for entry in getattr(binary, attribute, [])})


def system_import(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(".dll") and (lower[:-4] in SYSTEM_DLLS or lower.startswith(("api-ms-win-", "ext-ms-win-")))


def closure(root: str, binaries: dict) -> set[str]:
    pending = [root.lower()]
    selected: set[str] = set()
    while pending:
        name = pending.pop()
        if name in selected or system_import(name):
            continue
        if name not in binaries:
            raise EvidenceError(f"Unaccounted DLL dependency: {name} (from {root})")
        selected.add(name)
        pending.extend(binaries[name]["imports"])
    return selected


def verify_vcs_snapshot(archive: Path, names: set[str], base: str, source: str) -> None:
    """Read the archived bare Git repository; never check out or build code."""
    url = source.split('::')[-1]
    reference = url.partition('#')[2]
    kind, _, value = reference.partition('=')
    if kind not in ('commit', 'tag') or not value or not re.fullmatch(r'[A-Za-z0-9._/-]+', value):
        raise EvidenceError(f'Unpinned VCS source: {source}')
    git_name = source.split('::')[0] if '::' in source else url.split('#')[0].rstrip('/').rsplit('/', 1)[-1].removesuffix('.git')
    root = base + '/' + git_name
    if root + '/HEAD' not in names:
        raise EvidenceError(f'Archived Git repository missing: {source}')
    checksum = sha256(archive)
    proof_file = archive.parent / ('vcs-' + checksum + '.json')
    # The proof is produced again from the source archive, not trusted as an
    # approval checkbox. A modified cache marker cannot waive source checks.
    proof = json.loads(proof_file.read_text()) if proof_file.is_file() else {}
    temp_root = PROJECT / '.build/vcs-audit'
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temp_root) as directory:
        listing = subprocess.check_output(['tar.exe', '-tvf', str(archive), root + '/']).decode('utf-8')
        if any(line.startswith(('l', 'h')) for line in listing.splitlines()):
            raise EvidenceError(f'Unexpected link inside archived bare Git repository: {source}')
        subprocess.run(['tar.exe', '-xf', str(archive), '-C', directory, root + '/'], check=True, timeout=180)
        git_dir = Path(directory) / root
        env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}
        git = [os.environ.get('CRTV_BUILD_GIT', 'git'), '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=' + os.devnull, '--git-dir=' + str(git_dir)]
        commit = subprocess.check_output([*git, 'rev-parse', value + '^{commit}'], env=env, timeout=60).decode().strip()
        if kind == 'commit' and commit != value:
            raise EvidenceError(f'Archived Git commit differs from pinned source: {source}')
        # A tar snapshot reads all trees/blobs, catching missing Git objects and
        # recording a readily reproducible snapshot without full Git history.
        snapshot = Path(directory) / 'snapshot.tar'
        with snapshot.open('wb') as target:
            subprocess.run([*git, 'archive', '--format=tar', commit], stdout=target, env=env, check=True, timeout=180)
        proof[source] = {'archive_sha256': checksum, 'commit': commit, 'snapshot_sha256': sha256(snapshot)}
        write_json(proof_file, proof)


def source_check(package: dict, archive: Path, binary_archive: Path, *, check_cargo: bool = True) -> list[str]:
    """Match recipes to .BUILDINFO and verify non-VCS source checksums."""
    base = package["base"]
    recipe = tar_bytes(archive, base + "/PKGBUILD")
    build = fields(tar_bytes(binary_archive, ".BUILDINFO").decode("utf-8"))
    info = fields(tar_bytes(binary_archive, ".PKGINFO").decode("utf-8"))
    if info.get("pkgname") != [package["name"]] or info.get("pkgver") != [package["version"]]:
        raise EvidenceError(f"Package identity mismatch: {package['name']}")
    recipe_hash = hashlib.sha256(recipe).hexdigest()
    if build.get("pkgbuild_sha256sum") != [recipe_hash] or recipe_hash != package["recipe_sha256"]:
        raise EvidenceError(f"Source recipe does not match binary build record: {package['name']}")
    srcinfo = fields(tar_bytes(archive, base + "/.SRCINFO").decode("utf-8"))
    names = set(tar_names(archive))
    problems = []
    for suffix in ("", "_x86_64"):
        sources, checksums = srcinfo.get("source" + suffix, []), srcinfo.get("sha256sums" + suffix, [])
        if sources and len(checksums) != len(sources):
            problems.append(f"{package['name']}: source declarations need checksum audit")
            continue
        for source, expected in zip(sources, checksums):
            alias, separator, url = source.partition("::")
            if not separator:
                url = alias
            filename = alias if separator else url.rsplit("/", 1)[-1].split("?")[0]
            if url.startswith(("git+", "hg+", "svn+")):
                # makepkg VCS checksums do not hash a source tarball. Preserve
                # the supplier archive, but require a separate checkout audit.
                if url.startswith('git+'):
                    verify_vcs_snapshot(archive, names, base, source)
                else:
                    problems.append(f"{package['name']}: VCS snapshot must be checked against {url}")
                continue
            name = base + "/" + filename
            if name not in names:
                raise EvidenceError(f"Missing declared source input: {package['name']}: {filename}")
            if expected == "SKIP":
                if not filename.endswith((".asc", ".sig")):
                    problems.append(f"{package['name']}: unchecked input {filename}")
            elif hashlib.sha256(tar_bytes(archive, name)).hexdigest() != expected:
                raise EvidenceError(f"Declared source checksum mismatch: {package['name']}: {filename}")
    text = recipe.decode("utf-8")
    if check_cargo and re.search(r"cargo\s+(fetch|update|build|cbuild|capi)", text):
        problems.append(f"{package['name']}: Cargo registry/git inputs and effective Cargo.lock need a matching vendored-source audit")
    return problems


def verify(lock: dict, cache: Path, *, internal: bool = False) -> dict:
    if lock.get("schema") != 1:
        raise EvidenceError("Unsupported media lock schema")
    problems = list(lock.get("unresolved", []))
    packages = {}
    for package in lock["packages"]:
        binary = fetch(package["binary"], cache)
        source = fetch(package["source"], cache)
        problems.extend(source_check(package, source, binary, check_cargo=not bool(lock.get('source_inputs'))))
        packages[package["name"]] = (package, binary, source)
    for name, item in lock["binaries"].items():
        safe_relative(item["path"])
        package, archive, _ = packages[item["package"]]
        payload = tar_bytes(archive, item["path"])
        if hashlib.sha256(payload).hexdigest() != item["sha256"]:
            raise EvidenceError(f"Binary hash mismatch inside approved package: {name}")
    for root in ("ffmpeg.exe", "ffprobe.exe", "mpv.exe"):
        closure(root, lock["binaries"])
    from release_source_checks import verify_inputs
    problems.extend(verify_inputs(lock, cache, packages))
    result = {"status": "INCOMPLETE — INTERNAL ONLY" if problems else "Verified pinned binaries, source inputs and redistribution notices", "problems": sorted(set(problems))}
    if problems and not internal:
        raise EvidenceError("Public packaging blocked:\n- " + "\n- ".join(result["problems"]))
    return result


def stage(lock: dict, cache: Path, output: Path, evidence: dict) -> None:
    """Keep the established ffmpeg/mpv paths so existing profiles still work."""
    packages = {p["name"]: p for p in lock["packages"]}
    mapped = {}
    for directory, roots in (("ffmpeg", ("ffmpeg.exe", "ffprobe.exe")), ("mpv", ("mpv.exe",))):
        names = set().union(*(closure(root, lock["binaries"]) for root in roots))
        for name in sorted(names):
            item = lock["binaries"][name]
            package = packages[item["package"]]
            destination = output / "tools" / directory / Path(item["path"]).name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(tar_bytes(cache / package["binary"]["filename"], item["path"]))
            check_hash(destination, item["sha256"])
            imports = pe_imports(destination)
            if imports != sorted(set(item["imports"])):
                raise EvidenceError(f"PE import map differs from lock: {name}")
            mapped[destination.relative_to(output).as_posix()] = {"sha256": item["sha256"], "package": package["name"], "version": package["version"], "licenses": package["licenses"], "source": "msys2/" + package["source"]["filename"], "provenance": package["provenance"], "imports": imports}
    notices = output / "licenses/msys2"
    for package in lock["packages"]:
        for name in package["notices"]:
            target = notices / package["name"] / safe_relative(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar_bytes(cache / package["binary"]["filename"], name))
    if lock.get('source_inputs'):
        from release_source_checks import stage_inputs
        stage_inputs(lock, cache, output)
    write_json(output / "MEDIA-TOOLCHAIN.json", {"lock_sha256": sha256(LOCK), "evidence": evidence, "binaries": mapped, "packages": lock["packages"]})
    if evidence["problems"]:
        (output / "INTERNAL-BUILD-NOT-FOR-DISTRIBUTION.txt").write_text("This is an internal test candidate. Do not publish.\n\n" + "\n".join(evidence["problems"]) + "\n", encoding="utf-8")


def verify_output(output: Path) -> dict:
    data = json.loads((output / "MEDIA-TOOLCHAIN.json").read_text(encoding="utf-8"))
    lock = json.loads(LOCK.read_text(encoding='utf-8'))
    if data['lock_sha256'] != sha256(LOCK) or data['packages'] != lock['packages']:
        raise EvidenceError('Packaged media manifest differs from the approved toolchain lock')
    approved = data["binaries"]
    actual = {p.relative_to(output).as_posix() for p in (output / "tools").rglob("*") if p.suffix.lower() in (".exe", ".dll")}
    if actual != set(approved):
        raise EvidenceError(f"Unexpected or missing tool binaries: {sorted(actual ^ set(approved))}")
    for relative, item in approved.items():
        path = output / safe_relative(relative)
        check_hash(path, item["sha256"])
        locked = lock['binaries'].get(path.name.lower())
        if not locked or item['sha256'] != locked['sha256'] or item['package'] != locked['package']:
            raise EvidenceError(f'Packaged binary identity differs from the approved lock: {relative}')
        if pe_imports(path) != item["imports"]:
            raise EvidenceError(f"Changed imports: {relative}")
        for name in item["imports"]:
            if not system_import(name) and not any(p.name.lower() == name for p in path.parent.glob("*")):
                raise EvidenceError(f"Missing runtime DLL {name} for {relative}")
    return data


def frozen_inventory(output: Path) -> dict:
    """Account for frozen native libraries, including the interpreter's DLLs.

    Conda metadata is provenance, not blanket permission to redistribute a
    runtime. Copy component notices, retain unresolved permission questions.
    Never put local installation paths or credential files in the manifest.
    """
    import importlib.metadata
    import tomllib
    application_version = tomllib.loads((PROJECT / 'pyproject.toml').read_text())['project']['version']
    origins = {}
    prefix = Path(sys.base_prefix)
    metadata = prefix / "conda-meta"
    for record_path in metadata.glob("*.json"):
        record = json.loads(record_path.read_text(encoding="utf-8"))
        package = {key: record.get(key) for key in ("name", "version", "build", "license", "url", "sha256")}
        cache = prefix / "pkgs" / f"{record['name']}-{record['version']}-{record['build']}"
        notice_files = list((cache / "info/licenses").rglob("*"))
        notices = []
        for source in notice_files:
            if source.is_file():
                relative = Path("licenses/python-native") / cache.name / source.relative_to(cache / "info/licenses")
                destination = output / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                notices.append(relative.as_posix())
        package["notices"] = notices
        for relative in record.get("files", []):
            path = prefix / relative
            if path.suffix.lower() in (".dll", ".pyd") and path.is_file():
                origins.setdefault(sha256(path), package)
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get('Name', 'unknown')
        notices = [str(p.relative_to(output)) for p in (output / 'licenses' / name).glob('*') if p.is_file()]
        for relative in distribution.files or []:
            path = Path(distribution.locate_file(relative))
            if path.suffix.lower() in ('.dll', '.pyd') and path.is_file():
                origins[sha256(path)] = {'name': name, 'version': distribution.version,
                                        'license': distribution.metadata.get('License-Expression') or distribution.metadata.get('License'),
                                        'source': 'python-packages/' + name, 'notices': notices}
    pyinstaller = importlib.metadata.version("pyinstaller")
    result, problems = {}, []
    runtime_file = output/'MICROSOFT-RUNTIME.json'
    runtime = json.loads(runtime_file.read_text(encoding='utf-8')) if runtime_file.is_file() else None
    if runtime:
        problems.extend(runtime['problems'])
        pinned_runtime = json.loads((PROJECT/'packaging/microsoft-runtime.lock.json').read_text(encoding='utf-8'))
        for key in ('version', 'sha256', 'license_grant', 'notices', 'bundle'):
            if runtime.get(key) != pinned_runtime[key]:
                problems.append('Microsoft runtime record differs from pinned redistribution evidence: ' + key)
        if runtime.get('licensed_build_product') not in ('Microsoft.VisualStudio.Product.Community', 'Microsoft.VisualStudio.Product.Professional', 'Microsoft.VisualStudio.Product.Enterprise'):
            problems.append('Microsoft runtime lacks a licensed Visual Studio build product')
        if not str(runtime.get('licensed_build_version', '')).startswith('18.'):
            problems.append('Microsoft grant does not match the licensed Visual Studio edition version')
    for path in sorted(output.rglob("*")):
        if not path.is_file() or "tools" in path.relative_to(output).parts or path.suffix.lower() not in (".exe", ".dll", ".pyd"):
            continue
        checksum = sha256(path)
        origin = origins.get(checksum)
        if path.name.lower() == 'vcruntime140.dll':
            if not runtime or checksum != runtime['sha256']:
                problems.append('Microsoft VCRUNTIME140.dll lacks a matching official redistribution record')
            else:
                for filename, record in runtime['notices'].items():
                    check_hash(output/'licenses/microsoft'/filename,record['sha256'])
                origin={'name':'Microsoft Visual C++ retail runtime','version':runtime['version'],
                        'license':'Microsoft Visual Studio Distributable Code; Microsoft runtime terms',
                        'source':'unmodified retail DLL extracted from signed Microsoft redistributable (proprietary; no source redistribution grant)',
                        'license_grant':runtime['license_grant'],
                        'notices':['licenses/microsoft/'+n for n in runtime['notices']]}
        if path.name in ("CRTV-Sim.exe", "CRTV-Sim-Safe-Stop.exe"):
            origin = {"name": "CRTV Sim + PyInstaller bootloader", "version": application_version + " / " + pyinstaller, "license": "MIT (original app); GPL-2.0-or-later WITH Bootloader-exception (bootloader)", "source": "CRTV Sim repository; python-packages/pyinstaller", "notices": ["LICENSE", "licenses/pyinstaller/COPYING.txt"]}
        if origin is None:
            origin = {"name": "unresolved frozen native component", "version": None, "license": None, "notices": []}
            problems.append(f"No matching runtime provenance: {path.relative_to(output).as_posix()}")
        elif not origin.get("notices") and path.suffix.lower() != ".exe":
            problems.append(f"Runtime notice missing: {origin['name']} ({path.name})")
        result[path.relative_to(output).as_posix()] = {"sha256": checksum, "origin": origin, "imports": pe_imports(path)}
    for relative, item in result.items():
        directory = (output / relative).parent
        available = {p.name.lower() for folder in (directory, output / "_internal") for p in folder.glob("*") if p.is_file()}
        for dependency in item["imports"]:
            if dependency not in available and not system_import(dependency):
                problems.append(f"Frozen DLL dependency missing: {relative}: {dependency}")
    return {"native_binaries": result, "problems": sorted(set(problems))}


def source_archive(output: Path, cache: Path, version: str, *, internal: bool) -> Path:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    evidence = verify(lock, cache, internal=internal)
    data = verify_output(output)
    label = version + ("-INTERNAL" if internal else "")
    target = output.parent / f"CRTV-Sim-{label}-ThirdParty-Sources.zip"
    manifest = {"evidence": evidence, "media": data, "sources": {}}
    # Two independently readable ZIPs keep each source download below GitHub's
    # per-asset limit without omitting large supplier source inputs.
    extra = output.parent / f'CRTV-Sim-{label}-ThirdParty-Build-Inputs.zip'
    extra_sources = {}
    primary = {p['source']['filename'] for p in lock['packages']}
    with zipfile.ZipFile(extra, 'w', zipfile.ZIP_STORED, allowZip64=True) as archive:
        for package in lock.get('source_inputs', {}).get('packages', []):
            source = package['source']
            name = 'msys2/' + source['filename']
            if source['filename'] not in primary and name not in extra_sources:
                archive.write(cache/source['filename'], name)
                extra_sources[name] = source
        archive.writestr('README.txt', 'Required companion to the same-version ThirdParty-Sources.zip. Keep both source downloads beside the Windows download. Original supplier source archives are retained in full.\n')
    manifest['additional_archives'] = {extra.name: {'sha256': sha256(extra), 'sources': extra_sources}}
    with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
        included = set()
        for package in lock["packages"] + lock.get('source_inputs', {}).get('packages', []):
            source = package["source"]
            path = cache / source["filename"]
            name = "msys2/" + source["filename"]
            if source['filename'] in primary and name not in included:
                archive.write(path, name)
                manifest["sources"][name] = source
                included.add(name)
            for member in (".PKGINFO", ".BUILDINFO"):
                archive.writestr("build-records/" + package["name"] + "/" + package['version'] + "/" + member, tar_bytes(cache / package["binary"]["filename"], member))
            proof = cache / ('vcs-' + source['sha256'] + '.json')
            if proof.is_file():
                archive.write(proof, 'vcs-proofs/' + proof.name)
        for record in lock.get('source_inputs', {}).get('cargo', []):
            name='rust/'+record['filename']
            archive.write(cache/record['filename'],name)
            manifest['sources'][name]={'filename':record['filename'],'sha256':record['sha256'],
                                      'basis':'cargo vendor --locked; upstream/effective locks and all registry checksums verified'}
        # Retain complete Python package files, including certifi's MPL bundle,
        # not only the installed license files. No editable app distribution.
        import importlib.metadata
        for distribution in importlib.metadata.distributions():
            name = distribution.metadata.get("Name", "unknown")
            if name.lower() in ("retrotv", "retroweather", "pip", "setuptools"):
                continue
            for file in distribution.files or []:
                relative = Path(str(file))
                if relative.is_absolute() or ".." in relative.parts or relative.suffix == ".pyc":
                    continue
                path = Path(distribution.locate_file(file))
                if path.is_file():
                    archive.write(path, "python-packages/" + name + "/" + relative.as_posix())
        for file in (LOCK, PROJECT / "scripts/windows_toolchain.py", PROJECT / 'scripts/release_source_checks.py', PROJECT / 'scripts/acquire-windows-media.py', PROJECT / 'scripts/pin-windows-toolchain.py', PROJECT / 'scripts/collect-incorporated-sources.py', PROJECT / 'scripts/pin-cargo-sources.py', PROJECT / 'scripts/collect-cargo-notices.py', PROJECT / 'scripts/collect-upstream-notices.py', PROJECT / 'scripts/build-portable-windows.ps1', PROJECT / 'scripts/prepare-distribution.py', PROJECT / 'requirements.lock', PROJECT / 'requirements-build.lock', PROJECT / "docs/THIRD-PARTY.md", PROJECT / "docs/BUNDLED-SOFTWARE-RELEASE-CHECKLIST.md"):
            archive.write(file, "release-recipes/" + file.name)
        for path in sorted((output / "licenses").rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output).as_posix())
        for path in (PROJECT/'scripts/microsoft_runtime.py', PROJECT/'packaging/microsoft-runtime.lock.json', PROJECT/'scripts/check-media-tools.py', PROJECT/'scripts/smoke-distribution.py'):
            archive.write(path, 'release-recipes/'+path.name)
        archive.write(output/'MICROSOFT-RUNTIME.json', 'MICROSOFT-RUNTIME.json')
        readme = """CRTV Sim matching third-party sources and build materials

Read SOURCE-MANIFEST.json, release-recipes/windows-media.lock.json and the
release checklist before redistribution. Any listed problem blocks public
release. Publish this same-version source download AND its required companion
ThirdParty-Build-Inputs.zip beside the Windows ZIP. The manifest binds that
companion archive and all of its source files by SHA-256. Both are ordinary ZIPs
and can be extracted independently; no multipart archive software is needed.

The msys2/ directory contains supplier full-source archives (upstream code,
PKGBUILD compilation recipes, .SRCINFO, and patches). build-records/ contains
the exact binary .BUILDINFO and .PKGINFO. The PKGBUILD hashes were compared.
No CRTV Sim patches were applied to the media executables or their DLLs.

To inspect: Windows 10/11 tar.exe -tf msys2/<package>.src.tar.zst
To reproduce: use the MSYS2 UCRT64 build environment and makepkg-mingw for
each preserved PKGBUILD, with the versions recorded in its .BUILDINFO.
See https://www.msys2.org/wiki/Creating-Packages/ . Build prerequisites are
for contributors only; the Windows application needs no MSYS2 installation.
These instructions describe supplier builds; byte-identical rebuilding has
not been demonstrated. Exact incorporated static/header dependency versions
are retained in source_inputs.packages, with consumer mappings and reasons.
Compiler/build tools and normal System Libraries are recorded separately;
their executables are not incorporated application source requirements.

rust/ contains complete cargo vendor --locked inputs for rav1e, libdovi and
librsvg, original registry .crate files, original and effective Cargo.lock,
all file/package checksums and upstream or documented supplemental notices.
Extract the corresponding vendor ZIP alongside its upstream working tree.
Copy the included effective Cargo.lock to the lock location recorded in
lock_member (dolby_vision/Cargo.lock for libdovi). Point Cargo's crates.io
replacement to the extracted vendor directory using its config.toml, changing
only the directory path. Use CARGO_NET_OFFLINE=true and the original recipe's
compiler version and build features. For librsvg the only lock transformation
is its supplier prepare() downgrade of windows-sys; do not run an unconstrained
cargo update. Supplemental notice provenance explains exact upstream commits
or publisher SPDX declarations; canonical texts are not invented notices.

Original upstream notices accompany the app and this archive. Microsoft
VCRUNTIME140.dll is an unmodified retail redistributable with a separate
licensed Visual Studio grant and end-user terms; its proprietary source is
neither claimed nor included. MICROSOFT-RUNTIME.json records that distinction.

Python package files and their notices are preserved in python-packages/.
The app remains MIT; bundled components retain their own license terms.
"""
        archive.writestr("README-SOURCES.txt", readme)
        archive.writestr("SOURCE-MANIFEST.json", json.dumps(manifest, indent=2))
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise EvidenceError("Source archive integrity failure")
        for name, record in manifest["sources"].items():
            with archive.open(name) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != record["sha256"]:
                    raise EvidenceError(f"Source ZIP hash mismatch: {name}")
    verify_source_companions(target, manifest)
    return target


def verify_source_companions(primary, manifest):
    """A missing, changed or incomplete companion must block publication."""
    recovered = {name: record['sha256'] for name, record in manifest['sources'].items()}
    paths = []
    for name, record in manifest.get('additional_archives', {}).items():
        safe_relative(name)
        if Path(name).name != name:
            raise EvidenceError('Source companion must be next to the primary archive')
        path = primary.parent/name
        check_hash(path, record['sha256'])
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise EvidenceError('Source companion integrity failure')
            for member, item in record['sources'].items():
                with archive.open(safe_relative(member)) as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != item['sha256']:
                        raise EvidenceError('Source companion file checksum mismatch: '+member)
                recovered[member] = item['sha256']
        paths.append(path)
    if manifest['media'].get('lock_sha256'):
        lock = json.loads(LOCK.read_text(encoding='utf-8'))
        expected = {'msys2/'+p['source']['filename']: p['source']['sha256']
                    for p in lock['packages'] + lock['source_inputs']['packages']}
        expected.update({'rust/'+p['filename']: p['sha256'] for p in lock['source_inputs']['cargo']})
        if recovered != expected:
            raise EvidenceError('Source archive coverage differs from approved source inputs')
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "stage", "sources"))
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--version", default="0.9.53")
    parser.add_argument("--internal", action="store_true", help="Label incomplete evidence as INTERNAL; never certify it")
    args = parser.parse_args()
    if args.output and not args.output.resolve().is_relative_to((PROJECT / "dist").resolve()):
        raise EvidenceError("Output must be inside project dist")
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    if args.action == "sources":
        if not args.output:
            parser.error("sources requires --output")
        print(source_archive(args.output.resolve(), args.cache, args.version, internal=args.internal))
    else:
        evidence = verify(lock, args.cache, internal=args.internal)
        if args.action == "stage":
            if not args.output:
                parser.error("stage requires --output")
            stage(lock, args.cache, args.output.resolve(), evidence)
        print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (EvidenceError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
