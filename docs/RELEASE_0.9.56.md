# CRTV Sim 0.9.56 — IPTV, act-break detection and library management

**To use CRTV Sim, download only `CRTV-Sim-0.9.56-Windows.zip`.**
Extract the complete ZIP and open `CRTV-Sim.exe`. Python, FFmpeg, FFprobe and mpv
are included. Third-party sources, build materials, checksums and GitHub's
automatic source-code downloads are optional; none is required to run it.

## Changes since 0.9.55

- **IPTV live feeds:** Add supported HTTP/HTTPS HLS or MPEG-TS feeds, including
  selections from M3U playlists, on the Internet Media page. Test a feed before
  creating its channel. Assign it to existing or new scheduled channels,
  dayparts or weekly specials. Authentication and At Your Pace integration are
  outside this release.
- **Live-feed presentation:** Preserve the source picture, crop to fill 4:3 or
  stretch to 4:3. Use channel interstitial artwork while connecting or recovering.
  Restore the channel number/name display when tuning.
- **Live scheduling:** Preserve weekly-special priority and existing episode
  progress. Shared programs stay out of live windows; conflicting explicit
  channel programs are reported rather than silently chosen. Show an eligible
  Up Next screen before returning to Guide-listed local programming.
- **Improved commercial-break detection:** Use the normal program package for
  the established 1/3/5/7 opportunities in 30/60/90/120-minute packages. Add
  brightness-validated noisy VHS fades and held-card cues, then try quiet pauses
  of at least 1.5 seconds and scene transitions before midpoint fallback. Choose
  compatible cuts across the program while preserving manual timestamps.
- **Review counts in panel headings:** See pending actions without opening every
  Library panel, including duplicate sets, shadow files and holiday suggestions.
- **More reliable IPTV setup and database access:** Load channel/daypart choices
  independently of schedule-conflict checks. Keep graphics discovery and probes
  outside long database write transactions.
- **Queued channel deletion:** Confirm once, see progress and let CRTV Sim wait
  for active work and retry database contention. Cancel before deletion starts.
  Review missing channel folders with Recreate folders or Review deletion.
- **Clearer AI graphics controls:** Show generation progress and provider errors
  beside the buttons, prevent duplicate requests, and optionally omit the station
  name and logo from the artwork prompt.
- **Real artwork takes priority:** Applicable real channel graphics override
  holiday and weekly-special placeholders. Refresh channel graphics after copying
  artwork into the library; old placeholder interstitial renders update as needed.
- **Duplicate-file tools:** Click each copy's path to view its video and use
  Delete copy to remove one confirmed managed file. See surviving copies and
  affected shadows first. External media, backups and schedule history are
  protected. Duplicate deletion requires TV stopped and maintenance idle; a busy
  response does not queue the deletion, so retry after maintenance finishes.

Existing media is queued for updated act-break analysis through normal bounded
maintenance. Previous valid cuts remain usable until refresh succeeds, manual
choices remain, and locked/historical schedules are preserved.

The full [changelog](../CHANGELOG.md) is included in the application folder.
See [Getting Started](GETTING_STARTED.md) for setup and folder guidance.

## Downloads

**Required to run the program**

- [Windows portable ZIP](https://github.com/pinesal/CRTV-Sim/releases/download/v0.9.56/CRTV-Sim-0.9.56-Windows.zip)

**Optional media extras, reused unchanged from 0.9.55**

- [Classic Comedy example channel](https://github.com/pinesal/CRTV-Sim/releases/download/v0.9.55/CRTV-Sim-Example-Channel-Classic-Comedy.zip)
- [Background music pack](https://github.com/pinesal/CRTV-Sim/releases/download/v0.9.55/CRTV-Sim-Optional-Music-Pack.zip)

**Optional developer materials**

The release download list supplies matching third-party source archives and
current build materials. Unchanged sources are linked from their original
release instead of uploaded again. You do not need them to use CRTV Sim.

## Updating an existing installation

Use **Shut down safely**, then back up the stopped installation's `user-data`.
Copy the new application files over the application directory, preserving
`user-data`, your media library and personal files. Existing profiles skip setup.
See [Distribution](DISTRIBUTION.md) for the full procedure.

Keep the previous application and its compatible profile backup for rollback.
Packaging and publication do not modify your production installation.

## Beta notes

This beta targets Windows 10/11, 64-bit. The application is unsigned, so Windows
may show SmartScreen prompts. Internet feeds depend on upstream availability,
network buffering and provider changes. Detection suggests commercial boundaries;
internal sketch fades or visually similar cards can still need manual review.

Report the version, Windows version, reproduction steps and a relevant log excerpt
through [GitHub Issues](https://github.com/pinesal/CRTV-Sim/issues), removing private
paths and credentials first.
