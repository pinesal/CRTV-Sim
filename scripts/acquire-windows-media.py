"""Collect supplier packages and PE DLL closure for an explicit lock refresh.

Run this only when choosing new tool versions, then review the generated lock.
Normal builds use that lock and never consult a changing repository database.
No packages are installed; no supplier build recipes are executed.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import tempfile
import urllib.request

from windows_toolchain import CACHE, EvidenceError, fields, pe_imports, safe_relative, sha256, system_import, tar_bytes, tar_names, write_json


def download(url, path, expected=None):
    import truststore
    if not url.startswith("https://repo.msys2.org/"):
        raise EvidenceError("Unexpected supplier URL")
    if not path.is_file():
        partial = path.with_suffix(path.suffix + ".partial")
        try:
            with urllib.request.urlopen(url, context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), timeout=120) as response, partial.open("wb") as target:
                shutil.copyfileobj(response, target)
            if expected and sha256(partial) != expected:
                raise EvidenceError(f"Supplier package checksum mismatch: {path.name}")
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
    if expected and sha256(path) != expected:
        raise EvidenceError(f"Cached package checksum mismatch: {path.name}")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=CACHE)
    args = parser.parse_args()
    cache = args.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    database = download("https://repo.msys2.org/mingw/ucrt64/ucrt64.files", cache / "ucrt64.files")
    names = tar_names(database)
    # The repository database contains only regular desc/files records. Reject
    # links before extraction; all paths were containment-checked above.
    listing = subprocess.check_output(["tar.exe", "-tvf", str(database)]).decode("utf-8")
    if any(line.startswith(("l", "h")) for line in listing.splitlines()):
        raise EvidenceError("Repository database unexpectedly contains links")
    rows, by_file = {}, {}
    with tempfile.TemporaryDirectory(prefix="msys2-database-", dir=cache) as directory:
        subprocess.run(["tar.exe", "-xf", str(database), "-C", directory], check=True)
        for desc in Path(directory).glob("*/desc"):
            text = desc.read_text(encoding="utf-8")
            row = {match[1]: match[2].splitlines() for match in re.finditer(r"%([^%]+)%\n(.*?)(?:\n\n|$)", text, re.S)}
            name = row["NAME"][0]
            rows[name] = row
            for item in (desc.parent / "files").read_text(encoding="utf-8").splitlines():
                if item.startswith("ucrt64/bin/") and item.lower().endswith((".exe", ".dll")):
                    by_file[Path(item).name.lower()] = (name, item)
    packages, binaries = {}, {}
    pending = ["ffmpeg.exe", "ffprobe.exe", "mpv.exe"]
    while pending:
        name = pending.pop(0)
        if name in binaries or system_import(name):
            continue
        if name not in by_file:
            raise EvidenceError(f"Supplier has no package for imported DLL {name}")
        package_name, relative = by_file[name]
        if package_name not in packages:
            row = rows[package_name]
            filename = safe_relative(row["FILENAME"][0])
            path = download("https://repo.msys2.org/mingw/ucrt64/" + filename, cache / filename, row["SHA256SUM"][0])
            packages[package_name] = {"row": row, "binary": str(path), "info": fields(tar_bytes(path, ".PKGINFO").decode()), "build": fields(tar_bytes(path, ".BUILDINFO").decode())}
        package = packages[package_name]
        payload = tar_bytes(Path(package["binary"]), relative)
        # Persist just the reviewed PE files, not package scripts or symlinks.
        binary = cache / "pe" / Path(relative).name
        binary.parent.mkdir(exist_ok=True)
        binary.write_bytes(payload)
        imports = pe_imports(binary)
        binaries[name] = {"package": package_name, "path": str(binary), "sha256": sha256(binary), "imports": imports}
        pending.extend(imports)
        print("Collected", name, flush=True)
    def collect_source(pair):
        name, package = pair
        info = package["info"]
        filename = info["pkgbase"][0] + "-" + info["pkgver"][0] + ".src.tar.zst"
        path = download("https://repo.msys2.org/mingw/sources/" + filename, cache / filename)
        recipe = tar_bytes(path, info["pkgbase"][0] + "/PKGBUILD")
        if hashlib.sha256(recipe).hexdigest() != package["build"]["pkgbuild_sha256sum"][0]:
            raise EvidenceError(f"Supplier source recipe differs from binary: {name}")
        package["source"] = {"path": str(path), "sha256": sha256(path)}
        print("Collected source", filename, flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(collect_source, packages.items()))
    report = cache / "acquired.json"
    write_json(report, {"database_sha256": sha256(database), "packages": packages, "binaries": binaries})
    print(f"Review and pin with: python scripts/pin-windows-toolchain.py \"{report}\"")


if __name__ == "__main__":
    main()
