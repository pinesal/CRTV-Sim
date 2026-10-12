# CRTV Sim 0.9.48 — scheduled external media

- Register an existing series folder and assign it to one or more scheduled channels and dayparts without moving or copying media.
- Scan season folders, preserve asset identities across unchanged rescans and confident moves, and retain source history during outages.
- Use external episodes in random and recurring schedules with the existing eligibility, guide, player, and continuity behavior.
- Keep registered external media read-only across dashboard actions, automatic maintenance, source recovery, and file cleanup.
- Phase 1A supports scheduled channels. At Your Pace integration remains deferred to Phase 1B; its existing Internet Media playlist and progress are preserved.

## Validation

- Full unittest suite: 545 tests passed, one skipped.
- External media and shadow regression tests passed; Dashboard scripts passed Node syntax checks.
- Internal test build. This build script supports `-SkipArchive` to create the portable folder without a ZIP.
