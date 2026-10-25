# CRTV Sim 0.9.57 — Weekly-special episode progression fix

**To use CRTV Sim, download only `CRTV-Sim-0.9.57-Windows.zip`.**
Extract the complete ZIP and open `CRTV-Sim.exe`. Python, FFmpeg, FFprobe and mpv
are included. Third-party sources, build materials, checksums and GitHub's
automatic source-code downloads are optional; none is required to run it.

## Changes since 0.9.56

- **Fix repeating weekly-special episodes:** On recurring channels, a special
  could restart a series at episode one each week. Each special now keeps its
  own episode progress, separately from the regular channel and other specials.
  This also applies when shadows reuse the same physical video files.
- **Independent schedule modes:** Under **Settings → Weekly programming blocks**,
  choose **Recurring lineup** to retain established show slots, or **Random
  shows** to vary show placement. Both advance each series' episodes in order.
  Changing mode preserves episode progress.
- **Preserve existing setups:** Existing specials initially retain their
  channel's current mode. Their choice then remains independent of subsequent
  changes to the parent channel. Retained active schedule history seeds each
  special's last episode and established slots.
- **Protect progress:** Rebuilding a date or rejecting an invalid schedule does
  not consume another episode. Progress survives restarts, midnight boundaries
  and normal schedule-history cleanup. Locked and historical schedules retain
  their existing protections.

Series advance according to their schedules, whether watched or not. After a
complete run, a series can begin another pass through its episodes.

The full [changelog](../CHANGELOG.md) is included in the application folder.
See [Getting Started](GETTING_STARTED.md) for the weekly settings and update guide.

## Downloads

**Required to run the program**

- [Windows portable ZIP](https://github.com/pinesal/CRTV-Sim/releases/download/v0.9.57/CRTV-Sim-0.9.57-Windows.zip)

**Optional media extras, reused unchanged from 0.9.55**

- [Classic Comedy example channel](https://github.com/pinesal/CRTV-Sim/releases/download/v0.9.55/CRTV-Sim-Example-Channel-Classic-Comedy.zip)
- [Background music pack](https://github.com/pinesal/CRTV-Sim/releases/download/v0.9.55/CRTV-Sim-Optional-Music-Pack.zip)

**Optional developer materials**

The release download list links matching third-party sources and supplies
current build materials and checksums. Unchanged sources stay at their original
release instead of being uploaded again. You do not need them to use CRTV Sim.

## Updating an existing installation

Use **Shut down safely**, then back up the stopped installation's `user-data`.
Copy the new application files over the application directory, preserving
`user-data`, your media library and personal files. Existing profiles skip setup.
Normal readiness processing refreshes affected eligible unlocked schedules.
Changing a special's mode can also use **Save & update schedules** in Settings.
Episodes scheduled in protected historical entries are not rewritten.

Keep the previous application and its compatible profile backup for rollback.
See [Distribution](DISTRIBUTION.md) for the complete update procedure. Packaging
and publication do not modify your production installation.

## Beta notes

This beta targets Windows 10/11, 64-bit. The application is unsigned, so Windows
may show SmartScreen prompts. Progress recovery uses the retained schedule
history; it cannot reconstruct episodes whose records were already removed
before this fix. Report issues through [GitHub Issues](https://github.com/pinesal/CRTV-Sim/issues),
removing private paths and credentials from logs first.
