# Changelog

## Unreleased

## 0.9.57 — 2026-10-25

- Fix weekly specials restarting a series on recurring channels. Keep episode
  progress separate for each special, including shadow placements that share
  source videos, and seed existing specials from prior active schedules.
- Add independent Recurring lineup / Random shows choices in Settings → Weekly
  programming blocks. Both advance episodes sequentially; initialize existing
  choices from the channel, retain progress across mode changes and housekeeping,
  and preserve locked schedules during ordinary readiness updates.

## 0.9.56 — 2026-10-23

- Add individual video preview links and confirmed Delete copy actions to
  Duplicate Media Review. Show the selected physical file, surviving copies,
  and affected shadows. Protect external media, reject changed confirmations,
  preserve other copies and history, and refresh future schedules through maintenance.

- Treat weekly-special and holiday placeholders as last-resort artwork behind
  applicable real channel graphics. Upgrade old placeholder interstitial renders
  when real art becomes available, and add a focused channel-graphics refresh
  button for files copied directly into the library.

- Add an option to generate channel artwork without automatically supplying the
  station name or requesting a logo. Keep the selected channel and graphic scope;
  custom art direction is sent as written. Named artwork remains the default.

- Show AI graphic progress and provider errors inside Channel Graphics, beside
  the generation controls. Prevent duplicate generation clicks while a request
  is pending; keep cancellation and graphics-list refresh failures clear.

- Queue confirmed channel deletions durably, wait for active work, and retry
  database contention automatically. Show progress and allow cancellation before
  deletion starts. Preserve finished media and other channels' shared feeds;
  reject stale confirmations and wait until the affected channel is not playing.
- Review missing channel folders with Recreate folders and Review deletion
  choices. Keep catalog records while awaiting a decision, stop scans/graphics
  maintenance from silently recreating missing channel roots, and distinguish
  unavailable library drives from individual missing folders.

- Load IPTV channel/daypart choices independently of schedule-conflict checks.
  Avoid scanning historical schedules for conflict status, bound diagnostic
  queries, and show a retryable timeout instead of leaving setup at Loading.
  Refresh status can cancel a stalled options request without discarding edits.

- Avoid holding the database writer while graphics scans walk folders or probe
  video/music. Publish the completed catalog atomically, roll back failed
  publication, and skip channels deleted during discovery. Keep deletion
  previews read-only, explain temporary database contention, and reject channel
  deletion once safe shutdown has begun.

- Restore reliable IPTV channel/daypart assignment controls on Internet Media.
  Load options on page entry, label existing-channel placements, and provide an
  explicit daypart selector for new channels. Reject missing destinations in
  the form and preserve unsaved placements when refreshing available options.

- Show pending review counts in collapsed Library panel headings, including
  duplicate sets, shadows, holiday suggestions, and Library review. Keep counts
  independent of filters and pagination, refresh after actions, and distinguish
  automatic act-break analysis from optional fallback review. Surface external
  source errors, invalid Weather artwork, placeholder graphics, and recording
  issues without waiting for the full Programming Inventory.

- Correct act-break cadence using normal program packages: 1/3/5/7 opportunities
  for 30/60/90/120 minutes. Extend boundary targets to longer programs.
- Detect noisy VHS fades with brightness-context validation; search the safe
  program range for strong cues and jointly choose compatible cuts.
- Preserve manual timestamps, legacy manual overflow, and previous valid cuts
  during failed refreshes. Persist layouts and reject stale workshop saves.

- Detect bounded 2–15-second held-card cues even with music. Prefer them after
  strong natural breaks; use scene transitions without silence before midpoint.
- Require 1.5 seconds for quiet-only fallback breaks. Add still-card and
  scene-transition review filters/evidence while preserving manual overrides
  and usable previous cuts during bounded maintenance reanalysis.

- Add per-feed IPTV presentation: preserve the source, center-crop to fill 4:3,
  or stretch to 4:3. Preview the choice and update live playback without a reconnect;
  account for anamorphic pixels and format changes during the feed.
- Cover IPTV loading and recovery with the channel's scoped interstitial graphic;
  prepare reusable frames in the background and fix gaps in fallback color bars.
- Restore channel number/name OSD after tuning and at startup; suppress mpv's
  controls-visibility messages when changing between local and IPTV playback.
- Move IPTV setup to Internet Media and test unsaved selections before adding
  feeds. Require a matching successful playback test before creating a channel
  or its folders.

- Add HTTP/HTTPS IPTV feeds and M3U selection for scheduled channels, dayparts,
  and weekly specials, with independent setup previews and Dashboard diagnostics.
- Import mixed playlists without rejecting valid feeds because another entry
  uses an unsupported protocol; report the skipped-entry count.
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
