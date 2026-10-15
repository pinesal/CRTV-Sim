# Bundled third-party software

CRTV Sim's original code and documentation remain MIT-licensed (`LICENSE`).
The Windows package contains separate FFmpeg, ffprobe and mpv executables, a
frozen Python runtime, libraries and fonts with their own licenses. The MIT
license does not replace those terms.

Public packaging requires both the media source gate and the frozen-runtime
gate to pass, followed by real media and extracted-application smoke tests.
The reviewed lock includes recovered Rust dependencies, incorporated source
inputs and original notices. Microsoft redistribution uses the separately
pinned retail runtime and a licensed Visual Studio installation. An incomplete
internal candidate is never promoted merely by changing its filename.

## Selected media-tool candidate

The lock at `packaging/windows-media.lock.json` selects MSYS2 UCRT64 packages,
not executables discovered on the build computer's PATH:

| Program | Package version | Declared upstream license |
| --- | --- | --- |
| FFmpeg and ffprobe | 9.0.2-1 | GPL-3.0-or-later |
| mpv | 0.41.0-8 | GPL-2.0-or-later; linked GPLv3 FFmpeg requires compatible GPLv3 distribution |

The candidate keeps libx264, x265, AAC, crop/scale/aspect filters, audio loudness
and silence analysis, scene/black analysis, text/overlay rendering, Windows
mpv IPC and hardware rendering options. It uses native Windows binaries; users
need no MSYS2, Python or media-tool installation. More DLLs make this candidate
larger than the earlier static bundles. No CRTV Sim patches are applied to the
media binaries. Supplier patches and recipes are preserved in the source packages.

[MSYS2 mirrors](https://www.msys2.org/dev/mirrors/) provide binary packages and
matching full-source packages. Each selected binary archive has an approved
SHA-256 and `.PKGINFO`/`.BUILDINFO`. The source archive's `PKGBUILD` hash must
match the binary's `.BUILDINFO`; declared source files and their SHA-256 values
are checked. Archived Git sources are checked at their specified commits/tags;
Git reads the complete selected snapshot, whose hash and commit are retained.
PE imports and delay imports identify packaged DLL dependencies. Unknown DLLs
fail validation rather than being assumed safe because they exist in System32.

`MEDIA-TOOLCHAIN.json`, `BUILD-INVENTORY.json` and `THIRD-PARTY-MANIFEST.json`
map each executable/DLL to hashes, versions, licenses, supplier packages,
source archives and build provenance. Frozen native Python components have a
separate inventory. An unresolved origin or missing notice remains a problem.
These are engineering evidence, not a legal clearance certificate.

## Recovered inputs and verification

- rav1e, libdovi and librsvg use 726 locked crate entries in total. Their source
  ZIPs retain complete `cargo vendor --locked` trees, original registry archives,
  upstream/effective locks and per-file/package checksums. The sole librsvg lock
  adjustment is its original recipe's windows-sys downgrade. No unrelated
  dependency update is accepted. Missing upstream license files are supplemented
  using recorded upstream commits or publisher SPDX declarations and canonical
  terms, with provenance clearly distinguishing these sources.
- Incorporated static/header inputs, including dlfcn, fast_float, xxhash, Boost
  and shader/SPIR-V materials, are recovered at the actual versions in each
  consumer's `.BUILDINFO`. The validator independently derives coverage from
  recipes; removing a source record does not waive the obligation. Ordinary
  compilation tools and normal compiler/Windows System Libraries have separate
  documented exclusions. Original MinGW notices and the selected CRT sources
  remain included. Shipped compiler-runtime DLLs have their own source records.
- The application runtime contract records Windows rendering/audio interfaces,
  explicit bundled fonts, filters and encoders. Unused optional Mesa DLLs are
  excluded because CRTV Sim uses Windows OpenGL/Direct3D interfaces. Shaderc's
  static SPIR-V/glslang inputs are retained. CRTV Sim does not request frei0r
  plugins or index VapourSynth scripts; imported VapourSynth/Python DLLs still
  remain in the verified native closure. Isolated-PATH smoke tests exercise the
  encoders, filters, fonts, IPC and playback actually used by the application.
- Frozen Python libraries are mapped to exact installed package hashes and
  original notices. PyInstaller's search path excludes unrelated applications.
  `VCRUNTIME140.dll` is replaced with the hash-pinned, Authenticode-verified,
  unmodified retail file extracted as data from Microsoft's signed installer;
  the installer is never executed. Its permanent download URL, cabinet and
  member hashes are pinned. A complete licensed Visual Studio
  Community 2026, Professional or Enterprise installation is required for
  this public staging grant; the existing 2022 Build Tools alone are insufficient. The
  official Community distributable-code grant and runtime terms are included.
  See [Microsoft's redistribution list](https://learn.microsoft.com/en-us/visualstudio/releases/2026/redistribution).
  Users acknowledge runtime terms once. These terms apply only to the Microsoft
  component; they do not restrict the MIT application or GPL/LGPL components.
  Distributors must preserve these notices and require agreement to the runtime
  terms. Microsoft proprietary source is neither promised nor fabricated.
- Actual texts resolve supplier shorthand: JBIG's source permits GPLv2 or later;
  uchardet's tri-license and cairo's alternative permit LGPL distribution;
  the media tools use compatible GPLv3 terms. Upstream notices missing from
  binary packages have been recovered from the matching source archives.
  MSYS2 describes its package license metadata as best-effort:
  [MSYS2 licensing](https://www.msys2.org/license/).

Any missing evidence is an explicit public-build failure. `-InternalBuild` labels incomplete
artifacts **INTERNAL**; it does not waive redistribution obligations or turn
source evidence into a certification. Do not clear findings by adding an
unsupported `complete: true` flag.

## License structure and source access

The selected FFmpeg enables GPL and version 3 components, including libx264;
it does not enable `--enable-nonfree`. LGPL-only descriptions of this build
would be incorrect. [FFmpeg legal documentation](https://ffmpeg.org/legal.html)
explains how build features affect licensing. Keep GPL source, scripts, patches
and applicable dependency sources, with copyright notices and license texts.
Dynamic linking alone does not eliminate those obligations.

CRTV Sim starts the tools as separate processes, exchanging ordinary command
arguments and mpv's public JSON IPC. It does not link libavcodec or libmpv into
its executable. That supports retaining MIT for the independent application;
this is an assessment of the actual architecture, not a blanket rule for all IPC.
See the [GNU aggregation/communication FAQ](https://www.gnu.org/licenses/gpl-faq.en.html#MereAggregation)
and [mpv copyright/license](https://github.com/mpv-player/mpv/blob/v0.41.0/Copyright).

For download distribution, use a matching source download alongside
the Windows ZIP, following GPLv3 section 6(d) and the applicable GPL/LGPL terms.
[GPLv3 text](https://www.gnu.org/licenses/gpl-3.0.html) and the included full GPL
texts govern. Do not invent a written source offer or rely on a moving homepage.
After validation, upload all four artifacts together:

```
CRTV-Sim-X.Y.Z-Windows.zip
CRTV-Sim-X.Y.Z-ThirdParty-Sources.zip
CRTV-Sim-X.Y.Z-ThirdParty-Build-Inputs.zip
CRTV-Sim-X.Y.Z-SHA256SUMS.txt
```

The Windows package includes this document and its manifest; users can obtain
the same-version ThirdParty-Sources ZIP from the same GitHub Release as the
binary. Put both source links next to every binary download link. Both ZIPs are
required corresponding-source materials; splitting keeps each ordinary ZIP
below the hosting service's per-file size limit without omitting sources. Users do not
need the source ZIP to run the program. Preserve matching sources and their
availability for the applicable distribution period; do not replace them with
sources for a newer binary. Internal investigation artifacts include `-INTERNAL`
and are distinct from the validated public distribution.

The matching source download contains MSYS2 full-source archives, original
build records, Git snapshot proofs, complete Rust vendor sources, incorporated
inputs, Python package files/notices and release recipes. Its README explains
offline Cargo use and the precise lock transformation. Rebuild the supplier
packages in MSYS2 UCRT64 using `makepkg-mingw`, each
preserved PKGBUILD and the dependencies recorded in its `.BUILDINFO`:
[MSYS2 build instructions](https://www.msys2.org/wiki/Creating-Packages/).
Exact byte-for-byte rebuilding has not been demonstrated.

MIT/BSD libraries generally require retaining their copyright/license notices;
Apache components also retain applicable NOTICE text and license terms.
LGPL libraries require their corresponding sources and the applicable
replacement/relinking rights; their unmodified DLLs remain separate here.
certifi's MPL-2.0 bundle and package files are included in the source candidate.
Python's PSF license and component notices are included where obtained.
PyInstaller has a [bootloader exception](https://pyinstaller.org/en/stable/license.html)
allowing independently licensed applications; its included COPYING text governs.
Unmodified DejaVu fonts retain the Bitstream/DejaVu notice in assets/fonts.

Codec patent and DVD decryption/circumvention questions are separate from
copyright source obligations. This candidate includes libdvdcss through the
supplier's DVD stack. Assess applicable distribution regions and intended uses;
providing source does not grant patent or anti-circumvention permissions. Seek
qualified advice when a legal conclusion is required. Signing and an independent
clean Windows account/machine test remain release choices/checks.

## Earlier private releases

The old Gyan FFmpeg/ffprobe `2025-02-02-git-957eb2323a` and mpv CI build
`41f6a645068483470267271e1d09966ca3b9f413` are not used by this new candidate.
Their core source can be identified, but complete static dependency sources and
exact build inputs were not recovered. The mpv recipe uses moving dependency
references. Reconstructing those historical environments was not chosen.

The Windows downloads from `v0.9.47-test.20261011` and
`v0.9.48-external-media-test` used those older toolchains without matching source
ZIPs. Release 0.9.53 replaces those historical Windows downloads with a verified
paired distribution. A prerelease flag is not a privacy boundary: all remaining
published downloads become accessible when repository visibility changes.
Example-channel/music packs are separate media downloads with their own source
and rights notes; keep those notices with the media.
