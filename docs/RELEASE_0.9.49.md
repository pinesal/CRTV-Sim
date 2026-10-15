# CRTV Sim 0.9.49 — Weather rendering and playback improvements

- Fix Weather channel rendering with the bundled FFmpeg 9 by using its supported filter-graph file-loading syntax. A rendering failure now stops relying on options removed from FFmpeg.
- Remember manually selected audio and subtitle tracks per series across episodes, channels, and restarts. Track choices are matched by language and title when track numbers change.
- Keep the Dashboard's channel selection stable while live status refreshes.
- Includes scheduled external media support introduced in 0.9.48.

## Updating

Stop CRTV Sim, then copy the contents of this portable build over the existing
application folder. Keep the existing `user-data` folder and media library in
place. Retain the previous portable folder until the updated build has been
checked on the target PC.

## Validation

The portable build and bundled media tools are validated by the release build
workflow. See the matching checksum and source archives distributed beside the
Windows ZIP.
