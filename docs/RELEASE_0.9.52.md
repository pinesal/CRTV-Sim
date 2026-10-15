# CRTV Sim 0.9.52 — Programming Inventory schedule coverage

- Upcoming now counts remaining program airings in active generated schedules for recurring and non-recurring channels, including holiday and weekly-special placements.
- Recurring Lineup has its own column for saved recurring assignments and holiday displacement status.
- Programs with no upcoming airing show Not scheduled. The No upcoming programs filter finds these entries; dates without generated schedules are not included.
- Count episode acts separated by commercial breaks as one airing, while counting separate repeats individually.
- Match physical media and logical placements so unrelated series folders and shadow collections do not borrow each other's counts. Include shared and external media.
- Refresh inventory after Dashboard schedule generation and changes to recurring preferences.
- Includes the Act Break review and quiet-pause fallbacks from 0.9.51, and reliable generic Up Next narration from 0.9.50.

## Updating

Stop CRTV Sim, then copy the contents of this portable build over the existing
application folder. Keep the existing `user-data` folder and media library in
place. Retain the previous portable folder until the updated build has been
checked on the target PC.

Use Refresh inventory after background maintenance creates schedules. Not scheduled
describes the currently generated schedules and does not predict later dates.
