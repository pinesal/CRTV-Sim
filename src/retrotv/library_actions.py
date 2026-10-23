"""Read-only Library heading counts, independent of table filters and pagination.

Do not build programming inventories, inspect media, or contact external sources
here: collapsed headings must remain inexpensive even on large libraries.
"""
from __future__ import annotations

import sqlite3

from retrotv.breakpoints import BreakpointAnalyzer
from retrotv.bumper_cues import bumper_review_catalog
from retrotv.config import AppConfig


def action_counts(connection: sqlite3.Connection, config: AppConfig) -> dict:
    def count(sql, values=()):
        return int(connection.execute(sql, values).fetchone()[0])

    library = count("SELECT COUNT(*) FROM assets WHERE review_status='open'")
    shadows = count("SELECT COUNT(*) FROM shadow_files WHERE status='review'")
    media = count("""
        SELECT COUNT(*) FROM assets a
        WHERE a.review_status='open'
           OR EXISTS (SELECT 1 FROM commercial_duration_reviews r
                      WHERE r.asset_id=a.id AND r.decision='pending')
           OR (a.integrity_status IN ('ready','not_scanned') AND (
               EXISTS (SELECT 1 FROM normalization_profiles n
                       WHERE n.asset_id=a.id AND n.analysis_status='review')
               OR EXISTS (SELECT 1 FROM audio_normalization_profiles n
                          WHERE n.asset_id=a.id AND n.analysis_status='review')))
    """)
    duplicates = count("""
        SELECT COUNT(*) FROM (
            SELECT h.original_sha256 FROM asset_content_hashes h
            JOIN assets a ON a.id=h.asset_id
            LEFT JOIN duplicate_reviews r ON r.original_sha256=h.original_sha256
            WHERE a.integrity_status IN ('ready','not_scanned')
            GROUP BY h.original_sha256
            HAVING COUNT(*)>1 AND NOT (
                COALESCE(r.status,'pending')='intentional'
                AND COALESCE(r.reviewed_member_count,0)=COUNT(*)))
    """)
    holidays = count("""
        SELECT COUNT(*) FROM holiday_filename_reviews r
        JOIN assets a ON a.id=r.asset_id WHERE r.status='pending'
    """)
    external = count("""
        SELECT COUNT(*) FROM external_sources s JOIN storage_roots r ON r.id=s.root_id
        WHERE r.enabled=1 AND (s.availability='unavailable' OR s.scan_status='error'
            OR COALESCE(s.scan_error,'')!='' OR EXISTS (
                SELECT 1 FROM external_assignments e WHERE e.root_id=r.id
                AND e.enabled=1 AND e.channel_slug='weather'))
    """)  # Reserved synthetic channel slug; do not resolve companion/network paths.
    analyzer = BreakpointAnalyzer(None, config)
    minimum = float(config.data.get('scheduling', {}).get('midroll_min_media_minutes', 15)) * 60
    breaks = connection.execute("""
        SELECT COALESCE(SUM(b.asset_id IS NULL OR b.status IN ('pending','error')
                   OR b.refresh_error IS NOT NULL OR b.source_signature !=
                      CAST(a.file_size AS TEXT)||':'||CAST(a.modified_ns AS TEXT)||?),0),
               COALESCE(SUM(EXISTS (SELECT 1 FROM asset_break_candidates c
                   WHERE c.asset_id=a.id AND c.selected=1
                     AND c.method IN ('midpoint','quiet_pause','scene_fallback'))),0)
        FROM assets a JOIN asset_media_info m ON m.asset_id=a.id
        LEFT JOIN asset_break_analyses b ON b.asset_id=a.id
        WHERE a.media_type IN ('episode','movie') AND m.duration_seconds>?
          AND a.enabled_for_scheduling=1 AND a.review_status='ready'
    """, (f':breaks-v{analyzer.ANALYSIS_VERSION}:{analyzer.policy_key}', minimum)).fetchone()

    def badge(value, label, detail):
        return dict(count=int(value), label=label, detail=detail)

    return {'counts': {
        'external-action-count': badge(external, 'need attention',
            'Enabled external sources with access, scan, or unsupported-assignment errors. Disconnected sources are excluded.'),
        'library-review-count': badge(media, 'review',
            'Unique files needing Library, long-commercial, aspect, or audio review; a file is counted once.'),
        'inventory-review-count': badge(library + shadows, 'review',
            'Library review files plus shadows needing review, matching Items needing review. Overlaps other panels.'),
        'bumper-review-count': badge(bumper_review_catalog(connection, config)['pending_count'], 'review',
            'Bumpers with an unmatched series clue.'),
        'shadow-review-count': badge(shadows, 'review', 'Shadow files needing review.'),
        'duplicate-review-count': badge(duplicates, 'review',
            'Pending duplicate sets, not individual copies. Intentional sets are excluded.'),
        'holiday-review-count': badge(holidays, 'review', 'Pending holiday filename suggestions.'),
        'break-analysis-count': badge(breaks[0], 'awaiting analysis',
            'Eligible programs awaiting automatic act-break analysis or retry. No manual action is required.'),
        'break-fallback-count': badge(breaks[1], 'fallback programs',
            'Programs with quiet-pause, scene-transition, or midpoint fallback cuts. Manual review is optional. May also await analysis.'),
    }}
