# RetroWeather

The authoritative numbered development sequence is
[`RETROWEATHER_IMPLEMENTATION_PLAN.md`](../RETROWEATHER_IMPLEMENTATION_PLAN.md).
The broader phases in the design document do not renumber those steps.

RetroWeather is the standalone companion application that will generate the
synthetic Weather channel consumed by RetroTV. This directory intentionally has
its own Python package, commands, settings, tests, and release lifecycle.

The current implementation provides portable initialization, a localhost
status dashboard, strict normalized weather models, live Open-Meteo forecasts,
last-valid-snapshot fallback with visible fetch health, a deterministic
two-minute channel plan, and a validated static H.264/AAC forecast renderer
with atomic version publication and dashboard playback.

## Run from the source tree

From the repository root in PowerShell:

```powershell
$env:PYTHONPATH = 'RetroWeather/src'
python -m retroweather --home RetroWeather init
python -m retroweather --home RetroWeather serve
```

Open `http://127.0.0.1:8775/`. The `--home` directory is the portable
RetroWeather application-data root. It may be moved to an external drive or
set with the `RETROWEATHER_HOME` environment variable.

The dashboard supports guided location setup, a three-stage readiness
checklist, one-click first-channel creation, Fetch now, Render now, combined
Fetch + render, background render progress, validation and publication status,
and an embedded channel preview. Channel identity, units, forecast appearance,
soundtrack, and automatic refresh timing can be edited with inline validation
and per-section restore-default controls. Its live asset library inventories
supported backgrounds, motion media, music, and Halloween/Christmas artwork
without requiring a server restart. Expand the inventory to see dimensions,
duration, codec, validity, and renderer eligibility; provider, scheduler, and
storage diagnostics are collected at the bottom of the dashboard.

From October 1 through October 31, every forecast screen automatically uses
the Halloween palette and one validated transparent PNG/WebP border from
`retroweather-media/graphics/holidays/halloween/overlays`. The edge artwork is
placed above the weather background but below panels and text. Missing, opaque,
or damaged borders are skipped without disabling the seasonal colors or the
normal Weather channel.

From December 1 through December 25, the same automatic system uses an
evergreen, burgundy, gold, and cream Christmas palette plus one validated
transparent garland border from
`retroweather-media/graphics/holidays/christmas/overlays`. December 26 restores
the configured normal appearance. Christmas artwork uses the same safe
palette-only fallback as Halloween.

## Install in editable mode

```powershell
python -m venv RetroWeather/.venv
RetroWeather/.venv/Scripts/python -m pip install --no-deps -e RetroWeather
RetroWeather/.venv/Scripts/retroweather --home RetroWeather init
```

## Commands

```text
retroweather init          Create settings, media folders, cache, and output roots
retroweather location-search "Los Angeles, California"
                           Search Open-Meteo for matching locations
retroweather location-set 5368361
                           Persist an exact Open-Meteo location selection
retroweather fetch         Fetch, validate, and save current and forecast weather
retroweather show-weather  Validate and show the latest normalized snapshot
retroweather show-plan     Validate and show the planned two-minute timeline
retroweather render        Render, validate, and publish the forecast channel
retroweather serve         Run the localhost dashboard
```

Initialization never overwrites an existing settings file or user media.

## Asset library

Supported still images are PNG, JPG/JPEG, and WebP. Motion backgrounds are
MP4, MKV, and WebM. Music formats are MP3, WAV, M4A, AAC, FLAC, Ogg, and Opus.
The dashboard validates these files with FFprobe and marks damaged or
misplaced files ineligible.

## Music

Put general-purpose tracks in `retroweather-media/music/default`. Music in a
condition folder such as `music/conditions/rainy` takes priority whenever that
condition family appears in the channel; otherwise RetroWeather uses default
music, then valid AAC silence. Selection is deterministic for each generation.

The renderer loudness-normalizes the chosen track, mixes it at 28% by default,
crossfades repeats to cover the complete forecast loop, and fades the start and
finish. Use **Asset library → Enable soundtrack / Music volume**, save the
setting, then render again. **Active soundtrack** identifies the file used by
the published channel.

## Forecast appearance

All seven forecast screens use screen-specific translucent cards, consistent
television-safe margins, outlined high-contrast typography, and automatic
shrinking or ellipsis for unusually long labels. The channel uses crisp cuts
at exact segment boundaries so tuning and publication switching remain
frame-safe.

The `appearance` section of `config/settings.json` controls `panel_opacity`,
`panel_border_opacity`, `safe_margin`, `text_scale`, and these palette roles:
`header`, `panel`, `panel_alternate`, `primary`, `secondary`, `muted`, `accent`,
and `condition`. Colors accept six-digit `0xRRGGBB` or `#RRGGBB` values and
invalid values safely fall back to the built-in palette. The channel name is
read from `channel.display_name`. Changes apply to the next render.

## Condition artwork

Drop PNG, JPG, WebP, MP4, MKV, or WebM backgrounds into the matching folders under
`retroweather-media/graphics/conditions/<condition>/<day-or-night>/`, then
render the channel again. Each screen selects the condition and day/night
variant relevant to its forecast period. Missing condition artwork falls back
to `default/day` or `default/night`, then to the built-in plain background.
When a folder contains multiple assets, one is selected for each published
version and remains stable for the entire loop.

Only files that pass asset-library validation are eligible. Motion clips loop
when shorter than a screen, trim at the exact screen boundary, and contribute
video only; Weather channel audio is handled separately. Artwork center-crops
to 640×480 behind the solid title bar and localized translucent data cards.
There is no full-screen darkening wash. Forecast temperatures of at least
100°F/38°C receive an `EXTREME HEAT` badge, and temperatures of at most
32°F/0°C receive a `FREEZING` badge without changing the condition background.

## Condition classification

RetroWeather classifies each represented forecast period in this order:

1. Active `severe` or `extreme` alert → `alert`
2. WMO 95, 96, 99 → `stormy`
3. WMO 56, 57, 66, 67, 71, 73, 75, 77, 85, 86 → `snowy`
4. WMO 51, 53, 55, 61, 63, 65, 80, 81, 82 → `rainy`
5. WMO 45, 48, or visibility at/below 1 mile or 1.6 km → `foggy`
6. Wind at/above 25 mph or 40 km/h → `windy`
7. WMO 3 → `cloudy`
8. WMO 2 → `partly-cloudy`
9. WMO 0, 1 → `sunny`
10. Unknown or missing facts → `default`

Alerts only override periods during which they are active. Alert art is read
from `retroweather-media/graphics/alerts`; dedicated alert fetching and alert
screens arrive in Steps 25–26. In the dashboard, expand **Inspect screen theme
assignments** below the channel preview to see each screen's requested theme
and the artwork or built-in fallback selected for the active publication.

## CRTV Sim artwork and holiday integration

CRTV Sim's Library page provides **Weather Graphics** upload and AI generation
controls for every day/night condition, alerts, and each configured holiday.
Generated backgrounds are resized to 640×480 without cropping. Holiday layers
must be transparent still images with a mostly clear center for forecast data.
Uploaded backgrounds may also be MP4, MKV, or WebM clips. Multiple files rotate
through the existing renderer; artwork changes take effect on the next render.

When managed by CRTV Sim, `config/crtvsim-holidays.json` supplies holiday names,
inclusive dates, priorities, and enabled flags. Any configured holiday can use
`retroweather-media/graphics/holidays/<holiday>/overlays`. Cross-year ranges work;
the highest priority active holiday is selected, with slug order breaking ties.
The Weather dashboard's overlay readiness cards reflect this shared calendar.
An empty shared calendar disables seasonal artwork. Without the shared file,
standalone RetroWeather retains October Halloween and December 1–25 Christmas.
Saving Weather settings does not overwrite or copy the TV-owned calendar.

## Tests

From the repository root:

```powershell
$env:PYTHONPATH = 'RetroWeather/src'
python -m unittest discover -s RetroWeather/tests -v
```
