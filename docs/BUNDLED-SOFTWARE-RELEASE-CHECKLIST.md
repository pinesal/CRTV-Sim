# Bundled software release checklist

CRTV Sim ships FFmpeg, ffprobe, and mpv in the Windows portable package. These are separate third-party programs with their own licenses. CRTV Sim's MIT license does not cover them.

## Before publishing a portable package

- Confirm the exact executable and DLL hashes in `BUILD-INVENTORY.json` match the release candidate.
- Include each upstream copyright notice, license text, and build configuration in `licenses/`.
- Include the exact corresponding source for each binary and every statically linked library, plus the scripts and configuration needed to rebuild it. A source repository link, version list, or configure line alone is not the complete source package.
- Verify that the source revisions correspond to the packaged binaries. Record source archive hashes and build instructions in the release source archive.
- Check upstream license obligations and codec patent rules for the intended distribution regions. Ask a qualified lawyer if a legal conclusion is needed.
- Publish the binary ZIP and corresponding source ZIP together. Keep the exact source archive available for the full period required by the applicable licenses.
- Test the portable ZIP on a clean Windows account and verify its SHA-256 checksum.

## Current status

The packaging script is set up to copy FFmpeg's upstream build README and license, mpv's copyright and GPL notice, and the Vulkan loader license into future packages. It records hashes for bundled executables and DLLs. `THIRD-PARTY.md` identifies the known upstream revisions and build details. The exact currently published candidate has not been rebuilt with these packaging changes.

The matching source package is not complete yet. In particular, the current FFmpeg binary statically links many external libraries, and the exact resolved dependency revisions for the mpv CI binary have not been recovered. Do not publish the current portable ZIP as a public release until this source gap is closed or the bundled binaries are replaced with builds whose complete source inputs can be pinned and distributed.

## Current binaries to resolve

- FFmpeg and ffprobe: Gyan essentials build `2025-02-02-git-957eb2323a`, core commit `957eb2323a`; its build README is included in the package.
- mpv: official Windows CI build from mpv commit `41f6a645068483470267271e1d09966ca3b9f413`; the executable reports embedded FFmpeg revision `f853d12`. The CI recipe at that commit uses some moving dependency references, so the recipe alone does not identify every exact source input used for this binary.
- Vulkan loader: `vulkan-1.dll` is separately included and its license notice is bundled.

This checklist is a release engineering record, not legal advice or a claim that all distribution obligations have been satisfied.
