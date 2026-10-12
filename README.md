# CRTV Sim

Previously named RetroTV. The public application and future Windows downloads
use **CRTV Sim**; the `retrotv` Python package and command remain compatible.
`crtvsim` is also available after installing the updated package. Existing
`user-data`, media paths, databases and saved provider keys do not need renaming.

CRTV Sim is a simulated linear television project for Windows. Phases 1–7.5
provide a portable SQLite media library, folder classification, integrity and
aspect analysis, corrected-media replacement with verified backups, persistent
date-aware schedules, complete blocks, join-in-progress playback, a rendered TV
Guide channel, and a unified Windows application dashboard with automation.

## Development

CRTV Sim is developed by its maintainer with assistance from OpenAI Codex, an
AI coding tool. The project direction, requirements, and release decisions are
human-led.

## Why CRTV Sim?

CRTV Sim builds always-on TV channels directly from your video files. It does
not require Plex or another media server: put media in channel and daypart
folders, and CRTV Sim scans it, builds schedules and a guide, and tunes into
whatever program is already in progress. It is designed for collections gathered
from different sources and with different formats and quality levels. Optional
video and audio preparation, automatic aspect analysis, and review tools help
bring that library into a consistent presentation, while the deeper controls
stay available when you want them.

New to CRTV Sim? Start with the plain-language
[Getting Started guide](docs/GETTING_STARTED.md). The rest of this README and the
other project documents describe the detailed technical design and operation.

## Requirements

- Python 3.11 or newer
- FFmpeg and FFprobe available on `PATH`, or configured explicitly
- mpv for production playback; FFplay is available as a Windows fallback
- Windows is the supported development and distribution platform

CRTV Sim itself has no third-party Python dependencies. On Windows, the bundled
RetroWeather companion installs the small `tzdata` package so IANA forecast
timezones work inside the shared virtual environment.

## Companion utilities

- [CRTV Sim Commercial Prep](commercial-prep/README.md) detects the black
  transitions in commercial compilations, previews and creates individual
  clips, then uses OpenAI GPT Transcribe to preview and apply descriptive
  filenames.
- **CRTV Sim DVD Ripper** prepares owned movie and TV-series DVDs for the media
  library. Its TV Series mode tracks episode names and numbering across discs.

Both are standalone Windows desktop applications with their own environments;
their optional media dependencies do not change the main CRTV Sim installation.

## Windows setup

From PowerShell:

```powershell
.\scripts\setup-windows.ps1
```

The setup script creates `.venv`, installs both CRTV Sim and RetroWeather in
editable mode, installs RetroWeather's Windows timezone data, initializes the
companion, copies the Windows example profile to
`config\development.windows.local.toml`, and initializes the database.

Review this setting before scanning:

```toml
[storage_roots]
media = "F:/Videos/media"
```

## Commands

If the virtual environment is active:

```powershell
retrotv --config config\development.windows.local.toml init-db
retrotv --config config\development.windows.local.toml scan --limit 10
retrotv --config config\development.windows.local.toml stats
retrotv --config config\development.windows.local.toml weather-status
retrotv --config config\development.windows.local.toml review
retrotv --config config\development.windows.local.toml analyze-normalization --limit 10
retrotv --config config\development.windows.local.toml preview-normalization 2
retrotv --config config\development.windows.local.toml replace-with-backup 2 --confirm-replace
retrotv --config config\development.windows.local.toml generate-schedule --channel fox
retrotv --config config\development.windows.local.toml show-schedule --channel fox
retrotv --config config\development.windows.local.toml play-channel --channel fox --dry-run
retrotv --config config\development.windows.local.toml play-channel --channel fox
retrotv --config config\development.windows.local.toml serve
```

Without installation:

```powershell
$env:PYTHONPATH = 'src'
python -m retrotv --config config\development.windows.example.toml stats
```

The library interface defaults to:

```text
http://127.0.0.1:8765
```

`weather-status` reads and validates RetroWeather's atomically published
`current.json` and active channel manifest. It reports the generation, age,
stale state, and resolved video path without opening the media database or
sending the generated MP4 through scanning and normalization. Use `--root` to
inspect a publication outside the default
`RetroWeather/synthetic-channels/weather` folder.

The Weather synthetic channel can now be tuned directly without creating a
normal schedule or importing its generated video into the media library:

```powershell
.\.venv\Scripts\retrotv.exe `
  --config .\config\development.windows.local.toml `
  play-channel --channel weather --dry-run
```

Remove `--dry-run` to launch the configured player. CRTV Sim begins at
`current epoch seconds modulo loop duration`, resolves the active manifest
segment, and loops continuously. It adopts a newly validated publication only
at a segment or loop boundary.

When enabled, RetroWeather is also available as a normal tuner destination and
defaults to channel 42. Its publication root, display name, channel number,
stale-warning threshold, companion home, host, and port can be changed in
CRTV Sim Settings. Host and port are the single source of truth for both process
launch and the dashboard link; the Windows default is `127.0.0.1:8775`.
The CRTV Sim Dashboard reports its readiness, active segment, generation, update
age, last-known-good state, and stale or invalid-publication warnings. Guide
listings are generated directly from the repeated synthetic manifest, including
future entries that cross the end of the two-minute loop. These integrations do
not add the generated Weather MP4 to the media database or linear scheduler.

Launching the normal CRTV Sim application now supervises RetroWeather. CRTV Sim
first checks the configured health endpoint and reuses an independently running
companion without taking ownership. Otherwise it launches RetroWeather silently
from the same virtual environment, requests Fetch + render when weather or the
publication is missing/stale, and keeps the previous valid channel immediately
playable during that work. An unexpectedly exited owned process is restarted
with bounded exponential backoff. The Dashboard reports starting, running,
updating, stale, or unavailable state and provides a restart action. On exit,
CRTV Sim cleanly stops only the companion process it launched; an independent
RetroWeather process is left running.

While the RetroWeather dashboard server is running, its background scheduler
fetches weather every 15 minutes and renders a validated channel every 30
minutes. Material changes to displayed conditions can trigger an earlier
render. Automatic jobs share the dashboard's overlap guard, retry temporary
failures with bounded backoff, preserve the active channel on failure, and
clean up older publications. The RetroWeather dashboard shows the automation
state and next fetch/render times. These defaults live in the `refresh` section
of `RetroWeather/config/settings.json`.

RetroWeather is designed to fail back to its most recent usable output instead
of producing a blank channel. Cached weather snapshots and the active channel
pointer are replaced atomically; a failed fetch or render leaves the previous
validated publication active and reports its data age and stale state. On
restart, abandoned render locks and partial build directories are recovered,
while a lock owned by a running renderer is left alone.

The RetroWeather dashboard also includes a live asset library for still-image
backgrounds, motion media, music, and holiday artwork. Supported files appear
without restarting the server and show their role, validity, dimensions or
duration, and rendering eligibility. The media-folder shortcut opens the
complete drag-and-drop folder structure. Weather music now follows condition
music → default music → AAC silence, with deterministic selection, loudness
normalization, volume control, crossfaded looping, whole-program fades, and the
active soundtrack shown in the dashboard.

In CRTV Sim's **Library & Schedules → Weather Graphics**, upload artwork or
generate it with the saved OpenAI key and optional art direction. Controls cover
day and night for every supported condition, alert artwork, and transparent
border layers for every holiday configured in Settings. Generated images are
resized to 640×480 without cropping. Regenerate replaces that type's generated
image; additional uploads remain available for the existing rotation. New
artwork is used on the next Weather render.

CRTV Sim shares holiday names, inclusive date ranges, enabled flags, and priorities
through `RetroWeather/config/crtvsim-holidays.json` in the configured companion
home. RetroWeather reads this calendar on each render, including custom and
cross-year holidays. If holidays overlap, the highest priority wins, with the
slug breaking ties. Its own settings remain separate. Standalone RetroWeather
without a shared calendar retains its original Halloween and Christmas dates.

The channel preview includes an expandable screen-theme inspector. It lists
the condition family and day/night variant requested by every published screen
along with the selected artwork or built-in fallback, making condition-folder
coverage visible without opening the channel manifest.

The condition-artwork renderer accepts validated still and motion backgrounds.
Motion clips are looped and trimmed per forecast screen, while invalid files
are excluded from selection. Artwork remains bright outside the localized
forecast-data panel, and heat/freezing badges appear in the solid title bar so
they do not obscure either the forecast or user artwork.

## Scanner behavior

The scanner:

1. Stores paths relative to the configured logical media root.
2. Infers channel, daypart, type, holiday, and shared-pool eligibility.
3. Preserves the original filename.
4. Creates a quick content fingerprint by default.
5. Extracts technical metadata with FFprobe.
6. Decodes samples near the beginning and end with FFmpeg.
7. Enables high-confidence, valid assets for later scheduling.
8. Sends ambiguous or invalid assets to the review queue.

It never renames, moves, repairs, or quarantines source media.

The media root includes a reserved `_channel_template` folder containing the
complete supported channel hierarchy. Copy that folder, rename the copy to a
configured channel slug, and add media to the appropriate folders. Rename
`_series_name` folders to the actual series names and `_weekly_block_name` to a
weekly collection configured under `[seasonal.windows]`.

The scanner skips everything beneath `_channel_template`, including video
files accidentally left there. Consequently, the scheduler and Guide never
treat the template as a real channel.

Every channel supports both convenience scopes:

```text
<channel>/daytime/<media-type>/  -> morning + afternoon + evening
<channel>/all-day/<media-type>/  -> morning + afternoon + evening + night
```

Use `nickelodeon/daytime/bumpers` for general Nickelodeon presentation that
should stop when Nick at Nite begins. Keep Nick at Nite presentation in
`nickelodeon/night/bumpers`. Reserve `nickelodeon/all-day/bumpers` for material
that genuinely makes sense during both identities.

After moving files between these folders, run a normal scan. The scanner
recognizes moved files by fingerprint, keeps their asset IDs, and replaces their
daypart eligibility. If a file was already sitting in a previously unsupported
channel `daytime` folder, run one forced metadata-light rescan:

```powershell
retrotv --config config\development.windows.local.toml scan --force --skip-samples
```

For a stronger but slower whole-file fingerprint:

```powershell
retrotv --config config\development.windows.local.toml scan --fingerprint full --force
```

To index without decode samples during troubleshooting:

```powershell
retrotv --config config\development.windows.local.toml scan --skip-samples
```

## Phase 2 normalization

Analysis samples several points throughout each file. It automatically handles:

- Native 4:3 video
- 4:3 video pillarboxed inside a wider encoded frame

A borderless 16:9 frame is conservatively marked for review because it could
be genuine widescreen or 4:3 stretched to 16:9. Use the browser preview or:

```powershell
retrotv --config config\development.windows.local.toml analyze-normalization --asset 2
retrotv --config config\development.windows.local.toml set-normalization 2 stretched_4_3
retrotv --config config\development.windows.local.toml preview-normalization 2
```

Available manual modes are `auto`, `native_4_3`, `pillarboxed_4_3`,
`stretched_4_3`, `preserve_frame`, and `custom`. A custom CLI crop uses
`width:height:x:y`:

```powershell
retrotv --config config\development.windows.local.toml set-normalization 2 custom --crop 640:480:105:0
```

After the analysis or override looks right, use **Convert approved media** in
the Dashboard or `replace-with-backup` for an individual asset. CRTV Sim creates
the correction staging file internally; staging is not a permanent playback
copy.

The policy is `replace_with_backup`. A correction is first staged under
`data/normalized`; it must pass FFprobe, canvas, duration-drift, beginning/end
decode, and midpoint-seek checks before it can replace a working-library file.
The configured 640×480 canvas is a maximum rather than a mandatory upscale:
smaller sources retain an appropriate smaller 4:3 canvas, while larger sources
are downscaled to at most 640×480. Set `allow_upscale = true` under
`[normalization]` only when a fixed 640×480 output is specifically required.
CRF encoding is constrained by a source-derived H.264 bitrate ceiling because
noisy archival footage can otherwise become much larger than a low-bitrate
original. `maximum_output_size_ratio = 1.10` limits a validated correction to
110% of its source file size, and `audio_bitrate_kbps = 128` controls its AAC
audio target. A staging file exceeding the size limit fails validation and
cannot replace the working copy.
The original is first preserved outside the scanned media root under the
configured `originals_backup` root. It is a recovery copy rather than a permanent
archive: cleanup removes tracked normalization backups after 30 days by default,
but only after the current working file still matches its validated conversion
history. The retention period and automatic deletion are configurable. The final
promotion operation will require explicit confirmation. Encoding a full library
remains a separate operation because it can consume substantial time and disk
space.

Promote one validated correction into the working library:

```powershell
retrotv --config config\development.windows.local.toml replace-with-backup 1306 --confirm-replace
```

This command may create the corrected staging file if needed. It refuses to
continue unless the original backup and corrected copy validate. The backup
keeps the original relative path under `originals_backup`; an existing
different backup is never overwritten.

The historical `normalize` command and standalone **Create correction** action
were retired. The player does not use staged corrections under the normal
replace-and-backup policy. Validated staging is installed, recorded as the
working copy, and removed.

The browser interface supports per-asset analysis and a visual Aspect Review
workflow. Maintenance automatically generates a display-correct original thumbnail
for every open review. Selecting a correction changes only the browser draft;
**Preview correction** renders that exact crop, aspect correction, and output canvas
beside the original without changing the stored override. **Compare 3 frames**
renders matched early, middle, and late frames for uncertain material. Approval is
enabled only for the current rendered recipe and is rejected if the source file or
draft changed. Approved rows disappear without reloading the list, and J/K, arrow
keys, P, and A provide navigation, preview, and approval shortcuts.

Preview JPEGs are discarded immediately when an aspect review is approved. They
remain cached while the review is open; abandoned previews for reviews that are no
longer open expire after 7 days by default.

The Library page groups the media table, seasonal rules, linear schedule, and
complete-block cue editor into collapsible sections. Individual section state
and the Expand all / Collapse all controls are remembered in browser storage.
Its review selector separates library/classification review from aspect-ratio
review; the latter filters normalization profiles whose geometry is ambiguous.
From **Aspect review**, **Apply shown correction to folder** copies the exact
previewed correction and crop rectangle to every still-pending video in the same
folder. It is non-recursive, so season subfolders remain independent. CRTV Sim
validates every target first and applies none of them if the crop is incompatible
with any file.

### Audio loudness normalization

Audio loudness is measured separately from aspect-ratio analysis using FFmpeg's
EBU-style `loudnorm` measurements. The default target is `-18 LUFS` with a
`-1.5 dBTP` true-peak ceiling and a `1.5 LU` tolerance. Files already within
that range are skipped automatically. Silent or unreliable measurements enter
the Library's **Audio review** queue, where they can be retried or skipped.

Analyze every pending file without changing media:

```powershell
.\.venv\Scripts\retrotv.exe `
  --config .\config\development.windows.local.toml `
  analyze-audio
```

This decodes each complete audio stream and can take several hours for a large
library. While television playback is stopped, the automatic readiness pipeline
continues through all pending audio analyses and then normalizes measurable,
approved corrections. Files whose audio cannot be measured reliably remain in
the review queue and are not changed.

Normalize every analyzed file outside the configured tolerance:

```powershell
.\.venv\Scripts\retrotv.exe `
  --config .\config\development.windows.local.toml `
  normalize-audio --confirm-replace
```

Stop television playback before replacement. CRTV Sim stream-copies the video,
encodes only the audio, validates duration and video geometry/codec, and then
atomically replaces the working file. The immediate pre-audio version is kept
under `originals_backup/_audio_pre_normalization/` with its original relative
path. Existing aspect-normalization backups remain separate and are managed as
independent recovery copies.
Changed or manually restored files automatically return to loudness analysis.
The Windows Dashboard exposes both operations, progress, pending totals, current
file, and recent errors.

## Phase 3 linear scheduling

Generate one local calendar day for FOX:

```powershell
retrotv --config config\development.windows.local.toml generate-schedule --channel fox --date 2026-07-27
```

Generate the seven-day scheduling horizon:

```powershell
retrotv --config config\development.windows.local.toml generate-schedule --channel fox --date 2026-07-27 --days 7
```

Inspect or independently revalidate a persisted schedule:

```powershell
retrotv --config config\development.windows.local.toml show-schedule --channel fox --date 2026-07-27
retrotv --config config\development.windows.local.toml show-schedule --channel fox --date 2026-07-27 --programs-only
retrotv --config config\development.windows.local.toml validate-schedule 2
```

Phase 3 schedules are deterministic and persisted in SQLite. Regenerating the
same unlocked date atomically replaces its active generation. Program selection
uses a seeded series rotation, advances episodes in recognized episode order,
continues from prior schedule history, and avoids repeating an asset while
unused candidates remain.

The Dashboard's **Generate / Replace** action intentionally reproduces the same
schedule when its channel, date, media, and configuration are unchanged. Use
**Re-roll schedule** to give the selected channel/date a fresh seed and a
different valid arrangement. On a recurring channel, that date deliberately
ignores the inherited lineup and becomes the new base: already-generated later
dates in the same recurrence class are rebuilt chronologically. With Unique
weekends enabled, a Saturday re-roll updates later Saturdays without changing
weekdays or Sundays. Weekly and holiday ownership, episode order, cooldowns,
and every other scheduling rule remain active. Locked schedules are never
replaced; a locked matching date stops forward propagation.

Default dayparts are morning 06:00–12:00, afternoon 12:00–17:00, evening
17:00–22:00, and night 22:00–06:00. Episodes must fit before a boundary.
Ordinary programs are packed into clock-aware 15-, 30-, or multi-half-hour
packages. Episodes up to 18 minutes first try to consume the next unused episode
from the same series, in recognized episode order, inside one 30-minute package.
The scheduler keeps adding complete same-series programs until it reaches at
least 20 minutes of programming or the next item would exceed the half-hour.
Paired episode files use their exact file boundary as an act break: CRTV Sim
places a normal commercial pod there when a complete pod fits, and does not cut
inside either paired segment. Items classified as `short` continue back-to-back
without boundary breaks.
Programming has priority over interstitials: commercial pods may contain anywhere
from zero through the configured maximum when necessary to fit the selected
programs. The scheduler chooses the whole-item combination that best fits the
available time. The configured minimum and repeat cooldown guide selection but
do not force empty time when a smaller pod or a cooled item is the only useful
fit. Holiday limits remain hard limits. Different series are not mixed inside
one package.

Every ordinary assembled programming block starts with one eligible station
bumper. Bumper cooldown scales with the number available in the active media
pool: one bumper may repeat immediately, while larger pools increasingly favor
rotation. A bumper opportunity is never omitted merely because every bumper is
inside cooldown; the least recently used fitting bumper is selected instead.
If necessary, a bumper is reused so station presentation is not silently lost.
Missing bumper media simply starts the program immediately. Protected complete
 blocks remain untouched.

Filenames that say a show will be right back, return after messages, or ask
viewers not to go away are used only before a commercial pod. Filenames that
welcome viewers back or announce a return to the program are used only after
commercials when programming resumes. Punctuation and common transcription
differences are tolerated. General bumpers can fill either position or open a
program. A bumper that names a particular show must match the scheduled series;
unmatched named bumpers appear in **Library & Schedules → Bumper Placement
Review** and are withheld until assigned. Choose **Mark general** if the
detected show is a mistake. To link a bumper to a show, choose **Before
commercials** or **After commercials**, select the show in **Show to match**,
and save. Saved decisions can be changed under **All classified and saved**.

An individual episode longer than 15 minutes may receive one internal act break
for every 30-minute clock slot it occupies: one for a half-hour show, two for an
hour show, three for 90 minutes, and four for two hours. CRTV Sim searches around
the midpoint of each corresponding source-media portion for a fade to black,
overlapping silence, or a strong scene change. The source file is never split or
re-encoded. Playback uses media-offset schedule entries around each commercial
pod. Embedded chapter starts inside the safe search window add a small preference
only when a real visual or audio transition is nearby. The break stays at that
transition, and a manual break choice always wins. CRTV Sim prefers an out and
return bumper and reuses bumper or commercial media when a small pool would
otherwise eliminate a later break. If
no whole commercial pod can fit without displacing programming or the 30-second
Up Next screen, that break is omitted. Shorts and other files
of 15 minutes or less remain intact; movies currently receive an opening bumper
but no midroll, and complete blocks receive neither.

Remaining package time uses commercials and files from a `filler` folder. If a
large gap remains after the normal capped pod, one additional best-fit pod of up
to `gap_fill_commercials_max` commercials is allowed before the final station
graphic. Only an otherwise unfillable residue becomes `CRTV Sim Continuity`.
At a daypart boundary, a bounded pod of fitting commercials is also eligible,
followed by explicit filler and then continuity. Loose bumpers are never used
for this purpose. A future generated information-screen tier is reserved between
explicit filler and plain continuity.

Files in `promos` participate in both commercial pods and leftover filler gaps.
They count toward a commercial pod's item limit when used there and must fit
in full in either role. They retain the `promo` media type and one asset ID,
sharing the commercial cooldown across both uses (or the holiday rule's
cooldown for holiday promos). Cooldown is preferred, but the only fitting promo
may be reused instead of leaving usable time empty. Channel/daypart ownership,
weekly-special isolation, and holiday weights/limits still apply. Content Only
channels do not insert promos.

The presentation and break policy is configurable in
`config/base.example.toml`. `sequence_template` remains accepted for older
profiles; it no longer determines scheduling order. Use the explicit settings
below:

```toml
sequence_template = ["bumper", "commercial_break", "bumper", "episode"]
opening_bumper_enabled = true
midroll_enabled = true
midroll_min_media_minutes = 15
midroll_search_window_minutes = 4
midroll_min_segment_minutes = 5
midroll_max_pod_minutes = 5
midroll_fallback = "midpoint"
midroll_media_types = ["episode"]
commercials_per_break_min = 2
commercials_per_break_max = 3
gap_fill_commercials_max = 3
clock_slot_minutes = 30
short_slot_minutes = 15
same_series_pairing = true
short_program_target_minutes = 20
short_program_slot_minutes = 30
```

Older profiles may continue using `short_episode_max_minutes` and
`pairing_slot_minutes`; they are compatibility fallbacks for the new target and
slot settings.

For episodes occupying two half-hour slots, CRTV Sim reserves advertising time
for both mid-program breaks and the end of the hour. Each pod is capped at five
minutes by default for a two-slot episode, so one long commercial cannot
consume all available break time. The second natural-break search targets the half-hour
clock boundary after accounting for the first pod. Existing automatic break
analyses are refreshed once only for affected two-slot episodes; manual break
choices stay in place.

Application maintenance automatically regenerates saved schedule dates whose
configuration fingerprint no longer matches these settings. It also analyzes
new eligible episodes for natural breakpoints before schedule generation. Run
the analysis in bounded background-maintenance batches so a large first-time
library does not prevent the application from starting. Episodes still waiting
for analysis use the safe midpoint fallback. Run an unlimited analysis directly
with:

```powershell
retrotv --config config\development.windows.local.toml analyze-breakpoints
```

Use `set-breakpoint ASSET HH:MM:SS` for a manual override. The browser's
**Act Breaks** section shows eligible episodes, detection method, confidence,
and the currently selected timestamp.

The browser's **Linear Schedule** section can generate and inspect a channel
date. The detailed timeline includes interstitials and continuity; **Programs
only** provides a compact lineup. Episode titles in this table include the
series name, for example `Aaahh!!! Real Monsters - S02E01`.

## Phase 4 playback and join-in-progress

Check what CRTV Sim would tune without opening a player:

```powershell
retrotv --config config\development.windows.local.toml play-channel --channel fox --dry-run
```

The dry run prints the exact schedule entry, selected working or corrected
representation, wall-clock media offset, and remaining segment duration.

Start the live channel:

```powershell
retrotv --config config\development.windows.local.toml play-channel --channel fox
```

The Windows player uses mpv JSON IPC. It loads the scheduled file paused, waits
for `file-loaded`, seeks to the calculated in-progress offset, verifies the
reported position, unpauses, and retunes at the persisted entry boundary. Only
the selected channel is decoded. Press **R** to retune to the wall clock and
**Q** to quit.

Media resolution prefers a ready corrected staging file, then a promoted
corrected working copy, then the original. Missing media triggers bounded
recovery rather than silently changing the schedule. Continuity gaps use a
generated 640×480 slate.

For a fresh Windows installation, mpv can be installed with:

```powershell
winget install --id mpv-player.mpv-CI.MSVC --exact
```

## Tests

```powershell
$env:PYTHONPATH = 'src'
python -m unittest discover -s tests -v
```

The tests use temporary media and databases. They do not modify the real
library under `F:\Videos`.

## Current limitations

- Stretched 4:3 detection requires human confirmation when metadata cannot
  distinguish it from genuine widescreen video.
- Quick fingerprints use file size plus leading and trailing data. Full SHA-256
  is available for authoritative verification.
- A scan notices new, changed, renamed, removed, and potential duplicate files.
- Virtual multi-act scheduling awaits authored or approved internal break cues.
- Composite output and Pi hardware tuning remain Phase 8 appliance work.

## Phase 5 seasonal and recurring programming

Phase 5 treats holiday material as a calendar modifier and weekly branded
programming as a temporary programming owner. Halloween is active October
1–31 and Christmas is active December 1–31 by default.

Holiday programs can be scoped to an ordinary daypart or to an existing weekly
special. Use the configured daypart names; `daytime` expands to morning,
afternoon, and evening:

```text
media/fox/specials/holidays/halloween/night/episodes/
media/nickelodeon/specials/holidays/halloween/night/blocks/
media/nickelodeon/specials/holidays/halloween/daytime/episodes/
media/nickelodeon/specials/holidays/halloween/all-day/blocks/
media/nickelodeon/specials/holidays/halloween/specials/weekly/snick-main/episodes/
media/nickelodeon/specials/holidays/halloween/specials/weekly/snick-main/up-next/
media/nickelodeon/graphics/holidays/halloween/up-next/
```

These files never become eligible on another channel or outside their daypart
or weekly collection. The earlier `dayparts/<daypart>/` and
`weekly/<collection>/` paths remain readable, but new folders follow the
channel-like layout above. Existing flat paths such as
`media/fox/specials/holidays/halloween/blocks/` remain readable as **ordinary
all-day** holiday media; CRTV Sim never moves them automatically. Shared holiday
episodes may air on any channel while respecting daypart eligibility and the
active holiday. No separate episode cooldown is added. Shared complete blocks
still require explicit channel eligibility. Shared clips and explicitly eligible
movies may also use matching dayparts:

```text
media/shared/specials/holidays/halloween/night/commercials/
media/shared/specials/holidays/halloween/night/episodes/
media/shared/specials/holidays/halloween/night/bumpers/
media/shared/specials/holidays/halloween/night/promos/
media/shared/specials/holidays/halloween/night/movies/
```

The built-in Halloween and Christmas profiles use `boost` mode, weighted
holiday selection, a one-hour holiday cooldown, and at most two holiday
commercials per break. Change these values under `[seasonal.holidays.<name>]` in
`config/base.example.toml`. Add another holiday there and its drag-and-drop
folder becomes recognized without a code change.

Editable weekly starter profiles are included for SNICK, Adult Swim, Animation
Domination, TGIF, and MTV's Oddities. Their folders follow:

```text
media/nickelodeon/specials/weekly/snick/episodes/
media/cartoon-network/specials/weekly/adult-swim/episodes/
media/fox/specials/weekly/animation-domination/episodes/
```

Saving weekly programming blocks on the Dashboard Settings page automatically
creates any missing `episodes`, `shorts`, `movies`, `blocks`, `bumpers`,
`commercials`, `promos`, `station-ids`, `filler`, `up-next`, and
`interstitials` folders beneath each
configured channel and collection. Existing folders and media are never
overwritten, so a new block is immediately ready for drag-and-drop media.
The same save also creates empty holiday daypart folders and a matching holiday
variant for each weekly collection. A holiday weekly variant is optional and
uses the collection's existing time window. Its `up-next` and `interstitials`
files take priority only during that holiday and collection.

The `up-next` and `interstitials` folders are optional presentation overrides.
When their weekly collection owns the schedule, files in those folders take
priority over the channel's regular graphics. An empty folder falls through to
the existing holiday, daypart, and channel graphics. No placeholder is created
inside a weekly collection, so merely configuring a block never suppresses that
fallback. Audio files in either folder provide the matching weekly-special
graphic music. These graphics and audio remain outside the normal media library.

While a weekly window is active, its collection exclusively owns all of those
media pools. CRTV Sim never fills a missing weekly category with files from the
base channel. A collection containing both ordinary programs and complete
blocks retains the configured complete-block probability; when it contains no
episodes, shorts, or movies, the existing no-programming fallback can select an
eligible weekly block rather than falling through to base programming.

The starters are disabled to avoid changing a channel before its collection is
ready. Enable one without copying the other profiles by adding this to
`config/development.windows.local.toml`:

```toml
[seasonal.windows.adult-swim]
enabled = true
```

An end time earlier than its start time crosses midnight; for example,
`23:00`–`05:00` remains one Adult Swim window on both dates.

Scan after dropping in media, then regenerate the affected channel dates:

```powershell
retrotv --config config\development.windows.local.toml scan
retrotv --config config\development.windows.local.toml generate-schedule --channel fox --date 2026-10-01 --days 31
```

The generated entries store the programming owner, guide label, active rule,
collection, priority, selected weight, and an explanation. The web schedule
shows the owner and active rules; `show-schedule --json` exposes the complete
explanation. Equal-priority overlapping owners are rejected instead of being
resolved silently.

## Phase 6 complete blocks and cue sheets

Complete recordings from a channel's `blocks` folder can now enter the schedule
as one protected entry. CRTV Sim does not insert additional bumpers, commercials,
or promos inside a complete block. Join-in-progress seeks directly to the
elapsed position in the original block.

Default block policy:

```toml
[blocks]
selection_probability = 0.30
maximum_per_daypart = 3
repeat_cooldown_days = 30
fallback_when_no_programming = true
fallback_relaxes_repeat_restrictions = true
boundary_policy = "may_overrun"
long_daypart_threshold_hours = 8
extended_daypart_threshold_hours = 12
overlap_policy = "join_in_progress"
minimum_join_remaining_minutes = 60
minimum_join_remaining_fraction = 0.50
```

Complete blocks are considered only at the beginning of their governing
programming window. A daypart shorter than eight hours has one opportunity; an
eight-hour daypart has two evenly spaced opportunities; and a daypart of twelve
hours or longer has three. `maximum_per_daypart` caps those opportunities.
Weekly collections use the same rule within their own programming window. Each
opportunity makes one probability roll and, on success, chooses one eligible
block. It does not retry at ordinary half-hour slots.

The probability and repeat cooldown are diversity controls, not permission for
filler to beat real programming. When no episode, short, or movie can play at a
block opportunity, an eligible complete block is selected automatically. In
that fallback mode, recent-use and same-day repeat restrictions become soft;
the least recently used valid block is preferred and a lone block may repeat
in a later daypart. Boundary, overrun, join-in-progress, and minimum-remaining
rules are never relaxed. This behavior can be controlled from Settings with
`fallback_when_no_programming` and
`fallback_relaxes_repeat_restrictions`.

`may_overrun` lets a protected block finish after a daypart boundary. If an
earlier protected block occupies the next opportunity, `join_in_progress`
starts the selected block at the elapsed media offset while preserving its
nominal ending time. A join occurs only when both the configured minimum time
and fraction remain; otherwise that opportunity is skipped. `must_fit`, `hard`,
and `fill_short` refuse both daypart overruns and late joins. `soft` behaves like
`may_overrun` for protected blocks. No block may overrun the end of its saved
daily schedule.

The web interface lists every complete block and lets you edit:

- Its general TV Guide listing title
- Its per-block boundary policy
- An optional probability override from `0` to `1`
- A structured cue sheet using `HH:MM:SS` start and optional end times
- Cue types, titles, and descriptions

Cue sheets are replaced only after the entire sheet passes duration, ordering,
type, title, and overlap validation. A block without cues uses its general
title. Cue data is preserved for the Phase 7 guide to show the program currently
airing inside a long block.

## Phase 7 TV Guide channel

Phase 7 projects the active schedules into one shared guide data model. The live
browser guide and the rendered Guide channel therefore show the same Now, Next,
and rolling 30-minute listings used by playback. Commercials, bumpers, and
promos that belong to a program's sequence group are folded into that program's
listing instead of appearing as separate shows. Program cues inside complete
blocks supply the current listing title when available.

Episode listings prefer the series folder name and show the source filename
beneath it, so cryptic filenames such as `S1E23.mp4` still have a recognizable
show name. The series line is larger and bold, while the smaller filename line
keeps the continuously scrolling rows compact.

The three guide columns are anchored to a fixed 30-minute slot. At each top and
bottom of the hour, the two future columns shift one position toward `NOW` and
one new future column is added. For example:

```text
2:00 slot: NOW | 2:30 | 3:00
2:30 slot: NOW | 3:00 | 3:30
```

The application checks guide freshness every 15 seconds, but a render remains
valid for its whole half-hour slot. Crossing `:00` or `:30` makes it stale and
starts the next render. If the Guide channel is playing, the validated video is
swapped at the next seamless loop boundary.

Guide freshness also includes the resolved listings and renderer data
fingerprint. Restarting CRTV Sim after a guide implementation or schedule change
therefore replaces an incompatible render even when it is still in the same
half-hour slot.

The prerecorded guide does not include a clock because its render time would
quickly become misleading during playback.

If no guide-visible program covers a column timestamp, the guide displays
`TBD`. It does not pull the next future program backward into the gap.

With the web interface running, open:

```text
http://127.0.0.1:8765/guide
```

The page is a 4:3, 640x480-friendly live guide. It reloads schedule data every
15 seconds and rotates through four channels every 10 seconds. For repeatable
testing, append an ISO timestamp:

```text
http://127.0.0.1:8765/guide?at=2026-07-29T19:30:00-07:00
```

Inspect the same projection in PowerShell:

```powershell
retrotv --config config\development.windows.local.toml show-guide
retrotv --config config\development.windows.local.toml show-guide --json
```

Create and validate the prerecorded 640x480 guide loop:

```powershell
retrotv --config config\development.windows.local.toml render-guide
```

The default output is `data/generated/guide.mp4`. Channel rows move upward at a
constant speed as one continuous belt. The complete channel belt is repeated
enough times to cover the entire listing viewport at both ends of its travel,
and the loop duration is calculated so it reaches the exact starting position
at the wrap. There are no exposed background gaps, page pauses, fades, or hold
frames. The renderer validates the completed video before atomically replacing
the previous good file. A failed render leaves that previous file available.

Drop one or more music files into `<media root>/_guide/music`. Each new Guide
render chooses one valid MP3, WAV, M4A, AAC, FLAC, OGG, or Opus track at random
and avoids immediately repeating the previous track when another is available.
The scrolling visual belt repeats for the selected track without restarting its
audio at each short visual loop. The rendered file ends at the last complete
visual-scroll seam before the music finishes, so the music may be cut short but
the Guide's visual loop remains exact. A track shorter than one visual cycle is
padded with silence rather than restarted. An active Guide channel still adopts new
half-hour listings at the next short visual seam instead of waiting for the
entire song-backed file to end. `music_volume` controls the mix level and
`music_max_minutes` caps unusually long tracks. With no valid music files, the
Guide keeps its silent audio track. The `_guide` folder is reserved and never
becomes a discovered TV channel.

Run the refresh-safe form periodically to replace the file only when its
half-hour guide slot expires:

```powershell
retrotv --config config\development.windows.local.toml render-guide --if-needed
```

Channel numbers, row height, scroll speed, output, and refresh timing are
configured under `[guide]` in `config/base.example.toml`. Automatic recurring execution and
Guide-channel playback supervision are supplied by the Phase 7.5 application
runtime; the same refresh-safe command remains available for manual use.

### Demand-driven station announcers

CRTV Sim always renders the factual 640×480 coming-up-next screen from the active
schedule. An optional OpenAI station-announcer profile can write a natural,
channel-specific spoken script from the next three actual programming blocks
and synthesize it with that channel's configured voice and delivery style.
Windows SAPI is no longer selected automatically.

```powershell
.\.venv\Scripts\retrotv.exe `
  --config .\config\development.windows.local.toml `
  render-up-next --channel nickelodeon
```

The screen always retains exact schedule facts; the LLM may freely summarize or
combine repeated titles in speech. Results are cached beneath
`data/generated/announcements`. Use `--no-tts` for a music-only render or
`--force` to rebuild an existing cached clip.

Set `OPENAI_API_KEY`, then use **Library & Schedules → Station Announcer
Profiles** to enable channels, choose models and voices, edit personality and
delivery instructions, and generate each reusable generic fallback voice. The
equivalent fallback-generation command is:

```powershell
retrotv --config config\development.windows.local.toml `
  generate-generic-announcement --channel nickelodeon
```

Generated schedules now contain persistent, exact 30-second announcement
opportunities associated with programming-block identities. The fallback uses
the reusable generic station voice when it exists, otherwise music-only audio.
Dynamic OpenAI generation requires 60 seconds of continuous viewing,
recent human playback activity, and a lead of more than four but no more than
ten minutes. Playback never switches versions after an opportunity has begun.

Each enabled weekly collection also appears as an optional announcer profile.
When **Use weekly announcer profile** is enabled, its identity, on-screen and
spoken taglines, voice, prompts, models, and generic recording take precedence
inside that collection. Otherwise the active daypart profile, then the channel
profile, remains in effect. Dynamic AI generation has its own enable switch, so
a weekly identity and reusable generic recording do not require dynamic AI.

Each discovered channel also receives a separate graphics library:

```text
<channel>/graphics/up-next/
<channel>/graphics/interstitials/
<channel>/graphics/holidays/halloween/{up-next,interstitials}/
<channel>/graphics/holidays/christmas/{up-next,interstitials}/
<channel>/graphics/<daypart>/{up-next,interstitials}/
<channel>/graphics/<daypart>/holidays/halloween/{up-next,interstitials}/
<channel>/specials/weekly/<collection>/{up-next,interstitials}/
```

PNG, JPG, WebP, MP4, MKV, and WebM files are cataloged separately from normal
media. The Library page includes inventory, previews, destination-aware upload,
placeholder regeneration, and a Graphics To Do list for placeholder scopes that
do not yet have a user or AI replacement. Active weekly-special graphics take
priority when present; otherwise selection resumes through the normal holiday,
daypart, and channel defaults.
For ordinary station graphics, selection prefers an active holiday's daypart
art, then its channel-wide art, then standard daypart and channel art. Seeded
placeholders are a last resort and cannot hide a custom graphic at a fallback
scope. Older `specials/holidays/<holiday>/graphics/{up-next,interstitials}` art
remains readable; equally authored canonical art takes priority. New folders
use only `graphics/holidays/<holiday>` and `graphics/<daypart>/holidays/<holiday>`.
Move legacy custom art manually into these canonical folders when convenient;
CRTV Sim does not move or delete it. Nested `graphics/holidays` directories inside
the legacy holiday graphics root are ignored. Weekly-special art keeps its
existing collection-specific precedence, including holiday variants.
The Dashboard AI workflow has one **Generate and use AI graphic** action and an
optional art-direction field. It uses the validated composition-guide recipe,
creates the channel wordmark, resizes the result to 640×480, and installs it at
the selected exact scope immediately.
Generated graphics can later be regenerated, replaced with a local file, or
deleted; replacement and deletion restore normal scope selection without
touching placeholders or unrelated graphics.
The full inventory hides a generated placeholder whenever a valid user or AI
graphic exists for the exact same channel, weekly collection, daypart, holiday,
and purpose. The placeholder is retained and automatically reappears if its
replacement is removed or becomes invalid.
Up Next artwork follows a fixed 640×480 layout: the station logo belongs in the
top-center plaque, while the heading, three schedule listings, and tagline stay
inside a central text-safe panel. A reusable background, high-resolution master,
and labeled authoring overlay are provided in `assets/templates`. Newly created
or regenerated Up Next placeholders use this standard template automatically.

For source-development experiments, run:

```powershell
.\.venv\Scripts\retrotv.exe --config config\development.windows.local.toml graphics-lab
```

The local Graphics Lab opens at `http://127.0.0.1:8786`. It does not scan media,
write channel-graphics folders, or require a portable build. It compares the
existing text-only image request with template-reference and masked-template
edits, displays every result with the real Up Next text layout, and can use a
real station-logo file as both a visual reference and a fixed, exact overlay.
Its experiment files live under the configured state root in `graphics-lab`.
The lab defaults to `gpt-image-2.5-flare`; enter another model in the lab only
when comparing a specific recipe.
Standalone station graphics are optional and are used only after programs,
commercials, bumpers, promos, and ordinary filler cannot cover the remaining
time. They are not a required part of the final commercial break. A still image
holds or a motion background loops for the exact unresolved residue, even when
that residue is shorter than ten seconds. It may directly precede Up Next.
Visible continuity is used only when no usable channel graphic is available.
When several eligible images or videos exist, CRTV Sim chooses randomly; there
is no per-file recent-use restriction. User files continue to take precedence
over generated placeholders.

The 30-minute station-graphic cooldown is a repetition preference. CRTV Sim
overrides it whenever the only alternative is generated continuity. If no
usable station graphic exists, the last-resort continuity slate displays
visible SMPTE color bars instead of a near-black screen.

A scheduled channel with no generated entries remains safe to tune. CRTV Sim
holds its channel interstitial, rechecks for a schedule every 30 seconds, and
uses the continuity color bars only when no channel graphic can be rendered.

The Library media search accepts a title, filename, path, or an exact asset ID
written as `123`, `#123`, or `id:123`. The **Long commercials** review queue
holds files above the configurable three-minute default out of future pods
until you approve an exception, disable the file, or move it to another media
category. Each entry shows duration, location, and a playable preview. Existing
schedules refresh through normal maintenance without interrupting playback.
Station announcer profile cards can test
the currently entered text-generation settings and create a short playable TTS
sample without saving the profile. Temporary voice-test files are removed by
generated-file cleanup. The Settings startup-channel selector also includes the
CRTV Sim Guide.

Optional graphic music uses MP3, WAV, M4A, AAC, FLAC, Ogg, or Opus files placed
inside a channel's graphics folders. For example,
`<channel>/graphics/music/` is the general soundtrack, while
`<channel>/graphics/up-next/music/` and
`<channel>/graphics/interstitials/music/` are purpose-specific. Daypart,
holiday, and weekly-special folders can narrow the scope further. The most
specific matching folder takes precedence; when it has no valid track, CRTV Sim
falls back to the next broader applicable scope. Tracks loop to fit the graphic
and play quietly under Up Next narration. See the
[Getting Started guide](docs/GETTING_STARTED.md#optional-background-music) for
the folder examples and Weather soundtrack paths.

Weather music uses a separate library under
`RetroWeather/retroweather-media/music/`: condition-specific folders such as
`conditions/rainy/` take precedence over `default/`, with silence as the final
fallback. The RetroWeather dashboard opens this media folder.

Configured daypart branding overrides use the same fallback rules before the
ordinary channel folders. The shipped Nickelodeon `night` override creates
`nickelodeon/graphics/night`, labels the active nighttime identity **Nick at
Nite**, and exposes a separate Nick-at-Nite card in Station Announcer Profiles.
That card has its own AI enablement, voice, personality prompt, delivery
instructions, generic script, and reusable generic audio. An Up Next screen uses
the daypart of the first program it promotes, so a 7:59 PM screen for an 8:00 PM
program already uses Nick-at-Nite artwork and announcing. Standalone graphics,
the Guide channel label, and the channel-change OSD use the daypart active when
they appear.

## Phase 7.5 Windows application layer

The Windows application layer ties the existing media, normalization,
scheduling, playback, and Guide engines together behind one long-running
controller and dashboard. Start it from the project folder:

```powershell
.\.venv\Scripts\retrotv.exe `
  --config .\config\development.windows.local.toml `
  run
```

The dashboard opens at `http://127.0.0.1:8765/`. It provides television
start/stop/tune controls, the current program, automatic-maintenance status,
system readiness, links to the live Guide and library, schedule controls,
normalization previews, corrected-file creation, and confirmed replacement with
a verified original backup. It also reports the latest verified database snapshot
and provides a **Back up database now** action.

Routine configuration is available at `http://127.0.0.1:8765/settings`.
The Settings page covers discovered channel numbers, contiguous daypart hours,
holiday calendars and weights, weekly programming blocks, scheduler and act-
break behavior, Guide appearance and music, station graphics, announcer demand
rules, video and audio normalization, automatic maintenance, playback, and
generated-file cleanup. **Save & update schedules** immediately requests the
normal maintenance and schedule-regeneration workflow after saving.

By default, stopped television playback also starts a serialized readiness
pipeline: scan and update the library, convert approved video corrections,
analyze pending audio, and normalize approved audio corrections. Only one of
these media jobs runs at a time. Starting the TV lets the current FFmpeg file
finish safely, prevents another file or stage from starting, and resumes the
pipeline the next time playback stops. Library, aspect, duplicate, and
unusual-audio review items continue to require a person. Each automatic stage
can be enabled or disabled under **Automation** in Settings.

An optional daily maintenance window can guarantee an unattended readiness run.
Configure its start and end under **Maintenance window** in Settings; the
application timezone controls both boundaries, and an end time earlier than the
start time creates an overnight window. CRTV Sim checkpoints and stops playback,
runs the readiness pipeline, then resumes the same channel. Scheduled channels
join the current broadcast while Watch at Your Pace channels return to their
saved episode position. If all work finishes early, playback resumes immediately
and that day's window is considered complete. Starting the TV manually during a
window skips the remainder of that occurrence.

### Exact duplicate media

New or changed media receives a complete SHA-256 content hash before video
normalization begins. Exact matches remain separate assets, so the same file can
intentionally belong to several channels, dayparts, or weekly collections. The
hash lets CRTV Sim reuse a compatible aspect-analysis recipe and, when another
copy has already been corrected under the same normalization settings, stage
that verified result instead of running FFmpeg again.

The collapsible **Duplicate Media Review** section under Library & Schedules
shows hash progress, duplicate sets, scheduling locations, estimated duplicated
storage, and review status. **Keep all intentionally** dismisses a set without
deleting or disabling any copy. Adding another member to an intentional set
returns it to review. Existing libraries are hashed in bounded maintenance
batches; newly imported files are always hashed during their first scan.
Duplicate review contributes to the Guide's yellow `[!]` indicator.

### Programming inventory

The collapsible **Programming Inventory** section under Library & Schedules
lets you click any title to open the folder containing that collection in the
platform file browser. When episodes use season subfolders, the common series
folder is opened.
It groups episodes and shorts by their series-folder title, lists movies and
complete blocks, and summarizes bumpers, commercials, promos, station IDs, and
filler pools. Each row shows the item count, ready and review totals, channel,
daypart or special collection, and combined runtime. Search plus channel,
media-type, small-collection, and review filters make it possible to spot thin
series, misplaced programming, and expected shows that are absent without
browsing the media folders.

### Shadow files

Shadow files give one physical media asset additional scheduling placements
without copying, probing, converting, or analyzing the video again. A generated
file retains the real filename and adds `.shadow`, for example
`S02E12.mp4.shadow`. It is UTF-8 JSON containing a media-root-relative target
path, format version, and the target's working SHA-256 hash. Relative paths keep
the placement portable between installations; the hash and persisted target
asset allow CRTV Sim to retain the relationship when real media is moved.

Use **Create shadow** on an individual Media Library row, **Create shadows** on
a Programming Inventory folder, or the recursive folder picker in the
collapsible **Shadow Files** section. Choose a destination channel and a
daypart, recurring weekly block, or holiday collection. Batch creation preserves
media categories, series folders, and season subfolders, skips identical
existing shadows, and reports filename conflicts without overwriting them.

The scanner catalogs `.shadow` files separately from media assets. Ready
shadows contribute eligibility to their real asset, so playback, normalization,
audio loudness, act-break analysis, cue sheets, episode ordering, and used-asset
protection all continue to use the canonical video. Multiple placements of the
same target inside one scheduling pool do not multiply its selection chance.
Malformed files, invalid destinations, and unresolved targets appear in Shadow
Files review and contribute to the Guide's pending-action indicator. Deleting a
shadow removes only that placement; it never deletes the target video.
Programming Inventory displays shadows as separate tinted rows with a Shadow
badge. **Delete Shadows** previews the exact sidecar count before confirmation,
deletes only that row's `.shadow` files and records, and leaves canonical media
untouched. A changed preview is rejected, and affected future schedules rebuild
during maintenance.

Dashboard changes are written to a profile-specific file beside the selected
TOML profile—for example
`config/development.windows.local.settings.json`. CRTV Sim loads that file last,
so the application never rewrites or discards comments from the TOML files.
Unchanged values are omitted from the override, allowing future base defaults
to continue flowing through. Most settings take effect in the running program;
media paths, server address, timezone, and executable paths are clearly marked
and require a restart.

The dashboard's **Convert approved corrections** job processes only library-ready
assets whose approved effective mode is `pillarboxed_4_3`, `stretched_4_3`, or
`custom`. Files identified as `native_4_3` or `preserve_frame` are reported as
not needing correction and are not re-encoded. The job runs in the background
and shows its current file, progress, converted, already-done, not-needed, and
failed totals. With the default `replace_with_backup` policy, each corrected
file is validated, its original is archived under `originals_backup`, and the
working copy is then replaced. Assets already marked `promoted` are skipped, so
the job is safe to run again after approving additional media. Other output
policies retain ready staging proxies and likewise skip them on subsequent runs.

If a verified original is manually restored from `originals_backup`, run a
library scan and analyze that asset again. When the restored geometry differs
from the promoted working copy, CRTV Sim marks the old correction `stale` and
the next approved-corrections batch may process it again. A successful
replacement closes the prior promotion record as `restored` before recording
the new active correction.

The convenience launcher performs the same startup:

```powershell
.\scripts\start-retrotv.ps1
```

Pass `-StartTV` to tune the configured startup channel immediately, or use:

```powershell
.\.venv\Scripts\retrotv.exe `
  --config .\config\development.windows.local.toml `
  run --start-tv
```

Only one application instance can run against a profile at a time. Normal CLI
maintenance commands remain available while the application is stopped.

When CRTV Sim and its media/database live on an external drive, use **Shut down
safely** on the Dashboard before turning off the computer or disconnecting the
drive. Safe shutdown blocks new work, stops playback and the managed
RetroWeather companion, waits for the current FFmpeg and maintenance operation,
flushes SQLite's pending writes, and reuses a verified backup from the last 24
hours. If there is no recent completed backup, it creates and verifies one.
Shutdown does not perform a full database integrity scan when reusing a backup;
those checks remain part of backup creation. The Dashboard shows the current
shutdown stage, elapsed time, and any jobs it is waiting for. Step timings are
recorded in the application log. It displays a green confirmation only after
CRTV Sim has finished writing and closed its shutdown database connection.
Use the operating system's Eject/Safely Remove Hardware command before physically
unplugging the drive. Closing the browser tab does not stop RetroTV.

Portable builds also include `CRTV-Sim-Safe-Stop.exe` beside `CRTV-Sim.exe`. Use it
when the Dashboard cannot be opened. It sends a drive-local request to the active
runtime, so it does not depend on port 8765 or the browser. It waits for the same
writer drain, SQLite checkpoint, and recent-backup policy, then shows
a safe-to-remove confirmation. If neither the control channel nor the legacy
Dashboard API answers, it refuses to claim the drive is safe and instructs the
user to keep it connected and restart Windows.

The application now binds the Dashboard port before starting maintenance. A port
conflict therefore fails startup before FFmpeg or another media writer begins,
instead of leaving a headless maintenance process running without its controls.

At startup and every 15 minutes by default, the application:

1. Creates the day's verified SQLite backup if one does not already exist.
2. Performs an incremental scan; unchanged files are not reprobed.
3. Analyzes newly indexed media without replacing originals.
4. Extends missing schedules to maintain seven days of coverage.
5. Performs the heavier library and schedule maintenance.

Database snapshots use SQLite's online backup API, so WAL state is included even
while CRTV Sim is running. The source receives a quick integrity check; the staged
snapshot must pass a full integrity check and foreign-key validation before an
atomic rename publishes it. An invalid source or snapshot stops maintenance and
does not replace an existing healthy backup. The default location is `backups/`
beside the configured database, which becomes
`user-data/database/backups/` in a portable installation. Files are dated
`retrotv-YYYY-MM-DD.sqlite3`, one is created per day, and the newest seven are
retained. Enablement, location, and retention are configurable under
`[database_backups]` or the Settings page. **Back up database now** safely
refreshes today's dated snapshot on demand.

Guide freshness is checked separately every 15 seconds. A prerecorded Guide
becomes stale when the clock crosses an hour or half-hour boundary, without
rescanning the media library. The polling interval can be adjusted with
`guide_check_interval_seconds` under `[application_layer]` and
`max_age_minutes` under `[guide]`.

A full automatic scan retires database entries whose files have disappeared,
disables them from future scheduling, and resolves their library-review items.
The Dashboard reports a deletion once as `removed`, rather than warning about
it forever. CRTV Sim retains the asset metadata and backups so historical
schedules remain intelligible. Moving a file within the media root is recognized
by its fingerprint and updates the existing asset when the match is unambiguous.

These defaults are under `[application_layer]` in
`config/base.example.toml`. Automatic analysis remains non-destructive.
Installing a corrected working copy still requires explicit confirmation in the
library interface.

Generated Guide, announcement, station-graphic, continuity, and aspect-preview
media is also maintained automatically. Cleanup runs at most once every 24 hours
during normal maintenance and never removes outputs referenced by active schedules
or the active Guide render. By default, silent announcements are retained for 7
days, narrated announcements for 30 days, rendered station graphics for 14 days,
unapproved aspect previews for 7 days, and the newest 3 runtime Guide videos are
kept. Abandoned temporary render files and obsolete continuity versions are also
pruned.

Tracked pre-aspect and pre-audio-normalization recovery copies expire after 30
days by default. Before deleting one, CRTV Sim hashes the current working media and
verifies that it is the validated correction—or a later validated correction in
the same promotion chain. Missing, altered, unverified, and untracked backup files
are retained. Configure these policies under `[generated_cleanup]` or in the
Dashboard Settings.

Database housekeeping runs at most once every 24 hours while playback is stopped.
It keeps 30 days of schedule and operational history and retains missing-media
records for 90 days. A fresh verified database backup is required before rows are
removed. Compact schedule-history checkpoints preserve episode and complete-block
selection continuity after detailed schedule rows expire. Internet source
metadata, review corrections, watched history, and Watch at Your Pace progress
are retained. The Dashboard can run housekeeping manually and reports removed
row counts plus reclaimable database space.

**Empty Internet Cache** previews and removes managed Save Space downloads while
protecting active, incomplete, retained, and user-owned media. **Compact
Database** creates another verified backup, checks SQLite integrity, checkpoints
the WAL, and reclaims free pages; compaction is never automatic and requires the
TV and background writers to be stopped.

The Dashboard's **Clean files and expired backups now** button runs the same safe
cleanup immediately. The equivalent command remains:

```powershell
.\.venv\Scripts\retrotv.exe `
  --config .\config\development.windows.local.toml `
  cleanup-generated
```

When mpv has focus, its CRTV Sim input map supports:

| Key | Action |
| --- | --- |
| `0`–`9`, then `Enter` | Tune by channel number |
| `Backspace` / `Esc` | Edit or cancel channel entry |
| `G` | Tune to Guide channel 2 |
| `Up` / `Down` | Channel up/down |
| `L` | Return to the previous channel |
| `R` | Retune from the current wall-clock position |
| `Q` | Stop television playback |

The dashboard remains running after `Q` stops the TV, so playback can be
started again without rescanning or restarting the application.

When a new prerecorded Guide is rendered while channel 2 is playing, CRTV Sim
keeps the current loop uninterrupted and requests the reload at its next exact
loop boundary. Automatic runtime renders use versioned sibling files so Windows
never has to overwrite the copy mpv currently has open; old versions are
cleaned after safe replacements accumulate.

## Internet media and Watch at Your Pace

Open **Internet Media** from the Dashboard to create a scheduled or Watch at
Your Pace channel. Preview an Internet Archive item page or a direct HTTP(S)
video URL, review episode dates, select imports, and set the channel's exact
episode, start date, and saved timestamp. The merged playlist orders episodes by
date, same-date show priority, and the editable tie-order value.

Each source is also represented by a versioned sidecar beneath the normal media
tree, for example:

```text
Monday Night Wars/all-day/episodes/Raw/1998.source.json
```

Watch at Your Pace progress advances only after mpv reports a successful end of
file. CRTV Sim saves the position every 30 seconds during active playback and
immediately on pause, tune-away, stop, replacement, and shutdown. Save Space
uses the managed cache and live aspect correction; completed cache files are
deleted only after the player releases them. Keep Episodes and all scheduled
imports enter the normal scanner and normalization workflow.

The managed cache defaults to 50 GiB with a 5 GiB free-space reserve. Change
those values in Dashboard Settings. Downloads use a persistent one-worker queue
and recover safely after a restart.

New Watch at Your Pace channels remain idle until a starting date, exact
episode, or **Start at first episode** is selected. Scheduled Internet Media
channels prepare the first un-aired episode and one episode ahead by default.
That window advances only after the earlier retained episode has appeared in a
schedule and its airtime has ended. Changing channel mode or episode selection
cancels preparation that no longer belongs in the active window and removes
managed partial files.

The Internet Media page can delete either kind of channel after an exact typed
confirmation and a separate acknowledgement. Deletion removes the channel's
database records, source sidecars, and unfinished downloads. Finished source
media, normalized media, backups, and completed cache files remain untouched.
No ignore marker is created: if the channel folder remains, the normal scanner
will recreate the channel with Scheduled defaults on a later scan.

Portable launches mark each configured drive as CRTV Sim production storage. A
development profile that points at a marked drive warns before opening its
database or writing media and can continue only for that process. Portable
packages do not include the development dashboard settings overlay, which keeps
machine-specific development paths out of a new build.

Dashboard path overrides are authoritative. Portable startup may update an old
bundled-player path, but it never rewrites `storage_roots.media` or
`storage_roots.originals_backup` from the settings overlay. This allows the
application to live on a local drive while media and normalization backups live
on a separately selected external drive.

Version 0.9.26 is packaged separately from the current portable build. It
imports user data from a sibling 0.9.25 folder on first launch when available;
otherwise copy the current build's `user-data` folder into the new build before
launching. It preserves any existing restore point and creates one before the
new migration if needed. Keep the current installation intact. Extract the
complete `RetroTV-Portable-Internet-Media-0.9.26` folder beside it.
Run the external rollback helper after safely stopping CRTV Sim:

```powershell
powershell -ExecutionPolicy Bypass -File .\dist\Rollback-RetroTV-Internet-Media-0.9.26.ps1
```

Rollback verifies hashes, saves the post-upgrade database, restores the cloned
pre-feature database and settings, launches the verified previous build, and
waits for its runtime health endpoint. Internet source files, retained media,
and managed cache files are left in place for later review.

## Seasonal recurring lineups (0.9.41)

During an active holiday, recurring channels using Boost or Replace place holiday
episodes, shorts, and movies into a temporary recurring lineup before regular
shows. Each holiday series gets one placement per folder scope; normal shows
keep the remaining slots. Holiday episodes advance when scheduled, regardless
of viewing, and their progress continues when the holiday returns next year.
The regular lineup remains saved underneath and returns automatically afterward.

The recurring-lineup panel shows holiday assignments, waiting series, regular
fallback slots, and the normal lineup's return date. Fixed specials and full
blocks retain priority. Non-recurring schedules, holiday advertising, and
Decorate mode retain their existing behavior.

## Filename clues, Breeze Blue, and graphic variations (0.9.40)

In **Settings → Filename Clues**, edit comma-separated holiday and bumper phrases,
then save. Configured holiday names always remain active; additional phrases can
be removed. Holiday review only suggests groups: choose a destination and confirm
the move, or dismiss the suggestion. Bumper clues affect future schedule builds;
manual placement overrides take priority. Reset asks for confirmation and applies
when you save settings.

To use Breeze Blue, save its API key in **Settings → Breeze Blue credential**, then
choose Breeze Blue on an announcer profile and select an account model and voice.
Refresh reloads the dropdowns. The key is stored once in Windows Credential Manager;
`BREEZE_API_KEY` takes precedence. Existing OpenAI profiles keep their settings.
OpenAI still generates dynamic scripts; a Breeze voice test or generic fallback
can run with only the Breeze credential. Missing or unavailable saved voices are
retained until you choose another voice. Voice creation/cloning is managed in Breeze.

For an existing AI graphic, **Generate variation** adds a new image to its scoped
rotation without changing the original. **Regenerate** replaces all AI images in
that exact scope after confirmation, including variations. Delete removes only
the selected image. User-authored graphics retain their existing selection priority.


Windows distribution candidate setup, in-place updates and privacy are documented
in [DISTRIBUTION.md](docs/DISTRIBUTION.md),
[PRIVACY-AND-SERVICES.md](docs/PRIVACY-AND-SERVICES.md), and
[THIRD-PARTY.md](docs/THIRD-PARTY.md). See the remaining public-release gates
before publishing the ZIP.

## License

CRTV Sim's original source code and documentation are licensed under the MIT
License. Bundled third-party tools and assets retain their own licenses and
notices; see [docs/THIRD-PARTY.md](docs/THIRD-PARTY.md).
