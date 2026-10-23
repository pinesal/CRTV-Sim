import io
import json
import sqlite3
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from retrotv.iptv import IPTVService
from retrotv.scheduling import ScheduleBuilder
from retrotv.web import make_handler
import test_scheduling as fixtures


class IPTVOptionsTests(unittest.TestCase):
    setUp = fixtures.SchedulingTests.setUp
    tearDown = fixtures.SchedulingTests.tearDown

    def service(self):
        self.channel_id = ScheduleBuilder(self.connection, self.config).ensure_channel('fox')
        return IPTVService(self.connection, self.config)

    def generation(self, day, status='active'):
        row = self.connection.execute('''INSERT INTO schedule_generations(
            channel_id,schedule_date,seed,configuration_fingerprint,status,generated_at)
            VALUES(?,?,'seed','config',?,'now')''', (self.channel_id, day, status))
        self.connection.commit()
        return row.lastrowid

    def entries(self, generation, number, start, end, kind='commercial'):
        self.connection.executemany('''INSERT INTO schedule_entries(
            generation_id,channel_id,starts_at,ends_at,entry_type,title,daypart_slug,sequence_position)
            VALUES(?,?,?,?,?,'Test','morning',?)''',
            ((generation, self.channel_id, start, end, kind, index) for index in range(number)))
        self.connection.commit()

    def test_assignment_catalog_cannot_read_schedule_history(self):
        service = self.service()
        self.config.data['scheduling']['dayparts'][0]['slug'] = 'breakfast'
        def authorize(action, table, *_):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table in {'schedule_entries', 'schedule_generations'} else sqlite3.SQLITE_OK
        self.connection.set_authorizer(authorize)
        try:
            with patch.object(service, 'conflict_status', side_effect=AssertionError('Slow diagnostics called')):
                result = service.overview(include_conflicts=False)
        finally:
            self.connection.set_authorizer(None)
        self.assertEqual(result['channels'][0]['slug'], 'fox')
        self.assertIn('breakfast', result['scopes'])
        self.assertIn('all-day', result['scopes'])
        self.assertFalse(self.connection.in_transaction)

    def test_conflicts_skip_populated_history_using_existing_indexes(self):
        service = self.service()
        old = self.generation('2010-01-01')
        self.entries(old, 10000, '2010-01-01T00:00:00+00:00', '2010-01-01T00:01:00+00:00')
        # The former query walked all historical entries, even with no conflicts.
        steps = []
        self.connection.set_progress_handler(lambda: steps.append(1) and 0, 1000)
        self.connection.execute("""SELECT se.id FROM schedule_entries se
            JOIN schedule_generations g ON g.id=se.generation_id
            WHERE g.status='active' AND se.entry_type='iptv_conflict'
              AND se.ends_at>? ORDER BY se.starts_at LIMIT 100""", (datetime.now(UTC).isoformat(),)).fetchall()
        self.connection.set_progress_handler(None, 0)
        self.assertGreater(len(steps), 20)
        now = datetime.now(UTC)
        local = now.astimezone(ZoneInfo('America/Los_Angeles'))
        current = self.generation(local.date().isoformat())
        self.entries(current, 1, (local - timedelta(minutes=1)).isoformat(),
                     (local + timedelta(minutes=10)).isoformat(), 'iptv_conflict')
        with patch('retrotv.iptv.time.monotonic', wraps=time.monotonic) as clock:
            result = service.conflict_status()
        self.assertTrue(result['complete'])
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['slug'], 'fox')
        # Calls occur at deadline setup and every 1,000 VM operations. This
        # deterministic bound proves the 10,000 old rows were not traversed.
        self.assertLess(clock.call_count, 10)

    def test_expired_budget_and_shutdown_do_not_claim_no_conflicts(self):
        service = self.service()
        result = service.conflict_status(timeout_seconds=0)
        self.assertFalse(result['complete'])
        self.assertIn('retry', result['detail'])
        cancelled = threading.Event()
        cancelled.set()
        self.assertFalse(service.conflict_status(cancel_event=cancelled)['complete'])

    def test_expensive_query_is_interrupted_and_connection_is_reusable(self):
        service = self.service()
        tomorrow = datetime.now(UTC) + timedelta(days=1)
        generation = self.generation(tomorrow.date().isoformat())
        self.entries(generation, 10000, tomorrow.isoformat(),
                     (tomorrow + timedelta(minutes=1)).isoformat())
        with patch('retrotv.iptv.time.monotonic', side_effect=[0.0, 0.0, 3.0]):
            result = service.conflict_status(timeout_seconds=2)
        self.assertFalse(result['complete'])
        self.assertFalse(self.connection.in_transaction)
        # A lingering progress handler would interrupt these subsequent reads.
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM schedule_entries').fetchone()[0], 10000)
        self.assertTrue(service.conflict_status()['complete'])

    def test_fast_options_endpoint_and_separate_diagnostics_keep_legacy_api(self):
        self.service()
        handler_type = make_handler(self.config)
        def get(path):
            handler = object.__new__(handler_type)
            handler.path, handler.command, handler.wfile = path, 'GET', io.BytesIO()
            handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
            handler.do_GET()
            self.assertEqual(handler.send_response.call_args.args[0], 200)
            return json.loads(handler.wfile.getvalue())
        with patch('retrotv.iptv.IPTVService.conflict_status', side_effect=AssertionError('Options waited for diagnostics')):
            self.assertIn('morning', get('/api/iptv/options')['scopes'])
        status = {'items': [], 'complete': False, 'detail': 'Retry diagnostics'}
        with patch('retrotv.iptv.IPTVService.conflict_status', return_value=status):
            self.assertEqual(get('/api/iptv/conflicts'), status)
            legacy = get('/api/iptv')
        self.assertIn('channels', legacy)
        self.assertEqual(legacy['conflicts'], [])
        self.assertFalse(legacy['conflicts_complete'])
        self.assertEqual(legacy['conflicts_detail'], 'Retry diagnostics')


if __name__ == '__main__':
    unittest.main()
