# Bundled software release checklist

CRTV Sim remains MIT; bundled software retains separate terms. This checklist
is an engineering release record, not legal certification. See THIRD-PARTY.md
for the evidence and component license boundaries.

## Build and validate

1. Use the reviewed `packaging/windows-media.lock.json`,
   `packaging/microsoft-runtime.lock.json`, a licensed Visual Studio installation,
   pinned Python runtime
   and `requirements.lock` / `requirements-build.lock`. Never source public media
   executables from PATH. PyInstaller's DLL search must be isolated too.
2. Run `.\scripts\build-portable-windows.ps1` from the project root. The default
   public workflow must stop on a missing package, hash mismatch, recipe mismatch,
   unresolved corresponding-source input, native-library origin or missing notice.
   `-SkipArchive` does not bypass public evidence checks.
3. Check every bundled executable/DLL and Python extension in the manifests.
   Confirm source package hashes, `.BUILDINFO` recipe correspondence, dependency
   closure, source checksums, Git pins and actual license texts. Account for
   static/header, generated, runtime-loaded and fetched inputs, not just PE imports.
4. Confirm no `--enable-nonfree` configuration, no private user-data/media/keys,
   no unapproved output files, and no silently missing notices. Refuse to build
   over a directory containing user-data.
5. Run `python scripts/check-media-tools.py <candidate>` for real codecs, filters,
   render operations, Windows named-pipe IPC and seeking. Run the unittest suite.
6. Run `python scripts/smoke-distribution.py --archive <Windows.zip> --root
   .test-tmp/<new-isolated-name>` to extract and launch the real archive through
   setup, Dashboard, restart and safe shutdown. Use a clean Windows account/
   machine separately to check optional renderers/plugins and installed-tool
   independence. Automated static checks do not prove legal compliance.

## Publish only after the evidence is complete

- Require zero problems in THIRD-PARTY-MANIFEST.json and the source manifest.
  Retain full Rust vendor inputs, incorporated-source coverage, original notices
  and Microsoft grant evidence; never waive missing materials with a flag.
- Upload the Windows ZIP and checksums with either newly verified matching
  source archives or hash-verified links to already published matching archives.
  Reuse is bound to the actual media/runtime/Python inputs, not app version labels.
  Supply changed build recipes/notices in the small current Build-Materials ZIP.
  Put the actual source links next to the binary link in release notes and any
  download page, and retain all referenced downloads for the required period.
  See [Release workflow](RELEASE_WORKFLOW.md). Never reuse mismatched or missing sources.
- Keep complete upstream licenses, notices, recipes, patches and source-to-binary
  mapping; document modifications (none to these media executables by CRTV Sim).
- Assess codec patents, libdvdcss/anti-circumvention and Microsoft redistribution
  permissions for intended distribution regions. Obtain legal advice if needed.
- Before making the repository public, verify that the superseded Windows assets
  in v0.9.47-test.20261011 and v0.9.48-external-media-test have been retired in
  favor of the verified 0.9.53 distribution. Their original binaries lack matching
  source ZIPs. Never remove/replace releases without owner authorization.
- Review example-channel/media-pack rights separately. Signing and public hosting
  are owner decisions; they do not repair a source gap.

## Internal testing

`-InternalBuild` keeps development convenient, using the same pinned binaries.
It adds INTERNAL to archive/checksum names and a visible warning in the package.
It collects available source evidence without declaring it complete. It is not
permission to distribute an incomplete copyleft bundle. No upload is automated.
