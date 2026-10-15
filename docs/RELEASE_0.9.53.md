# CRTV Sim 0.9.53 — Public beta candidate

This beta is intended for Windows 10/11, 64-bit. Extract the entire Windows ZIP
and open `CRTV-Sim.exe`. Python, FFmpeg, FFprobe and mpv are included.
Start with [Getting Started](GETTING_STARTED.md) for setup and folder guidance.

## Changes since the earlier private test builds

- Register existing local or network series folders for scheduled channels,
  without moving or modifying the external files. Multiple channel/daypart
  assignments are supported; At Your Pace external integration is deferred.
- Create complete shared and channel-template folder trees on first setup and
  when creating channels. Weather cannot receive programming assignments.
- Render Weather with the bundled FFmpeg 9 and support configured holiday
  overlays. Weather setup remains optional.
- Remember audio and subtitle selections per series across episodes, channel
  changes and restarts. Keep Dashboard channel selections stable during refresh.
- Use valid generic Up Next narration whenever dynamic AI narration is
  unavailable. Preserve working recordings through edits and failed generation,
  and refresh cached videos when narration becomes available.
- Link Act Break review entries to their videos, include series names, and try
  a measured quiet pause before a generic midpoint when fallbacks are enabled.
  Existing eligible analyses refresh in bounded maintenance batches.
- Count upcoming program airings across recurring and non-recurring channels
  in Programming Inventory, with recurring assignment status shown separately.
- Show the next day's scheduled programs in Guide columns that cross midnight.
- Include pinned bundled-tool provenance, licenses, corresponding third-party
  sources, and checksums through the strict public packaging workflow.

## Downloads

Use the Windows ZIP to run CRTV Sim. The two matching source archives contain
the bundled software's corresponding sources and build inputs; neither is
required to run the app. Keep all downloads available together.

- [Windows portable ZIP](https://github.com/pinesal/CRTV-Sim/releases/download/v0.9.53/CRTV-Sim-0.9.53-Windows.zip)
- [Third-party sources](https://github.com/pinesal/CRTV-Sim/releases/download/v0.9.53/CRTV-Sim-0.9.53-ThirdParty-Sources.zip)
- [Third-party build inputs](https://github.com/pinesal/CRTV-Sim/releases/download/v0.9.53/CRTV-Sim-0.9.53-ThirdParty-Build-Inputs.zip)
- [SHA256 checksums](https://github.com/pinesal/CRTV-Sim/releases/download/v0.9.53/CRTV-Sim-0.9.53-SHA256SUMS.txt)

## Updating an existing installation

Use **Shut down safely**, then back up the stopped installation's `user-data`.
Copy the new application files over the existing application directory while
preserving `user-data`, your media library, and personal files. Existing profiles
skip initial setup. See [Distribution](DISTRIBUTION.md) for the full procedure.

Keep the previous application and compatible profile backup together for rollback;
do not open a migrated database with an older executable without restoring its
matching backup. This ZIP does not install anything into a production profile.

## Beta notes and reporting

The application is unsigned, so Windows may show SmartScreen prompts. Very large
libraries can take time to load Programming Inventory. At Your Pace external
sources and Plex browsing are not included in this release.

Report the version, Windows version, steps, timestamp and a relevant log excerpt
through [GitHub Issues](https://github.com/pinesal/CRTV-Sim/issues). Remove private
paths and credentials before posting; a complete database/profile is not normally
needed. Test on a copy or with optional source-changing processing disabled until
you are comfortable with your settings.
