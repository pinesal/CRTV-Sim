# CRTV Sim 0.9.55 — Library and Act-Break Workshop update

**To use CRTV Sim, download only `CRTV-Sim-0.9.55-Windows.zip`.**
Extract the complete ZIP and open `CRTV-Sim.exe`. Python, FFmpeg, FFprobe and mpv
are included. The third-party source archives, build inputs, checksums and
GitHub's automatic source-code downloads are optional; none is needed to run
the program.

## Changes since 0.9.53

- **Embedded Act-Break Workshop:** Find manual commercial breaks using preview
  playback, full-program and loaded-preview timelines, timestamp entry,
  five-second jumps and accurate frame stepping. Keep the established break
  cadence and existing manual override rules.
- **Responsive frame browsing:** Scrubbing fetches source frames directly.
  Nearby seeks reuse metadata and frame timings; playback clips of about 20
  seconds load only when requested. Variable-frame-rate boundaries remain accurate.
- **Faster inventory revisits:** Reuse the previous Programming Inventory
  snapshot while updated results load, with visible refresh status.
- **Find review items:** Click the inventory's review count for affected files,
  categories, reasons and links to their review tools.
- **Delete individual shadows:** Confirm and remove one shadow sidecar without
  deleting its source media or the other placements created in a folder batch.

The full [changelog](../CHANGELOG.md) is also included in the application folder.
See [Getting Started](GETTING_STARTED.md) for setup and folder guidance.

## Downloads

**Required to run the program**

- [Windows portable ZIP](https://github.com/pinesal/CRTV-Sim/releases/download/v0.9.55/CRTV-Sim-0.9.55-Windows.zip)

**Optional media extras**

- [Classic Comedy example channel](https://github.com/pinesal/CRTV-Sim/releases/download/v0.9.55/CRTV-Sim-Example-Channel-Classic-Comedy.zip) — example programming, graphics and music, with installation instructions.
- [Background music pack](https://github.com/pinesal/CRTV-Sim/releases/download/v0.9.55/CRTV-Sim-Optional-Music-Pack.zip) — music for the Guide, Weather and channel template.

**Optional developer materials and download verification**

You do **not** need these downloads to use CRTV Sim. Matching sources and build
inputs are provided for bundled-software license compliance and development.

- [Third-party sources](https://github.com/pinesal/CRTV-Sim/releases/download/v0.9.55/CRTV-Sim-0.9.55-ThirdParty-Sources.zip)
- [Third-party build inputs](https://github.com/pinesal/CRTV-Sim/releases/download/v0.9.55/CRTV-Sim-0.9.55-ThirdParty-Build-Inputs.zip)
- [SHA256 checksums](https://github.com/pinesal/CRTV-Sim/releases/download/v0.9.55/CRTV-Sim-0.9.55-SHA256SUMS.txt)

## Updating an existing installation

Use **Shut down safely**, then back up the stopped installation's `user-data`.
Copy the new application files over the application directory, preserving
`user-data`, your media library and personal files. Existing profiles skip setup.
See [Distribution](DISTRIBUTION.md) for the full procedure.

Keep the previous application with its compatible profile backup for rollback.
Packaging does not modify your production installation.

## Beta notes

This beta targets Windows 10/11, 64-bit. The application is unsigned, so Windows
may show SmartScreen prompts. The first inventory calculation can still take
time with a large library; cached revisits reuse the last snapshot. Preview
speed depends on the source format, hardware and storage connection. At Your
Pace external integration and Plex browsing remain outside this release.

Report the version, Windows version, steps and relevant log excerpt through
[GitHub Issues](https://github.com/pinesal/CRTV-Sim/issues), removing private paths
and credentials first.
