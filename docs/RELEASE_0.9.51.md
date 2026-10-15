# CRTV Sim 0.9.51 — Act Break review and quiet-pause fallbacks

- Act Breaks titles include the series name when known and link to the full video for manual review.
- When stronger natural cues are absent, enabled fallbacks try a fully measured quiet pause of at least 0.5 seconds below -35 dB before using the generic midpoint.
- Show quiet-pause evidence, add All fallback breaks and Quiet-pause fallback filters, and keep Generic halfway fallback for midpoint-only results.
- Refresh eligible analyses through bounded maintenance, prioritizing upcoming scheduled media. Existing source-matching version 5 cuts remain usable while queued, and manual overrides survive reanalysis.
- Includes the reliable Up Next generic narration improvements from 0.9.50.

## Updating

Stop CRTV Sim, then copy the contents of this portable build over the existing
application folder. Keep the existing `user-data` folder and media library in
place. Retain the previous portable folder until the updated build has been
checked on the target PC.

After installation, maintenance will reanalyze existing eligible programs in
the configured batches. Use Analyze in Act Breaks to process a particular file
sooner. Quiet-pause selections remain labeled as fallbacks for optional review.
