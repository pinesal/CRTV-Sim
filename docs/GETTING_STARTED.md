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

**Download only `CRTV-Sim-0.9.53-Windows.zip` from
[the current release](https://github.com/pinesal/CRTV-Sim/releases/tag/v0.9.53)
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

If no strong natural cue is found, enabled fallbacks try a sustained quiet pause
before the generic midpoint. These cuts are labeled **Quiet-pause fallback** and
remain available for optional manual review. Use **All fallback breaks** to see
both types. A quiet pause reduces the chance of cutting through dialogue but
does not guarantee a scene has ended; background music can mask useful pauses.
In Settings, **Quiet pause, then midpoint** enables both fallbacks and **No
fallback breaks** disables both. Existing breaks remain usable while maintenance
refreshes their analysis, and manual choices are preserved.

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

In **Library & Schedules → Programming Inventory**, **Upcoming** shows program
airings remaining in generated schedules for all channels, including holiday and
weekly-special programming. Commercial breaks within an episode do not count as
extra airings. **Not scheduled** means there is no airing in those schedules;
dates that have not been generated yet are not included. Choose **No upcoming
programs** to find these entries. The separate **Recurring Lineup** column shows
saved recurring assignments, including regular slots preserved during holiday
rotation. Use **Refresh inventory** after background schedule generation.

Reconnect the drive that contains your library, then use **Scan & update now**.
If a new channel or file is still missing, check that it is under the correct
channel, daypart, and media-type folder. For example, an episode should be under
`Your Channel/evening/episodes/Series Name/`. The Library page and Dashboard
report scan or setup problems; you can also consult the more detailed
[Windows package guide](PORTABLE_WINDOWS.md).
