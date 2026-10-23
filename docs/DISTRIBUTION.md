# CRTV Sim 0.9.56 for Windows

**Only `CRTV-Sim-0.9.56-Windows.zip` is needed to install and run CRTV Sim.**
Third-party sources, build inputs and checksums are optional downloads. The
example channel and music pack are also optional extras.
The application folder includes `CHANGELOG.md` describing the latest changes.

Extract the entire ZIP into a writable folder, such as Documents\CRTV Sim.
Keep the application directory and bundled **Media** directory together. Open
the application directory and double-click **CRTV-Sim.exe**. No administrator
rights or separate Python installation are required. Windows 10/11, 64-bit, with
working graphics/audio drivers is the supported target. Other versions are untested.

First launch opens local setup. Acknowledge the bundled Microsoft runtime terms,
which apply only to Microsoft's component. Select your media folder and confirm your timezone.
Choose presentation, video normalization, audio normalization and commercial-tail
trimming independently. All media processing choices start off for a fresh profile.
Review the summary before starting. Optional Weather and paid AI services can be
configured later. If Weather is enabled, the Dashboard requires you to choose
your location in RetroWeather and use **Fetch weather + create channel**. Weather
stays out of channel navigation and the Guide until a valid playable video is
published. The Guide and other channels remain usable during Weather setup.

The Library page's **Weather Graphics** section can upload or generate day/night
weather backgrounds, alert artwork, and transparent holiday borders. Generation
uses your OpenAI key; uploads need no AI provider. Holiday controls and activation
dates follow the holidays configured in CRTV Sim Settings, including custom
holidays. Artwork becomes available on the next Weather render.
Cancelling setup does not scan or change media; restarting resumes it.
An empty library is supported. Existing profiles skip setup automatically.

## Drop media into folders

Copy `Media/_channel_template`, rename the copy to a channel name, then add videos.
The template itself is ignored. CRTV Sim discovers channels automatically.

```
Media/
  shared/                         # Eligible media shared between channels
  _channel_template/              # Copy and rename; never used as a channel
  Your Channel/
    morning/episodes/Series/Season 01/
    afternoon/, evening/, night/, over-night/, daytime/, all-day/
      episodes/, shorts/, movies/, blocks/
      commercials/, bumpers/, promos/, filler/, station-ids/
    specials/holidays/halloween/<daypart>/<media type>/
    specials/holidays/christmas/<daypart>/<media type>/
    specials/weekly/               # Create a weekly special in Settings
    graphics/up-next/, graphics/interstitials/
    graphics/<daypart>/up-next/, graphics/<daypart>/interstitials/
    graphics/holidays/<holiday>/<purpose>/
    graphics/<daypart>/holidays/<holiday>/<purpose>/
```

Weekly-special settings create collection folders and their holiday variants.
Standard shared dayparts support episodes, commercials, bumpers, promos and filler.
Shared holiday pools also support movies and other clips; complete shared blocks
require explicit channel eligibility and are not scaffolded as a default pool.
Do not put holiday media directly under a holiday without a daypart. `daytime`
covers daytime periods; `all-day` covers every configured daypart. Complete blocks
already containing commercials remain intact. Templates contain no personal media.

## Processing and storage

Video presets are ceilings: 480p 640Ã—480, 720p 1280Ã—720, 1080p 1920Ã—1080,
4K 3840Ã—2160, or Custom. Larger sources fit within the ceiling; smaller sources
are not enlarged unless the independent upscale setting is enabled. Preserve
source aspect and retro 4:3 correction are separate choices. Guide and generated
station graphics remain SD; selecting 1080p does not make them HD.

Video proxy mode stores separate optimized copies under `user-data/normalized`.
Replace mode installs a corrected working file with a verified original backup.
Audio normalization and commercial trimming replace working media with recovery
backups even when video uses proxies. Backups require extra space and default to
30-day retention. Review the cleanup settings if you want longer retention.
Disabled features cannot be invoked silently by automatic or manual processing;
enable them in Settings to process media. Previously corrected files stay as-is.
Changing a ceiling does not recover discarded pixels or reconvert the whole library.

All settings, database, logs, caches and generated files live in **user-data**
inside the application directory. Keep it writable. Media can live on a removable
or network drive; it must remain accessible during playback. Playback/indexing can
use a read-only media root, but source-replacing operations require write access.
Protected application folders can prevent startup: move the complete application
to a writable location. Cloud-sync software can interfere with SQLite and video
writes; prefer a local, unsynchronized profile directory.

## Updating in place (your existing workflow)

1. Use **Shut down safely** and wait for the green confirmation.
2. Copy the existing `user-data` folder somewhere safe while CRTV Sim is stopped.
3. Copy new `CRTV-Sim.exe`, `CRTV-Sim-Safe-Stop.exe`, `_internal`, `assets`,
   `migrations`, `tools`, `portable-defaults`, `pyproject.toml` and documentation
   over the existing application folder. Also copy `licenses` and
   `MICROSOFT-RUNTIME.json`. Merge the assets; keep your own fonts.
4. **Do not replace or delete `user-data`, your Media library, or personal files.**
5. Run CRTV-Sim.exe. Existing configuration and database identify the installation
   as an upgrade even without a setup-completed marker. Library setup does not
   repeat. If Microsoft runtime terms have not been acknowledged, a one-time
   terms prompt appears before startup.

Defaults already inherited by an existing profile are preserved. Database schema
migrations create a pre-migration restore point when required. If updating fails,
stop CRTV Sim and restore the entire pre-update profile backup together with the
previous application files. Do not open a migrated database with an older build
without restoring its compatible backup.

CRTV Sim was previously named RetroTV. Existing installation folders do not need
renaming. Keep `user-data` and your media paths unchanged. The Python package,
`retrotv` command alias, database/log names, environment variables, production
markers and Windows Credential Manager targets retain their original internal
names for compatibility; saved API keys continue to work. Start the new
`CRTV-Sim.exe` after copying an update and update any shortcuts that still point
to `RetroTV.exe`. An old executable left in the folder still runs the old build.

Alternatively, extract into a new version directory and copy your stopped
installation's `user-data` into it before first launch. Absolute media paths remain
unchanged; check relative paths if you moved the application. The public ZIP does
not silently import another sibling installation. Updates are manual in this release.

Use **Shut down safely** before disconnecting drives. Closing the browser is not
shutdown. If the Dashboard is unreachable, use **CRTV-Sim-Safe-Stop.exe** and wait
for confirmation. Then use Windows Eject/Safely Remove Hardware.

## Troubleshooting and release status

Dashboard: http://127.0.0.1:8765/ . Weather: http://127.0.0.1:8775/ when enabled.
For an unavailable media drive, reconnect it and use Scan & update. Setup will not
run again merely because a library is empty, unavailable or damaged. If a profile
cannot load, the launch dialog points to `user-data/logs/retrotv.log`.

Report the version, Windows version, action, timestamp and relevant log excerpt
to the maintainer through the project's discussion/support channel. Remove API
keys, private filenames, network shares and usernames before sharing. Never upload
your Credential Manager contents or entire profile as a default diagnostic step.

This is an unsigned distribution. Windows may show SmartScreen prompts.
Optionally verify the ZIP against its accompanying SHA256 checksum. Hosting,
code-signing and an independent clean-machine check are separate release steps.
The packaging process requires matching third-party sources, runtime grant
evidence and automated media/application smoke checks; see THIRD-PARTY.md.

CRTV Sim is MIT-licensed; bundled FFmpeg, ffprobe, mpv, Python and libraries retain
their separate terms in licenses/. The matching **ThirdParty-Sources.zip**
and its companion **ThirdParty-Build-Inputs.zip** are available from the same
GitHub Release as the Windows ZIP for source access and license compliance.
Neither archive is required to use the app. **SHA256SUMS.txt** is also optional;
it lets you verify the downloads if you choose.
A package named INTERNAL is an incomplete development candidate and must not be
published as a public release. Its manifest lists the remaining source evidence.
