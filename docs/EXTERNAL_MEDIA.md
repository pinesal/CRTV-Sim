# External Media — Phase 1A

External series use the existing SQLite asset catalog, eligibility system,
random/recurring scheduler, Guide and player. They do not require a media server
or a second database. Register one series directory at a time in **Library &
Schedules → External Media Sources**. Supply the series title; Season/Episode
filename recognition orders files but is not a requirement for importing them.

## Physical files and logical programming

`storage_roots.source_kind` records immutable external ownership. Managed roots
retain their configuration-defined paths; registered external roots store an
absolute path. Each asset keeps a root-relative physical path. `external_sources`
stores the title, availability and scan status; `external_assignments` maps that
root to explicit channel/daypart scopes. Eligibility expansion uses the existing
exact, daytime and all-day rules. No shadow files or virtual directory trees are
created in the source library.

`asset_paths.resolve_asset_path` resolves an indexed asset ID through its root,
validates containment and supplies scheduled playback, streaming previews,
read-only fingerprinting and act-break analysis. Logical placement paths are
used only for existing scheduling scope interpretation, never for file access.

## Read-only boundaries

External assets never receive automatic video/audio correction preparation.
Normalization, promotion, restore/re-review, commercial trimming and holiday
moves reject external IDs in their service methods, regardless of the caller.
File replacement and unlink helpers check registered external directories again
immediately before modifying a file, including managed aliases and recovery.
Generated-file cleanup also refuses deletion inside registered external roots.
Disconnecting sources and deleting channel assignments do not delete source
files. There is no setting that enables external normalization.

Sources may not overlap one another, managed roots, or application data roots.
Absolute and traversing asset-relative paths are refused. Root junctions and
symlinks resolve to their physical destination before overlap checks. Nested
directory junctions/symlinks are not traversed; register the actual series root.
Escaping file aliases are reported rather than indexed.

## Scanning and outages

An explicit scan marks only that source pending. The next maintenance pass scans
pending sources; maintenance does not rescan every source on every run. By
default, ordinary readiness rescans enabled sources every 168 hours. Configure
`[external_media].rescan_interval_hours` in the profile configuration to change
that interval. Probing and lightweight size/head/tail fingerprints do not require
hashing every byte of the library. Unchanged files retain IDs. A unique matching
fingerprint/size with an absent original path can retain an ID after a move within
the registered root. Ambiguous matches retain the missing record and create a new
asset rather than guessing.

Unavailable roots retain their catalog and assignments but are excluded from
new candidate selection until access returns. Cancelled, failed or partial scans
do not reconcile unvisited records as missing. A complete successful scan marks
genuinely absent files missing without deleting their records. Housekeeping
retains external assets. Restarting and reconnecting do not reset series progress.

Source/assignment changes affect channel fingerprints. Existing readiness
rebuilds unlocked schedules chronologically; history and locked schedules stay
protected. Missing scheduled files use existing continuity fallback without
shifting later programming. If an existing folder is already registered, edit
its assignments instead of importing it again; reconnecting a disconnected root
reuses its records.

## API and current limitations

- `GET /api/external-media`: sources, scheduled channels and daypart choices.
- `POST /api/external-media`: register with `path`, `series_title`, and
  `assignments: [{channel, scope}]`; optional `new_channel`, `channel_number`,
  and `scope` create a scheduled channel.
- `POST /api/external-media/{root_id}`: edit title/assignments or set `enabled`.
- `POST /api/external-media/{root_id}/rescan`: queue a scan.
- Existing indexed-asset playback endpoints stream through the root resolver.

The application runtime returns queued operations; the Dashboard shows pending,
running, completed, partial, cancelled and error scan states. Maintenance may
defer work while another exclusive operation is active. Network paths and mapped
drives must be available to the Windows account running CRTV Sim, not merely the
computer displaying its Dashboard.

Phase 1A imports episodes for scheduled channels. At Your Pace assignments and
switching an externally assigned channel to that mode are refused until the
assignments are disconnected. The existing Internet Media playlist/progress
model is untouched; external integration belongs to Phase 1B. Bulk multi-series
discovery, external holiday/weekly assignments, other media types, and media
server connectors remain future work.
