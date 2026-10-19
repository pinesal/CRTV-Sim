# CRTV Sim 0.9.54 — Portable test build

## Changes since 0.9.53

- Add an embedded Act-Break Workshop with preview playback, full-program and
  zoomed timelines, precise source-frame stepping, and manual break placement.
  Keep the existing break cadence and support external read-only media.
- Cache Programming Inventory results so returning to the page reuses the last
  snapshot while updates refresh in the background.
- Make the inventory's review count clickable, showing affected files,
  categories and reasons with links to the appropriate review tools.
- Add individual shadow-file deletion, with confirmation of the exact sidecar
  being removed. Original media and other shadow placements remain untouched.

## Testing and updating

This unpacked Windows test build includes Python, FFmpeg, FFprobe and mpv.
Open `CRTV-Sim.exe` to start it. No separate runtime installation is needed.

To update an existing installation, use **Shut down safely** first and back up
its stopped `user-data`. Copy the new application files over the application
directory, preserving `user-data`, your media library and personal files.
Existing profiles skip initial setup.

Packaging does not install this build into your production folder. Keep the
previous application and its compatible profile backup together for rollback.
This internal test package is not a public release or a GitHub upload.
