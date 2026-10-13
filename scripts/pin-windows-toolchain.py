"""Freeze a previously acquired MSYS2 package set into a reviewable lock.

Input is the acquisition report, not a build computer PATH. This deliberately
does not clear audit findings or approve upstream licenses automatically.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess

from windows_toolchain import LOCK, fields, pe_imports, sha256, tar_bytes, tar_names, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    acquired = json.loads(args.report.read_text(encoding="utf-8"))
    packages = []
    for name, row in sorted(acquired["packages"].items()):
        binary, source = Path(row["binary"]), Path(row["source"]["path"])
        info, build = row["info"], row["build"]
        packages.append({
            "name": name, "base": info["pkgbase"][0], "version": info["pkgver"][0],
            "licenses": info.get("license", []), "provenance": build["packager"][0],
            "recipe_sha256": build["pkgbuild_sha256sum"][0],
            "binary": {"filename": binary.name, "url": "https://repo.msys2.org/mingw/ucrt64/" + binary.name, "sha256": sha256(binary)},
            "source": {"filename": source.name, "url": "https://repo.msys2.org/mingw/sources/" + source.name, "sha256": sha256(source)},
            "notices": [member for member in tar_names(binary) if "/share/licenses/" in member and not member.endswith("/")],
        })
    binaries = {name: {"package": row["package"], "path": "ucrt64/bin/" + Path(row["path"]).name, "sha256": sha256(Path(row["path"])), "imports": pe_imports(Path(row["path"]))} for name, row in sorted(acquired["binaries"].items())}
    unresolved = [
        "Static/header build inputs beyond PE imports need exact-build source accounting (including dlfcn, MinGW CRT/headers, fast_float, xxhash and SPIR-V/shader inputs)",
        "Runtime-loaded plugins/data need closure verification beyond PE imports (Mesa drivers, fontconfig, VapourSynth Python and frei0r)",
        "Frozen Python native dependencies need matching license and redistribution provenance; the existing Miniconda runtime cannot be certified from Python LICENSE.txt alone",
        "Supplier license labels are best-effort metadata; audit actual source license texts and linked combinations before publication",
    ]
    write_json(LOCK, {"schema": 1, "supplier": "MSYS2 UCRT64", "database_sha256": acquired["database_sha256"], "packages": packages, "binaries": binaries, "unresolved": unresolved})
    print(f"Pinned {len(packages)} packages / {len(binaries)} binaries in {LOCK}")


if __name__ == "__main__":
    main()
