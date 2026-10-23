import json
import sqlite3
import threading
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

from retrotv.channel_jobs import ChannelDeletionQueue
from retrotv.db import connect_database
from retrotv.runtime import ApplicationRuntime
from retrotv.web import make_handler
import test_channel_deletion as deletion_fixtures
import test_graphics as fixtures


class ChannelQueueTests(unittest.TestCase):
    tearDown = fixtures.GraphicsTests.tearDown
    request = deletion_fixtures.ChannelDeletionAPITests.request
    channel = deletion_fixtures.ChannelDeletionAPITests.channel

    def setUp(self):
        fixtures.GraphicsTests.setUp(self)
        for name in ('state', 'normalized', 'previews', 'originals_backup'):
            self.config.data['storage_roots'][name] = str(self.root / name)
        self.channel()
        self.runtime = ApplicationRuntime(self.config)

    def enqueue(self):
        with patch.object(self.runtime, '_start_channel_deletions'):
            return self.runtime.request_channel_deletion('cbs-news',
                confirmation_text='DELETE CBS NEWS', acknowledge_rescan=True)['job']

    def test_busy_api_accepts_durably_and_retry_completes_after_writer_released(self):
        with closing(connect_database(self.config.database_path)) as blocker:
            blocker.execute('UPDATE channels SET display_name=display_name WHERE id=1')
            with patch.object(self.runtime, '_start_channel_deletions'):
                status, response = self.request(make_handler(self.config, self.runtime),
                    '/api/channels/cbs-news/delete', {'confirmation_text':'DELETE CBS NEWS', 'acknowledge_rescan':True})
            self.assertEqual(status, 202)
            self.assertTrue(response['queued'])
            job = response['job']
            self.assertEqual(json.loads(self.runtime.channel_deletions.path.read_text())[0]['id'], job['id'])
            self.runtime._perform_channel_deletion(job)
            waiting = self.runtime.channel_deletions.pending()[0]
            self.assertEqual(waiting['state'], 'waiting')
            self.assertIn('retry automatically', waiting['detail'])
            # Restart recovers the same confirmed request, not a fresh confirmation.
            self.runtime.channel_deletions = ChannelDeletionQueue(self.config)
            self.assertEqual(self.runtime.channel_deletions.pending()[0]['id'], job['id'])
            blocker.rollback()
            self.runtime._perform_channel_deletion(self.runtime.channel_deletions.pending()[0])
        self.assertEqual(self.runtime.channel_deletions.snapshot()['jobs'][0]['state'], 'completed')
        self.assertIsNone(self.connection.execute("SELECT id FROM channels WHERE slug='cbs-news'").fetchone())
        self.assertIsNotNone(self.connection.execute("SELECT id FROM channels WHERE slug='nickelodeon'").fetchone())
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM iptv_assignments').fetchone()[0], 1)
        self.connection.execute('BEGIN IMMEDIATE')
        self.connection.rollback()

    def test_confirmation_and_deduplication_and_replaced_channel_identity(self):
        with self.assertRaisesRegex(ValueError, 'exactly'):
            self.runtime.request_channel_deletion('cbs-news', confirmation_text='bad', acknowledge_rescan=True)
        job = self.enqueue()
        self.assertEqual(self.enqueue()['id'], job['id'])
        self.connection.execute("DELETE FROM channels WHERE slug='cbs-news'")
        self.connection.execute("INSERT INTO channels(id,slug,display_name) VALUES(2,'cbs-news','CBS NEWS')")
        self.connection.commit()
        self.runtime._perform_channel_deletion(job)
        record = self.runtime.channel_deletions.snapshot()['jobs'][0]
        self.assertEqual(record['state'], 'failed')
        self.assertIn('replaced', record['error'])
        self.assertIsNotNone(self.connection.execute("SELECT id FROM channels WHERE slug='cbs-news'").fetchone())

    def test_current_channel_waits_without_stopping_playback_and_shutdown_keeps_request(self):
        job = self.enqueue()
        self.runtime.controller = SimpleNamespace(channel_slug='cbs-news')
        self.runtime.requested_channel = 'cbs-news'
        self.runtime._perform_channel_deletion(job)
        self.assertEqual(self.runtime.channel_deletions.pending()[0]['state'], 'waiting')
        self.assertIn('another channel', self.runtime.channel_deletions.pending()[0]['detail'])
        self.assertIsNotNone(self.runtime.controller)
        self.runtime.stop_event.set()
        self.runtime.controller = None
        self.runtime._perform_channel_deletion(job)
        self.assertEqual(len(ChannelDeletionQueue(self.config).pending()), 1)

    def test_worker_retries_automatically_and_shutdown_joins_it(self):
        self.enqueue()
        waiting = threading.Event()
        original = self.runtime.channel_deletions.update
        def update(*args, **kwargs):
            original(*args, **kwargs)
            if 'database writer' in kwargs.get('detail', ''):
                waiting.set()
        with closing(connect_database(self.config.database_path)) as blocker:
            blocker.execute('UPDATE channels SET display_name=display_name WHERE id=1')
            with patch.object(self.runtime.channel_deletions, 'update', side_effect=update):
                self.runtime._start_channel_deletions()
                worker = self.runtime.channel_deletion_thread
                self.assertTrue(waiting.wait(4))
                blocker.rollback()
                worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.runtime.channel_deletions.snapshot()['jobs'][0]['state'], 'completed')
        self.assertFalse(self.runtime.internet_pause_event.is_set())

    def test_cancel_pending_and_stale_preview_never_delete_channel(self):
        job = self.enqueue()
        with self.assertRaisesRegex(ValueError, 'Cancel the queued deletion'):
            self.runtime.recreate_channel_folders('cbs-news', job['channel_identity'])
        status, result = self.request(make_handler(self.config, self.runtime),
            f"/api/channels/deletions/{job['id']}/cancel", {})
        self.assertEqual(status, 200)
        self.assertTrue(result['cancelled'])
        self.runtime._perform_channel_deletion(job)
        self.assertIsNotNone(self.connection.execute("SELECT id FROM channels WHERE slug='cbs-news'").fetchone())
        with self.assertRaisesRegex(ValueError, 'preview'):
            self.runtime.request_channel_deletion('cbs-news', confirmation_text='DELETE CBS NEWS',
                acknowledge_rescan=True, expected_identity='old')

    def test_background_job_defers_without_taking_database_writer(self):
        self.enqueue()
        alive = threading.Event()
        self.runtime.maintenance_thread = SimpleNamespace(is_alive=lambda: True)
        original = self.runtime.channel_deletions.update
        def update(*args, **kwargs):
            original(*args, **kwargs)
            alive.set()
        with patch.object(self.runtime.channel_deletions, 'update', side_effect=update):
            self.runtime._start_channel_deletions()
            worker = self.runtime.channel_deletion_thread
            self.assertTrue(alive.wait(3))
            self.runtime.stop_event.set()
            worker.join(3)
        self.assertEqual(self.runtime.channel_deletions.pending()[0]['state'], 'waiting')
        self.assertEqual(self.runtime.channel_deletions.pending()[0]['attempts'], 0)

    def test_unreadable_queue_never_silently_drops_confirmed_requests(self):
        self.enqueue()
        self.runtime.channel_deletions.path.write_text('broken')
        queue = ChannelDeletionQueue(self.config)
        self.assertIn('Cannot read', queue.snapshot()['error'])
        with self.assertRaises(ValueError):
            queue.enqueue({'confirmation_text':'DELETE X'}, confirmation_text='DELETE X', acknowledge_rescan=True)
        self.assertEqual(queue.path.read_text(), 'broken')

    def test_request_arriving_as_worker_exits_starts_another_worker(self):
        self.enqueue()
        original_pending = self.runtime.channel_deletions.pending
        original_update = self.runtime.channel_deletions.update
        submitted = threading.Event()
        completed = threading.Event()
        def pending():
            result = original_pending()
            if (not result and not submitted.is_set()
                    and threading.current_thread().name == 'crtv-channel-deletions'):
                submitted.set()
                self.runtime.request_channel_deletion('nickelodeon',
                    confirmation_text='DELETE Nickelodeon', acknowledge_rescan=True)
            return result
        def update(job_id, **changes):
            original_update(job_id, **changes)
            if changes.get('state') == 'completed' and changes['result']['deleted'] == 'nickelodeon':
                completed.set()
        try:
            with patch.object(self.runtime.channel_deletions, 'pending', side_effect=pending), patch.object(
                    self.runtime.channel_deletions, 'update', side_effect=update):
                self.runtime._start_channel_deletions()
                self.assertTrue(completed.wait(7))
        finally:
            self.runtime.stop_event.set()
            worker = self.runtime.channel_deletion_thread
            if worker:
                worker.join(3)
        self.assertEqual([job['state'] for job in self.runtime.channel_deletions.snapshot()['jobs']], ['completed', 'completed'])


if __name__ == '__main__':
    unittest.main()
