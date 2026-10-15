# CRTV Sim 0.9.50 — Reliable Up Next narration fallback

- Use successfully generated AI narration when the existing eligibility rules allow it, then use a valid prerecorded generic announcement, and use music-only video only when neither narration option is usable.
- Keep a working generic recording available while profile settings change or replacement generation is running or fails. Publish a replacement only after it is validated.
- Make a channel's generic recording available across dayparts and weekly Up Next screens, and let an unusable scoped recording fall through to the channel recording.
- Refresh cached Up Next videos when the available narration changes. Keep generic recordings out of automatic cleanup.
- Show whether a prepared Up Next screen uses AI narration, generic narration, or music-only video, including the fallback reason.

## Updating

Stop CRTV Sim, then copy the contents of this portable build over the existing
application folder. Keep the existing `user-data` folder and media library in
place. Retain the previous portable folder until the updated build has been
checked on the target PC.
