import base64
from datetime import date, datetime, timezone
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid
import wave

from retrotv.config import AppConfig
from retrotv.graphics_lab import _png_rgba, png_dimensions
from retrotv.weather_graphics import WeatherGraphicsService, sync_weather_holidays
from retrotv.web import make_handler
from retroweather.assets import scan_asset_library
from retroweather.config import AppPaths, initialize, load_settings, save_settings
from retroweather.models import NormalizedWeatherSnapshot
from retroweather.planning import build_forecast_plan
from retroweather.rendering import build_screens, resolve_screen_backgrounds
from retroweather.rendering.seasonal import active_season, select_holiday_overlay, seasonal_appearance
from retroweather.rendering.themes import ThemeSelection, discover_backgrounds
from retroweather.web.server import create_server


class WeatherGraphicsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = Path(__file__).resolve().parents[1] / '.test-tmp' / f'weather-art-{uuid.uuid4().hex}'
        self.temporary.mkdir(parents=True)
        self.home = self.temporary / 'Weather'
        self.config = AppConfig(self.temporary / 'test.toml', self.temporary, {
            'application': {'database_path': str(self.temporary / 'db.sqlite3')},
            'synthetic_channels': {'weather': {'companion_home': str(self.home)}},
            'seasonal': {'holidays': {'summer': {'start': '06-01', 'end': '09-30', 'priority': 10}}},
            'graphics': {'ai_image_lab_model': 'test-image-model'},
        })
        self.service = WeatherGraphicsService(self.config)

    def tearDown(self):
        shutil.rmtree(self.temporary, ignore_errors=True)

    def require_ffmpeg(self):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            self.skipTest('FFmpeg and FFprobe required')

    def image(self, *, transparent=False):
        # An opaque colored border surrounds a transparent center.
        pixels = bytearray()
        for y in range(80):
            for x in range(100):
                clear = transparent and 5 < x < 95 and 5 < y < 75
                pixels.extend((20, 80, 160, 0 if clear else 255))
        return _png_rgba(100, 80, bytes(pixels))

    def upload(self, key, *, transparent=False):
        return self.service.upload({'key': key, 'filename': 'art.png',
            'content_base64': base64.b64encode(self.image(transparent=transparent)).decode()})

    def server(self, handler):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(3)
        self.addCleanup(stop)
        return f'http://127.0.0.1:{server.server_address[1]}'

    def test_catalog_covers_every_family_day_night_alert_and_dynamic_holiday(self):
        with patch('retrotv.weather_graphics.configured_openai_api_key', return_value=None):
            catalog = self.service.catalog()
        keys = {slot['key'] for slot in catalog['slots']}
        self.assertEqual(len(keys), 20)
        for family in ('default', 'sunny', 'partly-cloudy', 'cloudy', 'rainy', 'stormy', 'snowy', 'foggy', 'windy'):
            self.assertTrue({f'{family}/day', f'{family}/night'}.issubset(keys))
        self.assertIn('alert', keys)
        self.assertIn('holiday/summer', keys)
        self.assertFalse(catalog['ai_available'])
        self.config.data['seasonal']['holidays'] = {'winter': {'start': '12-01', 'end': '02-28', 'enabled': False}}
        self.assertNotIn('holiday/summer', self.service.slots())
        self.assertFalse(self.service.slots()['holiday/winter']['enabled'])

    def test_shared_calendar_add_edit_remove_preserves_weather_settings(self):
        paths = AppPaths.discover(self.home)
        initialize(paths)
        original = paths.settings.read_bytes()
        self.assertTrue(sync_weather_holidays(self.config))
        self.assertFalse(sync_weather_holidays(self.config))
        settings = load_settings(paths)
        self.assertEqual(settings['crtvsim_holidays'], self.config.data['seasonal']['holidays'])
        save_settings(paths, settings)
        self.assertNotIn('crtvsim_holidays', json.loads(paths.settings.read_text()))
        self.assertEqual(paths.settings.read_bytes(), original)
        self.config.data['seasonal']['holidays']['summer']['end'] = '08-31'
        sync_weather_holidays(self.config)
        self.assertEqual(load_settings(paths)['crtvsim_holidays']['summer']['end'], '08-31')
        self.config.data['seasonal']['holidays'] = {}
        sync_weather_holidays(self.config)
        self.assertEqual(load_settings(paths)['crtvsim_holidays'], {})
        self.assertIsNone(seasonal_appearance(load_settings(paths), date(2026, 10, 5))[1])

    def test_custom_ranges_inclusive_cross_year_priority_disabled_and_ties(self):
        holidays = {
            'summer': {'start': '06-01', 'end': '09-30', 'priority': 10},
            'winter': {'start': '12-01', 'end': '02-28'},
            'disabled': {'start': '01-01', 'end': '12-31', 'priority': 1000, 'enabled': False},
        }
        for day in (date(2026, 6, 1), date(2026, 9, 30)):
            self.assertEqual(active_season(day, holidays), 'summer')
        for day in (date(2026, 12, 31), date(2027, 1, 1), date(2027, 2, 28)):
            self.assertEqual(active_season(day, holidays), 'winter')
        self.assertIsNone(active_season(date(2026, 10, 1), holidays))
        holidays['another'] = dict(holidays['summer'])
        self.assertEqual(active_season(date(2026, 8, 1), holidays), 'another')
        holidays['summer']['priority'] = 20
        self.assertEqual(active_season(date(2026, 8, 1), holidays), 'summer')
        self.assertEqual(active_season(date(2026, 10, 1)), 'halloween')
        self.assertEqual(active_season(date(2026, 12, 25)), 'christmas')
        self.assertIsNone(active_season(date(2026, 12, 26)))

    def test_uploads_are_immediately_eligible_for_renderer_and_rotation(self):
        self.require_ffmpeg()
        first, second = self.upload('sunny/day'), self.upload('sunny/day')
        self.assertNotEqual(first, second)
        self.assertEqual(set(discover_backgrounds(self.service.root, ThemeSelection('sunny', 'day'))), {first, second})
        overlay = self.upload('holiday/summer', transparent=True)
        self.assertEqual(select_holiday_overlay(self.service.root, 'summer', 'generation'), overlay)
        self.assertIsNone(select_holiday_overlay(self.service.root, None, 'generation'))
        with patch('retrotv.weather_graphics.configured_openai_api_key', return_value='test-key'):
            holiday = next(s for s in self.service.catalog()['slots'] if s['key'] == 'holiday/summer')
        self.assertTrue(holiday['assets'][0]['usable_now'])

    def test_rejects_corrupt_opaque_and_fake_transparency_without_installing(self):
        self.require_ffmpeg()
        with self.assertRaisesRegex(ValueError, 'transparent center'):
            self.upload('holiday/summer')
        with self.assertRaises(ValueError):
            self.service.upload({'key': 'sunny/day', 'filename': 'broken.png', 'content_base64': 'Y29ycnVwdA=='})
        with self.assertRaises(ValueError):
            self.service.upload({'key': 'sunny/day', 'filename': 'a.exe', 'content_base64': 'YQ=='})
        with self.assertRaises(ValueError):
            self.service.upload({'key': 'sunny/day', 'filename': 'a.png', 'content_base64': 'invalid!=='})
        self.assertFalse(self.service.folder('holiday/summer').exists())
        self.assertEqual(list(self.service.root.glob('.weather-upload-*')), [])

    def test_forecast_screens_use_custom_holiday_layer_with_tv_calendar(self):
        self.require_ffmpeg()
        paths = AppPaths.discover(self.home)
        initialize(paths)
        sync_weather_holidays(self.config)
        overlay = self.upload('holiday/summer', transparent=True)
        fixture = Path(__file__).resolve().parents[1] / 'RetroWeather/tests/fixtures/normalized-weather.json'
        snapshot = NormalizedWeatherSnapshot.from_dict(json.loads(fixture.read_text()))
        settings = load_settings(paths)
        plan = build_forecast_plan(snapshot, settings)
        screens = build_screens(snapshot, plan, settings, now=datetime(2026, 8, 1, tzinfo=timezone.utc))
        rendered = resolve_screen_backgrounds(screens, self.service.root, 'test-generation')
        self.assertTrue(all(screen.season == 'summer' and screen.holiday_overlay == overlay for screen in rendered))
        screens = build_screens(snapshot, plan, settings, now=datetime(2026, 10, 1, tzinfo=timezone.utc))
        rendered = resolve_screen_backgrounds(screens, self.service.root, 'test-generation')
        self.assertTrue(all(screen.season is None and screen.holiday_overlay is None for screen in rendered))

    def test_rejects_unknown_slot_and_filename_traversal(self):
        for key in ('../outside', 'holiday/not-configured', 'sunny/../day'):
            with self.assertRaises(ValueError):
                self.service.folder(key)
        for name in ('../other.png', '..\\other.png', '.hidden.png', 'other.txt'):
            with self.assertRaises(ValueError):
                self.service.asset_path('sunny/day', name)

    def test_refuses_artwork_folder_symlink_escape(self):
        outside = self.temporary / 'outside'
        outside.mkdir()
        target = self.service.root / 'graphics/conditions/sunny/day'
        target.parent.mkdir(parents=True)
        try:
            target.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Creating directory symlinks not permitted')
        self.addCleanup(target.unlink)
        with self.assertRaisesRegex(ValueError, 'leaves the media root'):
            self.service.folder('sunny/day')

    def test_generation_uses_existing_provider_settings_and_stretches_without_crop(self):
        self.require_ffmpeg()
        for key, transparent in (('snowy/night', False), ('holiday/summer', True)):
            with self.subTest(key=key), patch('retrotv.weather_graphics.OpenAIHTTPClient') as client:
                client.return_value.available = True
                client.return_value.request_json.return_value = {'data': [{'b64_json': base64.b64encode(self.image(transparent=transparent)).decode()}]}
                target = self.service.generate({'key': key, 'art_direction': 'Hand painted watercolor.'})
                endpoint, request = client.return_value.request_json.call_args.args
                self.assertEqual(endpoint, 'images/generations')
                self.assertEqual(request['model'], 'test-image-model')
                self.assertEqual(request['background'], 'transparent' if transparent else 'opaque')
                self.assertIn('Hand painted watercolor.', request['prompt'])
                self.assertIn('No', request['prompt'])
                self.assertEqual(png_dimensions(target), (640, 480))
                self.assertEqual(target.name, 'ai-weather.png')
                self.assertTrue(self.service.probe(target)['valid'])

    def test_provider_failure_does_not_replace_existing_artwork(self):
        target = self.service.folder('sunny/day') / 'ai-weather.png'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'existing graphic')
        with patch('retrotv.weather_graphics.OpenAIHTTPClient') as client:
            client.return_value.available = True
            client.return_value.request_json.side_effect = RuntimeError('provider outage')
            with self.assertRaisesRegex(RuntimeError, 'provider outage'):
                self.service.generate({'key': 'sunny/day'})
        self.assertEqual(target.read_bytes(), b'existing graphic')
        with patch('retrotv.weather_graphics.OpenAIHTTPClient') as client:
            client.return_value.available = False
            with self.assertRaisesRegex(ValueError, 'OpenAI API key'):
                self.service.generate({'key': 'sunny/day'})
            client.return_value.request_json.assert_not_called()

    def test_art_catalog_does_not_poison_cached_music_probes(self):
        self.require_ffmpeg()
        music = self.service.root / 'music/default/music.wav'
        music.parent.mkdir(parents=True)
        with wave.open(str(music), 'wb') as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(8000)
            writer.writeframes(b'\0\0' * 8000)
        self.service.catalog()
        inventory = scan_asset_library(self.service.root)
        self.assertTrue(inventory['assets'][0]['valid'])
        self.assertEqual(inventory['assets'][0]['media_type'], 'audio')

    def test_http_catalog_upload_preview_and_invalid_inputs(self):
        self.require_ffmpeg()
        base = self.server(make_handler(self.config))
        with urlopen(base + '/api/weather-graphics', timeout=5) as response:
            self.assertEqual(len(json.load(response)['slots']), 20)
        data = {'key': 'cloudy/night', 'filename': 'art.png', 'content_base64': base64.b64encode(self.image()).decode()}
        with urlopen(Request(base + '/api/weather-graphics/upload', data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'}), timeout=5) as response:
            target = Path(json.load(response)['path'])
        query = urlencode({'key': 'cloudy/night', 'filename': target.name})
        with urlopen(base + '/api/weather-graphics/file?' + query, timeout=5) as response:
            self.assertEqual(response.read(), self.image())
        query = urlencode({'key': 'cloudy/night', 'filename': '../outside.png'})
        with self.assertRaises(HTTPError) as caught:
            urlopen(base + '/api/weather-graphics/file?' + query, timeout=5)
        self.assertEqual(caught.exception.code, 400)

    def test_weather_dashboard_reports_configured_custom_holidays(self):
        paths = AppPaths.discover(self.home)
        initialize(paths)
        sync_weather_holidays(self.config)
        server = create_server(paths, host='127.0.0.1', port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f'http://127.0.0.1:{server.server_address[1]}/api/assets', timeout=5) as response:
                self.assertEqual(json.load(response)['holidays'], self.config.data['seasonal']['holidays'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_saved_tv_settings_stay_successful_if_weather_calendar_is_unwritable(self):
        base = self.server(make_handler(self.config))
        with patch('retrotv.web.SettingsService') as settings, patch('retrotv.weather_graphics.sync_weather_holidays', side_effect=OSError('read only')):
            settings.return_value.save.return_value = {'ok': True, 'message': 'Settings saved.'}
            request = Request(base + '/api/settings', data=b'{"holidays": {}}', headers={'Content-Type': 'application/json'})
            with urlopen(request, timeout=5) as response:
                result = json.load(response)
                self.assertEqual(response.status, 200)
                self.assertTrue(result['ok'])
                self.assertIn('Weather holiday sync failed', result['message'])
