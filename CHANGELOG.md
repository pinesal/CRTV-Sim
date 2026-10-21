# Changelog

## Unreleased

- Add HTTP/HTTPS IPTV feeds and M3U selection for scheduled channels, dayparts,
  and weekly specials, with independent setup previews and Dashboard diagnostics.
- Preserve fixed-special priority and local recurring progress. Shared program
  pools are excluded during live windows; explicit channel programming conflicts
  show continuity instead of silently choosing a source.
- Add live connection/stall recovery, clock-boundary transitions, and a final
  30-second Up Next handoff to Guide-listed local programming.

## 0.9.55 — 2026-10-18

Changes since the previous GitHub release, 0.9.53:

### Act-Break Workshop

- Choose manual commercial breaks in an expandable workshop inside the Act
  Breaks panel, with preview playback, timelines, timestamp entry, five-second
  jumps, and precise Previous Frame / Next Frame controls.
- Scrubbing fetches source frames directly instead of waiting for video encoding.
  Source metadata, nearby frame timings, and loaded playback windows are reused.
- Generate approximately 20-second playback clips only when Play is requested.
  Keep accurate frame boundaries for variable-frame-rate media.
- Retain the existing break cadence, manual override rules and external-media
  read-only protections. Keep corrected fallback entries visible until refresh.

### Library and Dashboard

- Reuse cached Programming Inventory results when returning to the page, show
  refresh status, and retain the last working results while updates load.
- Make the inventory's review count clickable. List the affected files, review
  categories and reasons, with links to the corresponding review tools.
- Add individual shadow-file deletion with confirmation of the exact sidecar.
  Leave original media and other shadow placements untouched.

### Packaging

- Include this changelog in the portable application and publish matching
  third-party source archives and checksums beside the Windows download.
- Check actual source-frame retrieval in the frozen application in addition to
  setup, scheduling, Guide and episode playback, Weather, restart and safe stop.

## 0.9.54 — 2026-10-17 (internal test build)

- Introduced the embedded Act-Break Workshop, inventory caching, clickable
  review list and individual shadow deletion. Not released on GitHub.
- Its slow preview generation is superseded by the direct-frame path in 0.9.55.

## 0.9.53 — 2026-10-15

- Packaged the Windows beta with existing-folder external media sources,
  complete media templates, Weather rendering fixes, remembered per-series
  audio/subtitle selections, reliable generic Up Next narration, quiet-pause
  act-break fallbacks, inventory schedule counts, and cross-midnight Guide data.
- Published verified bundled-tool notices, corresponding sources and checksums.

See [the 0.9.53 release notes](docs/RELEASE_0.9.53.md) for that release's details.
