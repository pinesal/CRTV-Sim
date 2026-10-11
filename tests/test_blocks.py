import shutil
import unittest
import uuid
from pathlib import Path

from retrotv.blocks import BlockService
from retrotv.db import apply_migrations, connect_database, ensure_storage_root, utc_now


class BlockServiceTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(__file__).resolve().parents[1]
        root = self.project / ".test-tmp" / f"blocks-{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        self.root = root
        self.connection = connect_database(root / "retrotv.sqlite3")
        apply_migrations(self.connection, self.project / "migrations")
        storage_id = ensure_storage_root(self.connection, "media")
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO assets(
                id, source_root_id, source_relative_path, original_filename,
                file_size, modified_ns, media_type, display_title, channel_slug,
                daypart_slug, metadata_status, integrity_status, review_status,
                enabled_for_scheduling, first_seen_at, last_seen_at
            ) VALUES (
                1, ?, 'fox/evening/blocks/test.mp4', 'test.mp4',
                1, 1, 'complete_block', 'Test Block', 'fox', 'evening',
                'ready', 'ready', 'ready', 1, ?, ?
            )
            """,
            (storage_id, now, now),
        )
        self.connection.execute(
            "INSERT INTO asset_media_info(asset_id, duration_seconds) VALUES (1, 3600)",
        )
        self.connection.commit()
        self.service = BlockService(self.connection)

    def tearDown(self):
        self.connection.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_replace_cues_orders_and_persists_valid_sheet(self):
        cues = self.service.replace_cues(
            1,
            [
                {
                    "offset": "00:10:00",
                    "end": "00:15:00",
                    "cue_type": "commercial_break",
                    "title": "First commercial break",
                },
                {
                    "offset": "00:00:00",
                    "end": "00:10:00",
                    "cue_type": "program",
                    "title": "Opening Show",
                },
            ],
        )
        self.assertEqual([cue.offset_ms for cue in cues], [0, 600000])
        rows = self.service.cues(1)
        self.assertEqual([row["title"] for row in rows], [
            "Opening Show",
            "First commercial break",
        ])

    def test_rejects_overlapping_or_out_of_range_cues(self):
        with self.assertRaisesRegex(ValueError, "overlaps"):
            self.service.replace_cues(
                1,
                [
                    {
                        "offset_ms": 0,
                        "end_offset_ms": 10000,
                        "cue_type": "program",
                        "title": "One",
                    },
                    {
                        "offset_ms": 9000,
                        "end_offset_ms": 20000,
                        "cue_type": "program",
                        "title": "Two",
                    },
                ],
            )
        with self.assertRaisesRegex(ValueError, "outside"):
            self.service.replace_cues(
                1,
                [
                    {
                        "offset_ms": 3600000,
                        "cue_type": "program",
                        "title": "Too late",
                    }
                ],
            )


if __name__ == "__main__":
    unittest.main()
