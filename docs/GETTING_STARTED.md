# Getting Started with CRTV Sim

CRTV Sim turns your video collection into a set of always-on TV channels. You
organize shows and other media into folders; CRTV Sim reads those folders,
builds schedules, and plays the channel from whatever point matches the current
time. You normally use the Windows app and its Dashboard. You do not need to run
the developer commands described elsewhere in this repository.

## 1. Install and open CRTV Sim

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

Reconnect the drive that contains your library, then use **Scan & update now**.
If a new channel or file is still missing, check that it is under the correct
channel, daypart, and media-type folder. For example, an episode should be under
`Your Channel/evening/episodes/Series Name/`. The Library page and Dashboard
report scan or setup problems; you can also consult the more detailed
[Windows package guide](PORTABLE_WINDOWS.md).
