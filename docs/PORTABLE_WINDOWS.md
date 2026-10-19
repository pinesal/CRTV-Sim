# CRTV Sim portable Windows build

The portable ZIP remains self-contained: users do not install Python, MSYS2,
FFmpeg, ffprobe or mpv. Developers use the existing Python environment and
PyInstaller build script; no new package manager or media-player implementation
is introduced.

From the project root, the public release command is:

```powershell
.\scripts\build-portable-windows.ps1
```

It selects packages in `packaging/windows-media.lock.json`, verifies binary
and source hashes, matches source recipes to binary build records, freezes the
application with isolated DLL search paths, prepares notices/manifests, creates
paired Windows/source ZIPs and SHA256SUMS, then runs media and extracted-app
smoke checks. Output defaults to `dist/release-candidate/`. No upload occurs.

Public builds require the recovered sources and notices in the lock and a
licensed Visual Studio Community 2026/Professional/Enterprise installation
for the pinned Microsoft retail runtime. The existing 2022 Build Tools alone
are insufficient for this grant.
Missing evidence stops the build. See THIRD-PARTY.md and the release checklist.

For isolated internal investigation/testing:

```powershell
.\scripts\build-portable-windows.ps1 -InternalBuild `
  -Output 'dist\internal-candidate\CRTV-Sim-Portable-0.9.55'
```

This still verifies pinned packages and produces available source evidence, but
labels incomplete archives `-INTERNAL` and includes an internal-only warning.
Add `-SkipArchive` when an unpacked internal build suffices; public evidence
checks are still mandatory unless `-InternalBuild` is explicit. Only internal
builds may add `-SkipSmokeTests`. Neither option authorizes public distribution.
A build refuses to overwrite a directory with user-data. Existing packages and
production installations are not selected as output automatically.

When selecting updated media packages, explicitly refresh and review the lock:

```powershell
.venv\Scripts\python.exe scripts\acquire-windows-media.py
.venv\Scripts\python.exe scripts\pin-windows-toolchain.py .build\windows-media\acquired.json
```

These scripts collect supplier artifacts and dependency closure without
installing MSYS2 or executing supplier build recipes. Normal releases do not
refresh the lock. Review new source inputs and actual license terms, retain
approved artifacts, and resolve findings before using the public workflow.
The lock and scripts belong in Git; large packages/source archives stay in
`.build/` and `dist/`. Offline builds can use a preserved `-MediaCache` whose
contents match all locked hashes. Python packages/build tools are pinned in
`requirements.lock` and `requirements-build.lock`; changing the interpreter
requires a fresh native-runtime review, not an implicit license approval.

The package excludes installed user-data, personal media and development
settings. Fresh installations run setup before indexing. Public downloads do
not automatically import sibling installations. Existing profiles skip setup;
their inherited defaults and personal settings are preserved. A new bundled
Microsoft runtime requires a one-time acknowledgement of its terms, without
repeating library setup. Updates can be
copied over an existing installation while retaining its user-data and media.
Update shortcuts to the new launcher rather than starting a leftover RetroTV.exe.

See [DISTRIBUTION.md](DISTRIBUTION.md) for setup, safe shutdown, updates,
rollback and the remaining public-release gates. The build script creates a
clean release package: never target a live installation containing user-data.

The internal retrotv package, database paths, environment variables, credential
targets and production markers retain their existing names for compatibility.
Recovery recognizes both CRTV-Sim.exe and legacy RetroTV.exe. RetroWeather
keeps its separate name.
