# Getting Started with CRTV Sim

CRTV Sim turns your video collection into a set of always-on TV channels. You
organize shows and other media into folders; CRTV Sim reads those folders,
builds schedules, and plays the channel from whatever point matches the current
time. You normally use the Windows app and its Dashboard. You do not need to run
the developer commands described elsewhere in this repository.

## Why use CRTV Sim?

CRTV Sim reads and plays your files directly. It does not rely on Plex or any
other media server. Your folders tell CRTV Sim which channel and daypart a show
belongs to; it handles the schedule and guide, can fill breaks with the
commercials and continuity clips you provide, and joins programs already in
progress. It is built for
personal collections with media from different sources and with different
formats or picture quality. Optional processing can help standardize that media,
and review screens let you make the call when an automatic correction is unsure.

## 1. Install and open CRTV Sim

**Download only `CRTV-Sim-0.9.55-Windows.zip` from
[the current release](https://github.com/pinesal/CRTV-Sim/releases/tag/v0.9.55)
to use the program.** Third-party sources, build inputs, checksums and GitHub's
automatic source-code archives are optional and are not needed to install or run
CRTV Sim. Checksums let you verify a download if you choose.

The Classic Comedy example channel and background music pack on that release
are optional extras. Each includes its own installation instructions.

1. Extract the complete CRTV Sim ZIP into a folder you can write to, such as
   **Documents\CRTV Sim**. Do not run it from inside the ZIP file.
2. Keep the app files and its **Media** folder together. You can instead choose
   a media library on another drive during setup.
3. Open the folder and double-click **CRTV-Sim.exe**. The Dashboard opens in your
   browser. You do not need to install Python or open a command prompt.

On the first run, a setup page asks where your media is and how you want CRTV
Sim to handle it. Pick the correct media folder and local time zone. Review the
optional video, audio, and commercial cleanup settings before continuing. These
choices affect media files, so leave a feature off if you do not want CRTV Sim to
make that kind of change. Smaller videos are not enlarged by video normalization.
You can set up Weather and AI services later; neither is required for regular TV
channels.

## Use an existing series folder instead

You do not have to rearrange an established TV collection. Open **Library &
Schedules**, expand **External Media Sources**, and:

1. Enter the series folder's full path, such as `E:\TV Series\Doug`, or a
   network share path accessible to the computer running CRTV Sim.
2. Enter the series name and choose a scheduled channel and daypart. You can
   instead enter a new channel name and optional channel number.
3. Choose **Add & Scan**. The maintenance pipeline scans videos in the folder
   and its season subfolders, then updates affected unlocked schedules. Refresh
   sources to see availability, scan progress and errors.
4. Use **Edit title & assignments** to add other channels/dayparts. Use **Rescan**
after adding or moving episodes. Maintenance scans requested sources and
periodically checks enabled sources (weekly by default); it does not rescan every
external library on every maintenance pass. Adjust
`[external_media].rescan_interval_hours` in the profile configuration if needed.

`daytime` covers morning, afternoon and evening; `all-day` covers all configured
dayparts. Recurring channels retain the same scope and scarcity rules as managed
media. Episode names containing season/episode numbers are ordered naturally;
other filenames are supported, but cannot provide missing episode numbers.

External media is **always read-only**. Video/audio normalization and commercial
trimming do not apply. Read-only act-break analysis may still identify places for
commercials. Put commercials, bumpers, graphics and other managed media in the
normal Media folder; they can be used alongside external episodes.

**Disconnect** removes a source from new scheduling without deleting its files
or catalog history. **Reconnect** and **Rescan** restore it. An offline network
share preserves the catalog; unavailable scheduled files use continuity until
normal programming can resume. Locked schedules are not rewritten.

External sources currently support **scheduled channels only**. At Your Pace
integration is planned separately; its existing Internet Media behavior is
unchanged. The Windows account running CRTV Sim must be able to access the path,
even if you open the Dashboard remotely. No Plex server or connector is required.

## Use a live IPTV feed instead

In **Internet Media → IPTV Live Feeds**, enter a direct HTTP/HTTPS HLS or
MPEG-TS stream URL, or inspect an M3U channel list and select a feed. Assign it to
a scheduled channel/daypart or weekly special. Choose **Test selected feed** first,
then **Add feed** once the test passes. Testing creates no channel or folders.
The separate test player opens on the PC running CRTV Sim.

Live windows have no inserted commercials or other local programming. Shared
programs do not conflict and are ignored during live playback; explicit
channel-specific programs in the same active window cause a visible conflict
instead of silently choosing a source. See [IPTV setup and troubleshooting](IPTV.md).

## 2. Put media in folders

The **Media** folder is your library. Copy **_channel_template**, rename the
copy to a channel name, then put videos into its folders. CRTV Sim discovers
channels from the folder names and uses subfolders to decide what each video is
for. Keep a show's episodes together under a series folder; season folders are
optional but help keep a large collection tidy.

For example:

```text
Media/
  _channel_template/                 copy this to start a channel
  Nickelodeon/
    morning/episodes/Rugrats/Season 01/
    afternoon/episodes/Doug/
    evening/commercials/
    night/episodes/Bewitched/
    specials/weekly/                  weekly blocks and specials
  shared/
    all-day/commercials/              available to eligible channels
```

Common dayparts are **morning**, **afternoon**, **evening**, **night**, and
**over-night**. The **daytime** folder can supply morning, afternoon, or evening;
**all-day** can supply any daypart. Each folder contains media-type folders such
as **episodes**, **movies**, **commercials**, **promos**, **bumpers**, and
**filler**. Put holiday episodes under
`specials/holidays/<holiday>/<daypart>/episodes/`. The bundled template shows
the supported layout; you can add media gradually and rescan as you go.

The **shared** folder is for items that can be considered by multiple channels,
such as commercials or episodes. Shared items are used alongside channel
content when their daypart and channel eligibility allow it.

### Optional background music

Music is separate from program audio, commercials, and bumpers. Supported
formats are MP3, WAV, M4A, AAC, FLAC, Ogg, and Opus. CRTV Sim loops the selected
track to fit the screen and keeps it quiet under Up Next narration.

For a channel's Up Next screens and station interstitials, put music in the
matching graphics scope. You can put tracks directly in the scope folder or in
an optional `music` subfolder; both layouts work. For example:

```text
Nickelodeon/graphics/music/                         applies to all graphics
Nickelodeon/graphics/up-next/music/                 Up Next only
Nickelodeon/graphics/interstitials/music/           interstitials only
Nickelodeon/graphics/night/music/                   nighttime graphics
Nickelodeon/graphics/night/up-next/music/           nighttime Up Next only
Nickelodeon/graphics/holidays/halloween/up-next/music/
Nickelodeon/graphics/night/holidays/halloween/interstitials/music/
Nickelodeon/specials/weekly/snick-main/up-next/music/
```

CRTV Sim selects one soundtrack rather than mixing tracks from several folders.
For a weekly special, its matching purpose folder has first priority. Otherwise,
the fallback order is: active holiday plus current daypart and purpose; current
daypart and purpose; current daypart general music; active holiday and purpose;
channel-wide purpose; then channel-wide general music. For example, a track in
`graphics/night/up-next/music` is used for Nick at Nite Up Next screens when it
exists; otherwise CRTV Sim tries the next matching scope. Add more than one
track to a folder to let CRTV Sim choose among them. Use the same structure under
another channel folder to give that channel its own soundtrack.

The Weather channel has a separate music library inside the RetroWeather folder
by default, not under `Media/<channel>/graphics`:

```text
RetroWeather/retroweather-media/music/default/
RetroWeather/retroweather-media/music/conditions/rainy/
RetroWeather/retroweather-media/music/conditions/stormy/
RetroWeather/retroweather-media/music/conditions/snowy/
RetroWeather/retroweather-media/music/conditions/foggy/
```

Weather uses a matching condition folder when it has a valid track, then falls
back to `music/default`; if neither has music, it uses silence. You can open the
Weather media folder from the RetroWeather dashboard and add tracks there.

The optional `CRTV-Sim-Optional-Music-Pack.zip` adds a default soundtrack for
the Guide and Weather channels, plus a general soundtrack in the channel
template. Extract it over the CRTV Sim installation folder and merge folders.
If your Media root is on another drive, copy the pack's `Media` contents into
that configured root. The pack includes a credits file with the music sources.

## How commercials, bumpers, and interstitials work

The **Act Breaks** panel under **Library & Schedules** shows **Series — Episode**
when the series name is known. Click a title to open the full video in another
tab, then use the manual timestamp controls to choose a better break. Some
source formats may need an external video player rather than browser playback.
No source file is split or edited by choosing a breakpoint.

Choose **Open workshop** on an Act Breaks entry to work inside the panel. Only
one workshop is open at a time. Select the break to adjust, scrub the full-program
or loaded-preview timeline, use the five-second jumps, or enter a timestamp and
choose **Go to timestamp**. **Previous Frame** and **Next Frame** select actual
source frames, including for variable-frame-rate videos. Short temporary clips
are generated only when you press Play, so browser-incompatible source formats
can also be reviewed. Scrubbing and frame stepping fetch source frames directly;
they do not wait for a video clip to be encoded. Nearby seeks reuse frame timings
and loaded playback clips. Playback previews cover about 20 seconds at a time.

**Play around this point** lets you listen before and after a candidate.
**Place Act Break** saves the verified frame boundary: commercials begin before
that frame and the program resumes with it. Browsing alone does not change the
saved cut. **Reset to automatic** restores the automatic choice. Corrected
entries remain visible under fallback filters until you refresh or change the
filter. The existing typed timestamp controls remain available if previews fail.
Break opportunities follow the program's normal rounded schedule package:
30 minutes has one, 60 has three, 90 has five, and 120 has seven. Each half-hour
portion has an interior opportunity and shares a boundary opportunity with the
next portion. Preferred boundary targets allow approximately five minutes of
advertising per portion; actual commercial lengths determine broadcast timing.
Shortened daypart windows do not create a different permanent breakpoint layout.
The scheduler may omit a pod when time or suitable advertising is insufficient.
Minimum program-segment spacing always applies.

Previews use the same working video as act-break analysis. They do not change
managed or external source files. If that video changes during review, reopen
the workshop before saving. Inactive workshop previews expire through temporary
file cleanup; open workshops retain their previews while connected.

Act-break selection follows this order: a strong natural cue (fade/black or a
supported scene transition), a **Still-card cue**, a **Quiet-pause fallback**, a
**Scene-transition fallback**, then the generic midpoint. Manual choices always
win. Still-card detection looks for a near-still image held for 2–15 seconds,
bounded by scene changes; music can continue throughout. Adjacent qualifying
cards use the transition between them, while a continuous hold uses its midpoint.
This is a visual heuristic, not recognition of the words on a title card:
animated or noisy cards may be missed, and a similarly held photograph or
freeze-frame can resemble a card. The configured search window is a preference for strong natural cues, which may
be selected elsewhere in the safe program range. Held-card cues and fallback
cuts stay inside their target's preferred window. Selection considers all targets
together, preventing an early choice from unnecessarily blocking later breaks.

Black detection also tolerates VHS recordings whose faded frames remain gray or
contain tape noise. These candidates require brighter footage on both sides;
very short tolerant intervals also need a measurable fade into darkness, rather
than an abrupt tape flash. Music need not stop. The workshop identifies strict
and VHS-tolerant black evidence, and explains selections outside the preferred
window. Fades inside sketches can still resemble commercial boundaries; the
workshop remains available for correction.

Manual timestamps remain fixed when cadence is updated. They replace the nearest
available automatic opportunities in order. If a legacy program already has more
manual cuts than the new normal count, all are retained and no automatic cuts
are added; the Dashboard explains this exception. Target-layout changes invalidate
open workshop saves so a changed target number cannot receive the wrong cut.

A quiet-only fallback now requires a complete pause of at least **1.5 seconds**
below the existing loudness threshold. A scene-transition fallback requires no
silence and is preferred over an arbitrary midpoint, but can still interrupt
speech. Use the workshop for optional corrections; its evidence shows held-card
boundaries, quiet duration and why a weaker cue was selected.

Use **All fallback breaks** to see quiet-pause, scene-transition and midpoint
cuts. **Still-card cues** has its own filter. In Settings, **Use fallback breaks**
enables the quiet-pause → scene-transition → midpoint sequence; **No fallback
breaks** disables those three, retaining natural and qualifying held-card cues.
Existing cuts and their recorded target layout remain usable after a failed refresh
and while bounded
maintenance refreshes them, prioritizing upcoming programs. Manual overrides
survive reanalysis; source files are never changed. Updated cuts flow into later
eligible schedule rebuilds without interrupting playback or replacing locked
and historical schedules.

These are three different kinds of station material:

- **Commercials** are the ad clips in a channel's `commercials` folder. CRTV Sim
  builds short commercial pods at suitable program breaks and when there is
  unused time to fill. It chooses clips that fit the available time. Program
  content takes priority, so a pod can be shorter than the configured target or
  skipped if no suitable clips fit.
- **Bumpers** are brief transitions around programs and commercial breaks, such
  as “we'll be right back” or “welcome back.” Put them in a `bumpers` folder.
  CRTV Sim uses filename clues to place break-start and return bumpers at the
  matching side of a break; general bumpers can be used in either position.
  Bumpers linked to a particular show are reserved for that show.
- **Interstitials** are channel-branded stills or short videos, not ads. Add
  them under `graphics/interstitials/` or a more specific daypart or holiday
  graphics folder. They cover small leftover gaps after scheduled programs,
  commercials, promos, and explicit `filler` clips have been placed. A still can
  stay on screen or a video can loop for the exact remaining time. If no usable
  station graphic is available, CRTV Sim shows its continuity slate instead.

The separate **Up Next** screen previews upcoming programming; an interstitial
is a general station graphic used to cover a gap. Add station graphics through
**Library & Schedules** if you prefer to upload or generate them in the Dashboard.

In **Channel Graphics**, **Include station name and logo in AI artwork** is on
by default. Uncheck it to request an unbranded background without automatically
including the station name in the AI prompt. The image still goes to the
selected channel, daypart, and holiday folder. Your art-direction text is sent
as written, so leave station names out of that text too when you want a generic
background. Generation progress and provider errors appear beside these controls.

After copying artwork directly into a graphics folder, use **Refresh channel
graphics** to index it. Real user or AI artwork takes priority over applicable
holiday, daypart, and weekly-special placeholders. A real holiday or special
graphic still takes priority over real year-round artwork. Placeholders are the
last resort when no applicable real artwork exists; artwork from an inactive
holiday or another special is not borrowed. Existing scheduled interstitials
switch from a placeholder to newly indexed real artwork on their next render,
without changing schedules or interrupting an already-playing screen.

Up Next can use AI narration when the watched channel meets the generation rules.
If AI is unavailable or generation fails, it uses a prerecorded generic announcement.
Choose **Save & generate generic voice** in Station Announcer Profiles to prepare
that recording. A channel's recording works across its dayparts and weekly Up Next
screens; optional profile-specific recordings can take precedence. Playing a saved
recording requires no API key. Music-only video is the last resort when no usable
recording is available or narration cannot be incorporated.

Your last working recording stays available when you edit a profile or generate a
replacement. **Ready — update available** means the recording can still play while
you prepare a new version. A failed replacement does not discard it. Completed
generic recordings are never deleted by automatic cleanup, and cached Up Next
screens refresh their narration when a replacement becomes available. Weekly promo
cards retain their separate narration behavior.

## 3. Let CRTV Sim scan and schedule

After adding or moving files, open the Dashboard and choose **Scan & update now**.
CRTV Sim indexes the library and runs its automatic readiness work. It can
identify files that need attention and list them for review; you do not have to
approve every ordinary media file before it can be scheduled. The Library page
shows what was found and any items that need a decision.

Schedules are made from eligible media in the channel folders. The daypart
folders determine when shows can air. If you want a recognizable lineup that
keeps its regular show times, enable **Recurring schedule** for that channel in
**Library & Schedules**. Otherwise, the scheduler can create a different lineup
when schedules are refreshed. Fixed weekly specials keep their reserved times.

Each special also has its own **Schedule** choice under **Settings → Weekly
programming blocks**. **Recurring lineup** keeps established show slots within
the special; **Random shows** varies show placement. Both advance episodes in
order, whether you watch them or not. Each special's episode progress is separate
from the regular channel and other specials, including when shadows reuse the
same videos. A completed series can begin another pass through its episodes.

Existing specials initially retain their channel's current mode. Changing the
parent channel later does not change an initialized special's choice. Switching
a special's mode preserves episode progress; switching to Recurring uses its
most recent lineup where compatible media remains available. Use **Save & update
schedules** to apply changes to eligible unlocked schedules.

On upgrade, retained schedule history seeds the last episode and established
slots for each special. Rebuilds do not consume another episode. Saved progress
survives restarts and normal history cleanup; locked and historical schedules
remain protected by the existing schedule-update rules.

Use **Live Guide** to see what is scheduled. It includes the CRTV Sim Guide
channel, which presents the schedule for the other channels.

## 4. Watch TV

On the Dashboard, select a channel and choose **Start TV** or **Tune**. CRTV Sim
joins the program that belongs on at the current time, so a show may begin part
way through rather than starting from its first frame. Channel changes follow
the schedule. The remote-control keys shown on the Dashboard can also change
channels and open the Guide.

For shows with multiple audio or subtitle tracks, change tracks in the player
as usual. CRTV Sim automatically remembers your last choice for that exact series
name, including subtitles turned off. It reapplies the choice after commercial
breaks, channel changes, and restarts, and shares it across channels showing the
same series name. If a later episode lacks the chosen track, that episode uses
its default without changing your saved preference. Tagged tracks are matched by
language and title; untagged tracks use their order within the file.

Weather is optional. If you enable it during setup, finish the location setup
and create the first weather video before tuning to that channel. Other channels
remain usable while you set up Weather.

## 5. Where to change things

- **Dashboard:** start or stop playback, tune channels, scan the library, refresh
  the Guide, and check whether automatic work is still running.
- **Library & Schedules:** review media, manage channels and schedules, and
  configure channel graphics and announcers.
- **Internet Media:** connect supported online sources or create a Watch at Your
  Pace channel.
- **Settings:** adjust dayparts, channel numbers, holidays, scheduling, media
  processing, cleanup, and other system-wide choices.

AI graphics, AI announcers, and Weather services are optional. Some AI features
require an API key and may incur charges from the provider. Ordinary scheduled
playback does not require an AI account.

## 6. Shut down and update safely

Use **Shut down safely** in the Dashboard and wait for its completion message
before turning off the computer or disconnecting a media drive. Closing the
browser window alone does not stop the TV system cleanly.

For an in-place update, stop CRTV Sim safely, keep your **user-data** and **Media**
folders, and copy the new application files over the old application folder.
Do not replace your personal library or user-data with the clean folders from a
new ZIP. The detailed update instructions are in
[the Windows package guide](PORTABLE_WINDOWS.md).

## If something does not appear

Library panel headings show their pending counts even when collapsed. Nonzero
counts are highlighted; zero means that queue has no pending items. Review and
analysis counts refresh after actions and once a minute while the page is visible,
independently of table filters and the slower Programming Inventory refresh.
Hover over a badge for its definition. A dashed badge retains the last known
count if refreshing fails; an unavailable status is not a zero.

The Media Library badge counts each file once across Library, long-commercial,
aspect, and audio review. Duplicate Media Review counts pending **sets**, not
copies. Act Breaks separates automatic analysis work from programs using
fallback cuts, whose manual review is optional. The same item can appear in
more than one panel, so do not add panel counts together. Graphics replacement
counts are optional artwork tasks; missing optional Weather layers and
never-generated announcer recordings are not reported as failures.

In **Duplicate Media Review**, click an individual file path to open its video
in a new tab, as in Holiday Filename Review. **Delete copy** lets you choose
exactly which managed video to remove. The confirmation shows its full path,
the copies being kept, and any shadows that reference it. Stop TV and wait for
maintenance to finish before confirming. Deletion is permanent; it preserves
other copies, backups, and schedule history. Shadows are not deleted; they may
need review or reconnect to a matching copy during rescanning. External media
remains read-only. Matching uses original content hashes, so normalized working
copies may differ. Choose **Keep all intentionally** to retain all placements.

In **Library & Schedules → Programming Inventory**, **Upcoming** shows program
airings remaining in generated schedules for all channels, including holiday and
weekly-special programming. Commercial breaks within an episode do not count as
extra airings. **Not scheduled** means there is no airing in those schedules;
dates that have not been generated yet are not included. Choose **No upcoming
programs** to find these entries. The separate **Recurring Lineup** column shows
saved recurring assignments, including regular slots preserved during holiday
rotation. Use **Refresh inventory** after background schedule generation.

Inventory results are reused between page visits while CRTV Sim stays running.
The last successful results remain visible during background refreshes, along
with a **Last updated** timestamp and refresh status. **Refresh inventory**
requests an immediate update. The first inventory load after restarting CRTV Sim
can still take longer; a failed refresh keeps the previous results available.

Click **Items needing review** in Programming Inventory for a complete list of
the files behind that count, their review categories, and recorded reasons.
The count includes Library review (including long commercials) and shadow
review, even when a file is unclassified or its shadow target is missing.
Use **Review file** or **Review shadow** to jump to that item's review controls.
Individual shadows can be deleted from the Shadow Files panel after confirming
the exact path; this removes only the shadow sidecar, leaving original media
and other shadow placements untouched. Aspect, audio, holiday, duplicate, and
act-break reviews are separate and are not included in this inventory count.

Reconnect the drive that contains your library, then use **Scan & update now**.
If an individual channel folder has disappeared, **Channel folders and queued
deletions** on Library and Internet Media offers **Recreate folders** or
**Review deletion**. Recreate restores empty folders; it cannot recover videos.
CRTV Sim keeps the channel's catalog until you decide. A disconnected library
drive is reported as unavailable instead of suggesting that its channels be deleted.

Confirmed channel deletions are queued, so a busy database no longer requires
repeated clicks. The same panel shows whether a job is waiting, running,
completed, or failed. Wait for background work to finish, and tune away if that
channel is playing. You can cancel a waiting deletion; otherwise it resumes
after a restart. Finished media stays on disk, and a remaining channel folder
can register the channel again on a later scan.

If a new channel or file is still missing, check that it is under the correct
channel, daypart, and media-type folder. For example, an episode should be under
`Your Channel/evening/episodes/Series Name/`. The Library page and Dashboard
report scan or setup problems; you can also consult the more detailed
[Windows package guide](PORTABLE_WINDOWS.md).
