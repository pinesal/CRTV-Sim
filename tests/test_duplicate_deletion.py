import json
import threading
import unittest
from unittest.mock import Mock, patch

import test_duplicates as fixtures
import test_channel_deletion as api_fixtures
from retrotv.db import connect_database, utc_now
from retrotv.duplicates import DuplicateService
from retrotv.web import make_handler


class DuplicateDeletionTests(unittest.TestCase):
    setUp = fixtures.DuplicateServiceTests.setUp
    tearDown = fixtures.DuplicateServiceTests.tearDown
    request = api_fixtures.ChannelDeletionAPITests.request

    def prepare(self):
        service = DuplicateService(self.connection, self.config)
        service.record_asset(1)
        service.record_asset(2)
        group = service.groups()['groups'][0]
        return service, group['sha256']

    def test_either_copy_can_be_deleted_without_touching_other_or_backups(self):
        service, digest = self.prepare()
        preview = service.deletion_preview(2, digest)
        backup = self.root/'original-backup.mkv'
        backup.write_bytes(b'original-backup')
        self.assertEqual(preview['remaining'][0]['id'], 1)
        self.assertEqual(preview['path'], str(self.paths[1]))
        result = service.delete_copy(2, digest, preview['token'])
        self.assertEqual(result['asset_id'], 2)
        self.assertFalse(self.paths[1].exists())
        self.assertEqual(self.paths[0].read_bytes(), b'identical-original-video')
        self.assertEqual(backup.read_bytes(), b'original-backup')
        self.assertEqual(tuple(self.connection.execute(
            'SELECT integrity_status,review_status,enabled_for_scheduling FROM assets WHERE id=2'
        ).fetchone()), ('missing','ignored',0))
        self.assertEqual(service.groups()['groups'], [])
        with self.assertRaisesRegex(ValueError, 'set changed'):
            service.delete_copy(2, digest, preview['token'])

    def test_selected_or_surviving_file_change_invalidates_confirmation(self):
        service, digest = self.prepare()
        for index in (0, 1):
            with self.subTest(index=index):
                preview = service.deletion_preview(1, digest)
                self.paths[index].write_bytes(b'changed-video-' + bytes(index+1))
                with self.assertRaisesRegex(ValueError, 'changed'):
                    service.delete_copy(1, digest, preview['token'])
                self.assertTrue(all(path.exists() for path in self.paths))
        self.assertFalse(self.connection.in_transaction)

    def test_missing_survivor_and_invalid_ids_cannot_delete_last_accessible_file(self):
        service, digest = self.prepare()
        for asset_id, group in ((99,digest),(1,'invalid'),(1,'0'*64)):
            with self.assertRaises(ValueError):
                service.deletion_preview(asset_id, group)
        self.paths[1].unlink()
        with self.assertRaisesRegex(ValueError, 'No other accessible copy'):
            service.deletion_preview(1, digest)
        self.assertTrue(self.paths[0].exists())

    def test_failed_unlink_rolls_back_catalog(self):
        service, digest = self.prepare()
        preview = service.deletion_preview(1, digest)
        with patch('retrotv.duplicates.checked_unlink', side_effect=PermissionError('file in use')):
            with self.assertRaises(PermissionError):
                service.delete_copy(1, digest, preview['token'])
        self.assertTrue(self.paths[0].exists())
        self.assertEqual(self.connection.execute('SELECT enabled_for_scheduling FROM assets WHERE id=1').fetchone()[0],1)

    def test_confirmation_is_bound_to_the_selected_copy(self):
        service, digest = self.prepare()
        preview = service.deletion_preview(1,digest)
        with self.assertRaisesRegex(ValueError, 'changed'):
            service.delete_copy(2,digest,preview['token'])
        self.assertTrue(all(path.exists() for path in self.paths))

    def test_path_traversal_and_physical_alias_are_rejected(self):
        service, digest = self.prepare()
        original = self.paths[0].relative_to(self.media).as_posix()
        self.connection.execute("UPDATE assets SET source_relative_path='../outside.mkv' WHERE id=1")
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, 'relative'):
            service.deletion_preview(1,digest)
        self.connection.execute('UPDATE assets SET source_relative_path=? WHERE id=1', (original,))
        self.connection.commit()
        with patch('retrotv.duplicates.resolve_asset_path',return_value=self.paths[0]):
            with self.assertRaisesRegex(ValueError,'same physical file'):
                service.deletion_preview(1,digest)
        self.assertTrue(all(path.exists() for path in self.paths))

    def test_three_copies_leave_remaining_group_in_review(self):
        service, digest = self.prepare()
        third = self.media/'fox/morning/episodes/Show/one.mkv'
        third.parent.mkdir(parents=True)
        third.write_bytes(self.paths[0].read_bytes())
        stat = third.stat()
        self.connection.execute('''INSERT INTO assets(source_root_id,source_relative_path,original_filename,
            file_size,modified_ns,metadata_status,integrity_status,review_status,enabled_for_scheduling,first_seen_at,last_seen_at)
            SELECT source_root_id,?,'one.mkv',?,?,'ready','ready','ready',1,first_seen_at,last_seen_at FROM assets WHERE id=1''',
            (third.relative_to(self.media).as_posix(),stat.st_size,stat.st_mtime_ns))
        self.connection.commit()
        service.record_asset(3)
        preview = service.deletion_preview(1,digest)
        service.delete_copy(1,digest,preview['token'])
        group = service.groups()['groups'][0]
        self.assertEqual(group['member_count'],2)
        self.assertEqual({member['id'] for member in group['members']},{2,3})
        self.assertTrue(third.exists())

    def test_busy_database_fails_before_unlink(self):
        service, digest = self.prepare()
        preview = service.deletion_preview(1, digest)
        writer = connect_database(self.config.database_path)
        try:
            writer.execute('BEGIN IMMEDIATE')
            self.connection.execute('PRAGMA busy_timeout=5')
            with self.assertRaisesRegex(Exception, 'locked'):
                service.delete_copy(1, digest, preview['token'])
            self.assertTrue(all(path.exists() for path in self.paths))
        finally:
            writer.rollback()
            writer.close()

    def test_external_and_managed_alias_of_external_are_read_only(self):
        service, digest = self.prepare()
        self.connection.execute("INSERT INTO storage_roots(logical_name,source_kind,absolute_path) VALUES('external-test','external',?)", (str(self.media),))
        external_id = self.connection.execute("SELECT id FROM storage_roots WHERE logical_name='external-test'").fetchone()[0]
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, 'read-only'):
            service.deletion_preview(1, digest)
        self.connection.execute('UPDATE assets SET source_root_id=? WHERE id=1', (external_id,))
        self.connection.commit()
        self.assertEqual(service.groups()['groups'][0]['members'][0]['source_kind'], 'external')
        with self.assertRaisesRegex(ValueError, 'read-only'):
            service.delete_copy(1, digest, 'anything')
        self.assertTrue(all(path.exists() for path in self.paths))

    def test_shadows_warn_and_become_review_items_but_are_not_deleted(self):
        service, digest = self.prepare()
        now = utc_now()
        sidecar = self.media/'fox/night/commercials/copy.mp4.shadow'
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text(json.dumps({'target':self.paths[0].relative_to(self.media).as_posix()}))
        self.connection.execute('''INSERT INTO shadow_files(shadow_relative_path,target_asset_id,target_relative_path,
            file_size,modified_ns,status,first_seen_at,last_seen_at) VALUES(?,1,?,1,1,'ready',?,?)''',
            (sidecar.relative_to(self.media).as_posix(),self.paths[0].relative_to(self.media).as_posix(),now,now))
        self.connection.commit()
        preview = service.deletion_preview(1, digest)
        self.assertEqual(len(preview['shadows']),1)
        service.delete_copy(1, digest, preview['token'])
        self.assertTrue(sidecar.exists())
        self.assertEqual(self.connection.execute('SELECT status FROM shadow_files').fetchone()[0],'review')

    def test_locked_schedule_and_history_links_are_preserved(self):
        service, digest = self.prepare()
        self.connection.execute("INSERT INTO channels(id,slug,display_name) VALUES(1,'fox','FOX')")
        self.connection.execute("""INSERT INTO schedule_generations(id,channel_id,schedule_date,seed,
            configuration_fingerprint,status,generated_at,locked) VALUES(1,1,'2026-01-01','s','f','active',?,1)""",(utc_now(),))
        self.connection.execute("""INSERT INTO schedule_entries(generation_id,channel_id,asset_id,starts_at,ends_at,
            media_start_ms,media_end_ms,entry_type,title,daypart_slug,sequence_position)
            VALUES(1,1,1,'2026-01-01T10:00:00','2026-01-01T10:20:00',0,1200000,'episode','Show','morning',1)""")
        self.connection.commit()
        before = tuple(self.connection.execute('SELECT * FROM schedule_entries').fetchone())
        preview = service.deletion_preview(1,digest)
        service.delete_copy(1,digest,preview['token'])
        self.assertEqual(tuple(self.connection.execute('SELECT * FROM schedule_entries').fetchone()),before)
        self.assertEqual(self.connection.execute('SELECT locked FROM schedule_generations').fetchone()[0],1)

    def test_api_preview_confirmation_and_runtime_guards(self):
        _, digest = self.prepare()
        runtime = Mock()
        runtime.exclusive_operation_lock = threading.Lock()
        runtime.lock = threading.RLock()
        runtime.stop_event = threading.Event()
        runtime.tv_state = 'playing'
        runtime._tv_is_idle.return_value = True
        runtime._exclusive_media_job_running.return_value = False
        runtime.request_maintenance.return_value = False
        handler = make_handler(self.config, runtime=runtime)
        status, preview = self.request(handler, '/api/duplicates/deletion-preview', {'asset_id':1,'sha256':digest})
        self.assertEqual(status,200,preview)
        request = {key:preview[key] for key in ('asset_id','sha256','token')}
        status, error = self.request(handler, '/api/duplicates/delete', request)
        self.assertEqual(status,400)
        self.assertIn('Stop TV',error['error'])
        runtime.tv_state = 'stopped'
        runtime.stop_event.set()
        self.assertEqual(self.request(handler, '/api/duplicates/delete', request)[0],400)
        runtime.stop_event.clear()
        runtime._exclusive_media_job_running.return_value = True
        self.assertEqual(self.request(handler, '/api/duplicates/delete', request)[0],400)
        runtime._exclusive_media_job_running.return_value = False
        runtime._tv_is_idle.return_value = False
        self.assertEqual(self.request(handler, '/api/duplicates/delete', request)[0],400)
        runtime._tv_is_idle.return_value = True
        runtime.exclusive_operation_lock.acquire()
        try:
            status, error = self.request(handler, '/api/duplicates/delete', request)
            self.assertEqual(status,400)
            self.assertIn('maintaining',error['error'])
        finally:
            runtime.exclusive_operation_lock.release()
        bad_request = dict(request, token='stale')
        self.assertEqual(self.request(handler, '/api/duplicates/delete', bad_request)[0],400)
        self.assertTrue(self.paths[0].exists())
        status, result = self.request(handler, '/api/duplicates/delete', request)
        self.assertEqual(status,200,result)
        self.assertIs(result['maintenance_queued'],False)
        self.assertFalse(self.paths[0].exists())
        runtime._refresh_pending_actions.assert_called_once()

    def test_successful_deletion_is_reported_even_if_followup_refresh_fails(self):
        _, digest = self.prepare()
        runtime = Mock(exclusive_operation_lock=threading.Lock(),lock=threading.RLock(),
                       stop_event=threading.Event(),tv_state='stopped')
        runtime._tv_is_idle.return_value = True
        runtime._exclusive_media_job_running.return_value = False
        runtime._refresh_pending_actions.side_effect = RuntimeError('Refresh unavailable')
        handler = make_handler(self.config,runtime=runtime)
        status, preview = self.request(handler,'/api/duplicates/deletion-preview',{'asset_id':1,'sha256':digest})
        self.assertEqual(status,200,preview)
        status, result = self.request(handler,'/api/duplicates/delete',preview)
        self.assertEqual(status,200,result)
        self.assertFalse(self.paths[0].exists())
        self.assertIn('Refresh unavailable',result['refresh_warning'])
        self.assertFalse(result['maintenance_queued'])

    def test_api_rejects_invalid_requests(self):
        _, digest = self.prepare()
        handler = make_handler(self.config)
        for payload in ([],{'asset_id':True,'sha256':digest},{'asset_id':1,'sha256':None}):
            status, result = self.request(handler,'/api/duplicates/deletion-preview',payload)
            self.assertEqual(status,400,result)


if __name__ == '__main__':
    unittest.main()
