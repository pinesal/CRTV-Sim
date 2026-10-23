import io
import json
import sqlite3
import threading
import unittest
from unittest.mock import Mock, patch

from retrotv.db import connect_database
from retrotv.iptv import IPTVService
from retrotv.web import make_handler
import test_graphics as fixtures


class ChannelDeletionAPITests(unittest.TestCase):
    setUp = fixtures.GraphicsTests.setUp
    tearDown = fixtures.GraphicsTests.tearDown

    def channel(self):
        self.connection.execute("INSERT INTO channels(id,slug,display_name,enabled) VALUES(2,'cbs-news','CBS NEWS',1)")
        self.connection.commit()
        return IPTVService(self.connection, self.config).register({
            'url': 'http://lan/live.ts', 'direct': True,
            'assignments': [{'channel': 'cbs-news', 'scope': 'all-day'},
                            {'channel': 'nickelodeon', 'scope': 'morning'}]})['id']

    def request(self, handler_type, path, payload=None):
        handler = object.__new__(handler_type)
        handler.path, handler.command = path, 'GET' if payload is None else 'POST'
        body = json.dumps(payload).encode() if payload is not None else b''
        handler.headers = {'Content-Length': str(len(body))}
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
        (handler.do_GET if payload is None else handler.do_POST)()
        return handler.send_response.call_args.args[0], json.loads(handler.wfile.getvalue())

    def test_preview_is_read_only_during_writer_and_busy_delete_can_be_retried(self):
        feed = self.channel()
        handler = make_handler(self.config)
        blocker = connect_database(self.config.database_path)
        self.addCleanup(blocker.close)
        blocker.execute('UPDATE channels SET display_name=display_name WHERE id=1')
        opened = []
        def connect(path):
            connection = connect_database(path)
            connection.execute('PRAGMA busy_timeout=50')
            opened.append(connection)
            return connection
        payload = {'confirmation_text': 'DELETE CBS NEWS', 'acknowledge_rescan': True}
        with patch('retrotv.web.connect_database', side_effect=connect):
            status, preview = self.request(handler, '/api/channels/cbs-news/deletion-preview')
            self.assertEqual(status, 200)
            self.assertEqual(preview['slug'], 'cbs-news')
            status, result = self.request(handler, '/api/channels/cbs-news/delete', payload)
            self.assertEqual(status, 409)
            self.assertTrue(result['retryable'])
            self.assertIn('background work', result['error'])
            blocker.rollback()
            status, result = self.request(handler, '/api/channels/cbs-news/delete', payload)
        self.assertEqual(status, 200)
        self.assertEqual(result['deleted'], 'cbs-news')
        # Reused feeds and another channel's placement survive channel deletion.
        self.assertIsNotNone(self.connection.execute('SELECT id FROM iptv_feeds WHERE id=?', (feed,)).fetchone())
        assignments = self.connection.execute('SELECT channel_slug,scope FROM iptv_assignments WHERE feed_id=?', (feed,)).fetchall()
        self.assertEqual([tuple(row) for row in assignments], [('nickelodeon', 'morning')])
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')

    def test_shutdown_rejects_deletion_before_opening_database(self):
        self.channel()
        runtime = Mock()
        runtime.stop_event = threading.Event()
        runtime.stop_event.set()
        with patch('retrotv.web.connect_database') as connect:
            status, result = self.request(make_handler(self.config, runtime),
                '/api/channels/cbs-news/delete', {'confirmation_text': 'DELETE CBS NEWS', 'acknowledge_rescan': True})
        self.assertEqual(status, 409)
        self.assertIn('shutting down', result['error'])
        connect.assert_not_called()
        self.assertIsNotNone(self.connection.execute("SELECT id FROM channels WHERE slug='cbs-news'").fetchone())


if __name__ == '__main__':
    unittest.main()
