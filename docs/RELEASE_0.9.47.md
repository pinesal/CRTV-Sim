# RetroTV 0.9.47 — Public portable preparation

- First launch asks for library, timezone, display, processing and optional services,
  with a review step before any scanning. Interrupted setup resumes safely.
- Existing profiles skip setup, including in-place copy-over upgrades without a
  marker; settings, custom dimensions, media paths and inherited defaults survive.
- Independent video/audio enable switches and commercial trimming controls apply
  to automatic maintenance, manual actions and Internet Media readiness.
- Video ceiling presets plus Custom; preserve-source-aspect option; existing proxy
  and replacement policies exposed. Upscaling remains independently opt-in.
- OpenAI keys can be saved/replaced/removed in Settings via Credential Manager,
  with environment overrides retained and no secret returned to the browser.
- Clean ZIP includes empty Media/shared and _channel_template layouts, local setup,
  optional Weather, a redistributable font, update/privacy instructions, dependency
  inventory and SHA256 checksum. No user's profile or personal media is packaged.

This is a distribution candidate, not a signed/published public release. See the
remaining gates in NEXT_UPDATE_PLAN.md and THIRD-PARTY.md.

## Validation

- Full local regression suite: 499 tests passed, including scheduling, holiday
  pacing, guide, playback, Internet Media, credentials, normalization and shutdown.
- Final distribution checks: 20 tests passed, including damaged-profile recovery.
- Setup and Settings JavaScript passed Node syntax checks.
- Frozen first-run setup committed before database/scanner startup. An isolated
  legacy restart without a marker preserved a custom ceiling and unavailable
  media path. Dashboard and Safe Stop passed from a non-ASCII path with spaces.
- ZIP integrity, empty canonical template directories and absence of installed
  user-data were checked; the companion SHA256 verifies the final archive.

Independent clean-machine QA and licensing/signing/hosting decisions remain as
listed in NEXT_UPDATE_PLAN.md; no production installation was replaced.
