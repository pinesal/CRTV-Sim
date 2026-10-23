# IPTV live feeds

CRTV Sim can receive a live feed and show it on a scheduled channel. The PC
still owns the channel list, timetable, Guide and playback. This does not turn
CRTV Sim into an IPTV server or broadcast your channels over the network.

## Add and test a feed

Open **Internet Media → IPTV Live Feeds**. Enter a direct HTTP/HTTPS HLS
or MPEG-TS URL, or an extended M3U channel-list URL. Choose **Inspect URL**, then
select one feed. HLS quality variants and video segments are treated as one
feed. Login pages, provider authentication, UDP/RTSP, remote EPG, recording and
timeshift are not supported in this first version.
Mixed playlists can still be imported: unsupported or invalid entries are skipped
and their count is shown, while supported HTTP/HTTPS entries remain selectable.

Choose an existing scheduled channel, or enter a new channel name and optional
unused number. Add assignments to dayparts, `daytime`, `all-day`, or an existing
weekly special. A feed can have several assignments across several channels.
Optional assignment labels override its default Guide name.
Channel and daypart choices load when the page opens. Under **Assign to existing
channels**, choose **Existing channel** and **Daypart or weekly special**. Entering
a new channel name instead shows **New channel daypart**; this defaults to
`all-day` and can be changed before saving. If options cannot load, use **Refresh
status** to retry. The form will not silently save a feed without a destination.
Assignment choices and saved feeds load independently of schedule-conflict
diagnostics. A slow conflict check does not prevent assigning or saving a feed.
Options requests time out after ten seconds with a visible retry message;
**Refresh status** can also cancel a stalled request and try again immediately.
Incomplete conflict checks are reported separately and never mean "no conflicts."

Choose **Test selected feed** before saving. A separate mpv window opens
**on the PC running CRTV Sim**, even when you use the Dashboard from another
device. The test checks decoded, advancing video and reports whether an audio
track is present without stopping television. Once video decodes, **Add feed**
becomes available. It closes after 30 seconds or when you choose **Close preview**;
closing a successful preview keeps the result available for saving.

Testing creates no source records, channels or channel folders. New channel
creation requires a successful test on the server as well as in the interface.
Changing the source URL, direct/playlist mode or selected feed requires another
test. Results expire after 30 minutes or an application restart. A failed or
cancelled test cannot authorize channel creation. **Direct stream** skips playlist
inspection, but still requires the playback test. Already saved feeds retain
**Test feed** for troubleshooting.

Guide, Weather and At Your Pace channels are excluded. Remove IPTV assignments
before changing a scheduled channel to At Your Pace. IPTV does not change the
existing Internet Media downloading, playlist or saved-progress behavior.

## Which source plays?

Fixed weekly specials take priority. Outside those windows, an exact daypart
feed overrides `daytime`, which overrides `all-day`. Equally specific competing
feeds cause a configuration conflict. When a special ends, the applicable live
feed resumes at its current live position.

A channel-specific local episode, short, movie or complete block eligible in
the feed's active window also causes a conflict. This includes broad daypart
placements, external assignments, channel-specific holiday programs, and channel
shadows—even when a shadow's video is physically stored in shared.

**Shared program pools never conflict with IPTV and never play during live
windows.** Shared holiday programming is also excluded. Shared programming remains
available in locally scheduled windows. Supporting commercials, bumpers, graphics,
music and filler can stay configured; they neither conflict nor get inserted into
a live feed. A conflict or outage never silently switches to local/shared media.

The Dashboard lists affected channel/scope windows and conflicting placements.
Fix their assignments or move/disable the channel-specific local programming,
then use **Scan & update now** if readiness is not already running. Schedule
changes rebuild affected unlocked dates; locked schedules and history are kept.
Recurring local episode progress does not advance during live/conflict windows.

## Transitions and outages

CRTV Sim cuts the outgoing source at the PC's scheduled boundary. The incoming
network source can take time to buffer. The channel's configured interstitial
graphic covers connection and recovery, using the existing holiday, weekly-special
and daypart selection rules. This is a silent still frame (the first frame for
video interstitials); standard continuity bars remain the fallback when no usable
graphic is available. Preparing or reading a graphic never delays opening the feed.
The cover fits the window and clears when decoded video is available.
Live seeking and pause are disabled. An unchanged feed continues across
adjacent scopes and midnight without reconnecting.

### Fit a live feed to a 4:3 CRT

Each feed has a **Presentation** selector, both before testing a new feed and
on its saved card:

- **Preserve original** (default): keep the stream's aspect ratio.
- **Fill 4:3 — center crop**: trim the sides of widescreen video without
  stretching. Native 4:3 remains intact; narrower video is cropped at the top
  and bottom to fill 4:3. Edge logos and subtitles can be cut off.
- **Stretch to 4:3**: keep the full frame but change its proportions to 4:3.

These affect live playback only, with no download, re-encoding or modification
of the source stream. mpv scales the result to the display. Crop calculations
respect pixel aspect ratios and adapt when a provider changes video format,
such as at an advertisement. This is a centered crop, not face tracking or
continuous black-bar detection.

The separate feed test uses the selected presentation. Changing it before
registration requires another successful test. For a saved feed, **Test feed**
previews the edited choice; **Save** applies it to TV playback, including an
already-playing feed without reconnecting. Presentation is shared wherever
that feed is assigned. Existing feeds retain Preserve original. Local programs,
channel graphics, normalization and scheduling progress are unaffected.

The final 30 seconds before a transition to locally scheduled programming can
show Up Next when that programming appears in the Guide. This uses the upcoming
programming's graphics/profile and existing AI, generic-recording and visual
fallback rules. Feed-to-feed transitions do not add Up Next.

Connection and sustained-stall limits are 15 seconds. Recovery retries after
5, 15, 30, then 60 seconds, cancelling when the channel/window changes. A recovered
feed returns to live playback. Repeated failures do not stop the TV player.

Active playlists refresh periodically (no more often than every 15 minutes
outside recovery) and can be refreshed manually. A unique playlist channel ID
allows its URL to change without losing the selection; otherwise the exact URL
identifies it. Reordering cannot choose a different channel. Confirmed removal
shows **Stream not found**; ambiguous entries require reselection. Temporary
playlist-fetch failures preserve the saved selection and last known URL.

Source URLs may contain private query tokens. Treat access to the Dashboard and
its configuration as access to those URLs. No IPTV source is normalized, trimmed,
copied into the media library, or assigned episode progress.

## Implementation interfaces

The `iptv_sources`, `iptv_feeds` and `iptv_assignments` tables are independent of
local assets and Internet Media. Scheduled `iptv_live` / `iptv_conflict` entries
carry their ownership and diagnostics in explanation metadata. Connection health
does not invalidate schedules; assignment and enable/disable changes do.

Dashboard interfaces are `GET /api/iptv`, `POST /api/iptv/inspect`, `POST /api/iptv`,
`POST /api/iptv/{id}`, and feed actions `refresh`, `test`, `remove`.
`GET /api/iptv/preview` reports the independent test and
`POST /api/iptv/preview-test` tests an unsaved URL/selection without registration.
Its server-issued `test_id` accompanies registration; new channels require a
matching successful test. `POST /api/iptv/preview-stop` closes the preview. Feed actions use saved identities; arbitrary
player commands and filesystem paths are not accepted.
