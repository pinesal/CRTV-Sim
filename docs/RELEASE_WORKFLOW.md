# Reusing completed release work

Release preparation and upload are separate steps. Do not rebuild a validated
candidate merely to upload it. Do not upload another copy of unchanged third-party
sources or optional media packs just to give them the new app version's name.

## Build or reuse a candidate

Run `scripts/build-portable-windows.ps1` for a new candidate. It records compilation
inputs and hashes of the executables and `_internal` files in `FROZEN-BUILD.json`.
Subsequent builds can reuse the last validated compiled files when those inputs
and every cached file still match. Changed code, app version, compiler script,
Python packages or native runtime inputs force fresh compilation. Older builds
without a receipt fall back to normal compilation; no receipt is invented for them.

To select a cache explicitly, pass `-ReuseBuild 'dist\<clean-build-directory>'`.
Never use a personal/live installation as a cache. Reuse does not skip media
capability, source-evidence, privacy, or actual Windows ZIP smoke checks.

## Reuse published downloads

`packaging/reusable-release-assets.json` records already published source and
media downloads with their SHA256 hashes, sizes and URLs. The current baseline is
the verified 0.9.55 release.

Source reuse requires matching media evidence, frozen native dependencies,
actual Python package source bytes and dependency locks. If anything differs,
the normal full source-packaging gate applies. If everything matches:

- The application includes `SOURCE-DOWNLOADS.json` pointing to existing archives.
- A small same-version `Build-Materials.zip` supplies current build scripts,
  locks and notices. It supplements the linked sources rather than replacing them.
- Unchanged optional media packs are linked from their existing release.
- A changed local media pack is checked against its updated `.zip.sha256` file
  and ZIP integrity, then included among the new uploads.

Referenced downloads must remain available. Do not delete a release holding
shared sources/media while another release links to it. Missing or changed
downloads stop publication; there is no silent source-availability waiver.

After publishing genuinely changed dependencies, register the verified new
source bundle with `release_reuse.py register --output <candidate> --tag <tag>`.
After publishing a changed media pack, use `release_reuse.py register-media
--tag <tag> --media <zip>`. Both commands verify GitHub's recorded hashes before
changing the registry. Commit that registry update for subsequent builders.

## Inspect and publish without rebuilding

Successful public build checks write two files beside the ZIP:

- `RELEASE-DOWNLOADS.json`: exact new uploads and existing download links.
- `RELEASE-VALIDATION.json`: binds the tested ZIP plan to its input fingerprint.

Inspect the plan without writing to GitHub:

```powershell
.venv\Scripts\python.exe scripts/publish-release.py dist\release-candidate-<version>
```

The Windows ZIP, changed build materials and checksums are normally the only
new uploads. The publisher verifies that linked sources/media remain public
and match their recorded hashes. It never compiles code or regenerates sources.

Commit and push the release source and version tag first, then publish explicitly:

```powershell
.venv\Scripts\python.exe scripts/publish-release.py dist\release-candidate-<version> `
  --publish --test-log .test-tmp\<application-tests>.log `
  --test-log .test-tmp\<weather-tests>.log
```

Use passed regression results appropriate to the current source; unchanged
validated candidates do not need another build merely for publication. A new
ZIP still needs its packaged checks. Changed inputs or artifacts invalidate the
validation receipt. Uploads remain a draft until verified; existing matching
uploads are skipped on retry. A published asset with different contents is never
silently replaced. Repository visibility and older releases are not changed.

The release notes are generated from the download plan so the source links next
to the Windows download point to the actual reused archives. Users still need
only the Windows ZIP to run CRTV Sim.
