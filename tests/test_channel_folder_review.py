import shutil
import unittest
from unittest.mock import patch

from retrotv.channel_folder_review import ChannelFolderReview
from retrotv.graphics import GraphicsService
from retrotv.scanner import LibraryScanner
from retrotv.settings import SettingsService
from retrotv.web import make_handler
import test_channel_deletion as deletion_fixtures
from test_scanner import FakeInspector
import test_graphics as fixtures


class ChannelFolderTests(unittest.TestCase):
    setUp = fixtures.GraphicsTests.setUp
    tearDown = fixtures.GraphicsTests.tearDown
    channel = deletion_fixtures.ChannelDeletionAPITests.channel
    request = deletion_fixtures.ChannelDeletionAPITests.request

    def scan(self):
        with patch.object(GraphicsService, '_write_placeholder'):
            return LibraryScanner(self.connection, self.config, inspector=FakeInspector()).scan()

    def test_missing_folder_is_reviewed_not_recreated_or_retired_and_reappearing_clears_it(self):
        folder = self.media / 'nickelodeon'
        episode = folder / 'night' / 'episodes' / 'Doug' / '1.mp4'
        episode.parent.mkdir(parents=True)
        episode.write_bytes(b'original')
        self.scan()
        asset = self.connection.execute('SELECT id FROM assets').fetchone()[0]
        parked = self.root / 'parked'
        shutil.copytree(folder, parked)
        shutil.rmtree(folder)
        self.scan()
        self.assertFalse(folder.exists())
        service = ChannelFolderReview(self.connection, self.config)
        items = service.inventory()['items']
        self.assertEqual([item['slug'] for item in items], ['nickelodeon'])
        row = self.connection.execute('SELECT integrity_status,enabled_for_scheduling FROM assets WHERE id=?', (asset,)).fetchone()
        self.assertEqual(tuple(row), ('ready', 1))
        self.assertEqual(self.connection.execute('SELECT enabled FROM channels WHERE id=1').fetchone()[0], 0)
        shutil.copytree(parked, folder)
        self.scan()
        self.assertEqual(service.inventory()['items'], [])
        self.assertEqual(self.connection.execute('SELECT id FROM assets').fetchone()[0], asset)
        self.assertEqual(episode.read_bytes(), b'original')

    def test_feed_channel_with_tracked_folder_is_reviewed_but_external_only_is_not(self):
        self.channel()
        service = LibraryScanner(self.connection, self.config)
        service._sync_channels_from_folders(self.media)
        self.assertEqual(ChannelFolderReview(self.connection, self.config).inventory()['items'], [])
        folder = self.media / 'CBS NEWS'
        folder.mkdir()
        service._sync_channels_from_folders(self.media)
        folder.rmdir()
        service._sync_channels_from_folders(self.media)
        with patch.object(GraphicsService, '_write_placeholder'):
            GraphicsService(self.connection, self.config).ensure_all()
        self.assertFalse((self.media / 'cbs-news').exists())
        item = ChannelFolderReview(self.connection, self.config).inventory()['items'][0]
        self.assertEqual(item['folder_name'], 'CBS NEWS')
        self.assertEqual(self.connection.execute("SELECT enabled FROM channels WHERE slug='cbs-news'").fetchone()[0], 1)

    def test_recreate_restores_full_empty_scaffold_only_after_explicit_action(self):
        scanner = LibraryScanner(self.connection, self.config)
        scanner._sync_channels_from_folders(self.media)
        shutil.rmtree(self.media / 'nickelodeon')
        scanner._sync_channels_from_folders(self.media)
        service = ChannelFolderReview(self.connection, self.config)
        item = service.inventory()['items'][0]
        with self.assertRaisesRegex(ValueError, 'changed'):
            service.recreate('nickelodeon', 'obsolete')
        result = service.recreate('nickelodeon', item['management_identity'])
        self.assertGreater(result['folders_created'], 0)
        for name in ('night/episodes', 'morning/commercials', 'all-day/blocks', 'graphics/up-next/music', 'specials/weekly'):
            self.assertTrue((self.media / 'nickelodeon' / name).is_dir(), name)
        self.assertEqual(service.inventory()['items'], [])
        self.assertFalse(list((self.media / 'nickelodeon').rglob('*.mp4')))

    def test_settings_preserve_missing_choice_and_api_recreates_after_review(self):
        scanner = LibraryScanner(self.connection, self.config)
        scanner._sync_channels_from_folders(self.media)
        shutil.rmtree(self.media / 'nickelodeon')
        scanner._sync_channels_from_folders(self.media)
        settings = SettingsService(self.config)
        windows = self.config.data['seasonal']['windows']
        settings._ensure_weekly_collection_directories({}, windows)
        settings.ensure_holiday_directories(windows, {'halloween':{}}, [])
        self.assertFalse((self.media / 'nickelodeon').exists())
        status, result = self.request(make_handler(self.config), '/api/channels/missing-folders')
        self.assertEqual(status, 200)
        identity = result['items'][0]['management_identity']
        status, result = self.request(make_handler(self.config), '/api/channels/nickelodeon/recreate-folders', {'channel_identity':identity})
        self.assertEqual(status, 200)
        self.assertTrue(result['ok'])
        self.assertTrue((self.media / 'nickelodeon/night/episodes').is_dir())

    def test_unavailable_media_root_does_not_create_reviews_or_folders(self):
        scanner = LibraryScanner(self.connection, self.config)
        scanner._sync_channels_from_folders(self.media)
        parked = self.root / 'offline'
        self.media.rename(parked)
        try:
            with self.assertRaises(FileNotFoundError):
                scanner.scan()
            data = ChannelFolderReview(self.connection, self.config).inventory()
            self.assertFalse(data['available'])
            self.assertEqual(data['items'], [])
            self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM channel_folder_state WHERE missing_since IS NOT NULL').fetchone()[0], 0)
            self.assertFalse(self.media.exists())
        finally:
            parked.rename(self.media)

    def test_manual_channel_registration_still_reviews_missing_folders_without_changing_enabled(self):
        self.config.data['library'] = {'auto_register_channels':False}
        scanner = LibraryScanner(self.connection, self.config)
        scanner._sync_channels_from_folders(self.media)
        (self.media / 'nickelodeon').rmdir()
        (self.media / 'new-unregistered').mkdir()
        scanner._sync_channels_from_folders(self.media)
        self.assertEqual([row['slug'] for row in ChannelFolderReview(self.connection, self.config).inventory()['items']], ['nickelodeon'])
        self.assertEqual(self.connection.execute('SELECT enabled FROM channels WHERE id=1').fetchone()[0], 1)
        self.assertIsNone(self.connection.execute("SELECT id FROM channels WHERE slug='new-unregistered'").fetchone())

    def test_synthetic_weather_does_not_generate_a_folder_review(self):
        self.connection.execute("INSERT INTO channels(slug,display_name) VALUES('weather','Weather')")
        self.connection.commit()
        LibraryScanner(self.connection, self.config)._sync_channels_from_folders(self.media)
        self.assertEqual(ChannelFolderReview(self.connection, self.config).inventory()['items'], [])


if __name__ == '__main__':
    unittest.main()
