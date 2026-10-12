import ctypes
import importlib.util
import json
import os
from pathlib import Path
import shutil
import unittest
import uuid
from unittest.mock import Mock, patch

from retrotv.config import AppConfig
from retrotv.onboarding import SetupService, existing_profile, atomic_json
from retrotv.processing import PRESETS, enabled, resolution_preset
from retrotv.normalization import NormalizationService
from retrotv.audio_normalization import AudioNormalizationService
from retrotv.commercial_tails import CommercialTailService
from retrotv.promotion import PromotionService
from retrotv.settings import SettingsService
from retrotv import credentials, portable


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(__file__).resolve().parents[1]
        self.root = self.project / '.test-tmp' / ('distribution-' + uuid.uuid4().hex)
        self.media = self.root / 'Média with spaces'
        self.media.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root)

    def config(self, **sections):
        data = {'application': {'database_path':str(self.root/'db')},
                'storage_roots': {'media':str(self.media), 'normalized':str(self.root/'normalized'),
                                  'previews':str(self.root/'previews'), 'originals_backup':str(self.root/'backup')}}
        data.update(sections)
        return AppConfig(self.root/'profile.toml', self.project, data)

    def values(self, **extra):
        return {'media':str(self.media), 'timezone':'Europe/London', 'display':'crt',
                'resolution':'1080p', 'aspect':'preserve', 'policy':'proxy', **extra}

    def test_setup_commits_independent_choices_without_scanning_or_media_writes(self):
        original = self.media/'untouched.mp4'; original.write_bytes(b'original')
        service = SetupService(self.root)
        service.save(self.values(audio=True, video=False, tails=False))
        settings = json.loads((self.root/'user-data/config/portable.windows.settings.json').read_text())
        self.assertFalse(settings['normalization']['enabled'])
        self.assertTrue(settings['audio_normalization']['enabled'])
        self.assertFalse(settings['commercial_tails']['enabled'])
        self.assertEqual(settings['scheduling']['timezone'], 'Europe/London')
        self.assertEqual(settings['guide']['text_size_percent'], 120)
        self.assertEqual(settings['normalization']['canvas_width'], 1920)
        self.assertEqual(original.read_bytes(), b'original')
        self.assertFalse((self.root/'db').exists())
        self.assertTrue(existing_profile(self.root))
        self.assertTrue((self.media/'shared/all-day/commercials').is_dir())
        self.assertTrue((self.media/'_channel_template/morning/episodes').is_dir())
        self.assertTrue((self.media/'_channel_template/over-night/station-ids').is_dir())
        self.assertTrue((self.media/'_channel_template/graphics/night/up-next/music').is_dir())
        self.assertTrue((self.media/'_channel_template/specials/holidays/halloween/night/movies').is_dir())

    def test_unavailable_library_does_not_commit(self):
        with self.assertRaisesRegex(ValueError, 'accessible'):
            SetupService(self.root).save(self.values(media=str(self.root/'absent')))
        self.assertFalse(existing_profile(self.root))

    def test_invalid_timezone_and_flag(self):
        for values in (self.values(timezone='not/a/zone'), self.values(video='yes')):
            with self.assertRaises(ValueError): SetupService(self.root).validate(values)

    def test_backup_under_media_rejected(self):
        with self.assertRaisesRegex(ValueError, 'outside'):
            SetupService(self.root).validate(self.values(backup=str(self.media/'backups')))

    def test_read_only_media_is_usable_without_source_edits(self):
        with patch('retrotv.onboarding.os.access', return_value=False):
            SetupService(self.root).validate(self.values(video=True, policy='proxy'))
            for flags in ({'audio':True}, {'tails':True}, {'video':True,'policy':'replace_with_backup'}):
                with self.assertRaisesRegex(ValueError, 'writable'):
                    SetupService(self.root).validate(self.values(**flags))

    def test_completed_setup_cannot_overwrite_profile(self):
        SetupService(self.root).save(self.values())
        with self.assertRaisesRegex(ValueError, 'already exists'):
            SetupService(self.root).save(self.values(timezone='UTC'))

    def test_interrupted_seed_remains_new_until_settings_commit(self):
        atomic_json(self.root/'user-data/state/setup-pending.json', {})
        profile = self.root/'user-data/config/portable.windows.toml'
        profile.parent.mkdir(); profile.write_text('seeded')
        self.assertFalse(existing_profile(self.root))
        SetupService(self.root).save(self.values())
        self.assertTrue(existing_profile(self.root))

    def test_legacy_profile_recognized_without_setup_marker_even_corrupt_or_empty(self):
        for name in ('config/portable.windows.toml','config/portable.windows.settings.json','database/retrotv.sqlite3'):
            path = self.root/'user-data'/name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'')
            self.assertTrue(existing_profile(self.root))
            path.unlink()

    def test_in_place_profile_prevents_sibling_import(self):
        profile = self.root/'user-data/config/portable.windows.toml'; profile.parent.mkdir(parents=True)
        profile.write_text('keep my preferences')
        with patch('retrotv.portable._previous_build_root') as previous:
            portable._import_previous_user_data(self.root)
            previous.assert_not_called()
        self.assertEqual(profile.read_text(),'keep my preferences')

    def test_existing_database_without_config_reports_recovery_instead_of_setup(self):
        database=self.root/'user-data/database/retrotv.sqlite3'
        database.parent.mkdir(parents=True); database.write_bytes(b'legacy database')
        with patch('retrotv.portable.portable_root',return_value=self.root), patch('retrotv.portable._seed_user_data') as seed, patch('retrotv.portable._show_error') as error, patch.dict(os.environ):
            self.assertEqual(portable.main([]),1)
            seed.assert_not_called()
            self.assertIn('configuration is missing',error.call_args.args[0])
        self.assertEqual(database.read_bytes(),b'legacy database')

    def test_fresh_command_line_run_also_requires_setup_before_processing(self):
        with patch('retrotv.portable.portable_root',return_value=self.root), patch('retrotv.onboarding.serve_setup',return_value=False) as setup, patch.dict(os.environ):
            self.assertEqual(portable.main(['run','--start-tv']),0)
            setup.assert_called_once_with(self.root)
        self.assertFalse((self.root/'user-data/database/retrotv.sqlite3').exists())

    def test_disabled_services_refuse_manual_work_before_any_io(self):
        config = self.config(normalization={'enabled':False}, audio_normalization={'enabled':False},commercial_tails={'enabled':False})
        connection = Mock()
        for service, methods in (
            (NormalizationService(connection,config), ('analyze_asset','create_proxy')),
            (AudioNormalizationService(connection,config), ('analyze_asset','normalize_asset')),
            (CommercialTailService(connection,config), ('analyze_asset',)),
            (PromotionService(connection,config), ('replace_with_backup',))):
            for method in methods:
                with self.subTest(method=method):
                    with self.assertRaisesRegex(ValueError,'disabled'): getattr(service,method)(123)
        connection.execute.assert_not_called()
        self.assertEqual(AudioNormalizationService(connection,config).pending_assets(), [])
        self.assertEqual(CommercialTailService(connection,config).pending_assets(), [])
        self.assertFalse(NormalizationService(connection,config).requires_conversion({}))

    def test_legacy_features_remain_enabled(self):
        for section in ('normalization','audio_normalization','commercial_tails'):
            self.assertTrue(enabled(self.config(),section))

    def test_preserved_aspect_ceiling_and_opt_in_upscale(self):
        profile = {'override_mode':'auto','analysis_status':'pending'}
        for name, (width,height) in PRESETS.items():
            service=NormalizationService(Mock(),self.config(normalization={'aspect_policy':'preserve','canvas_width':width,'canvas_height':height}))
            for source_width,source_height,ratio in ((3840,2160,'16:9'),(1920,1440,'4:3'),(640,480,'4:3'),(320,180,'16:9')):
                row={'width':source_width,'height':source_height,'display_aspect_ratio':ratio}
                actual_width,actual_height=service.output_canvas(row,profile)
                self.assertLessEqual(actual_width,min(width,source_width))
                self.assertLessEqual(actual_height,min(height,source_height))
                self.assertAlmostEqual(actual_width/actual_height,source_width/source_height,delta=.01)
                if source_width==320:
                    self.assertFalse(service.requires_conversion(row,profile))
            service.allow_upscale=True
            small={'width':320,'height':240,'display_aspect_ratio':'4:3'}
            self.assertTrue(service.requires_conversion(small,profile))

    def test_preset_does_not_change_explicit_upscale_flag(self):
        service=SettingsService(self.config(normalization={'allow_upscale':True}))
        for preset,dims in PRESETS.items():
            service.save({'values':{'normalization.resolution_preset':preset}})
            data=json.loads(service.path.read_text())
            self.assertEqual((data['normalization']['canvas_width'],data['normalization']['canvas_height']),dims)
            self.assertNotIn('allow_upscale',data['normalization'])
        service.save({'values':{'normalization.resolution_preset':'Custom','normalization.canvas_width':800,'normalization.canvas_height':600}})
        self.assertEqual(resolution_preset(json.loads(service.path.read_text())),'Custom')

    def test_openai_environment_override_skips_credential_store(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY':'env-test-key'}), patch.object(credentials,'saved_openai_key') as saved:
            self.assertEqual(credentials.configured_openai_api_key(),'env-test-key')
            saved.assert_not_called()

    def test_provider_error_does_not_expose_full_or_partial_key(self):
        import io
        from urllib.error import HTTPError
        from retrotv.ai_announcements import OpenAIHTTPClient, OpenAIProviderError
        key='sk-secret-test-key'
        body=json.dumps({'error':{'message':f'Incorrect API key: {key}; sk-secret***partial'}}).encode()
        error=HTTPError('https://api.openai.com/v1/responses',401,'Unauthorized',{},io.BytesIO(body))
        client=OpenAIHTTPClient(api_key=key,ssl_context=Mock())
        with patch('retrotv.ai_announcements.urllib.request.urlopen',side_effect=error):
            with self.assertRaises(OpenAIProviderError) as failure: client.request_json('responses',{})
        self.assertNotIn('sk-secret',str(failure.exception))
        self.assertIn('[redacted]',str(failure.exception))

    def test_separate_provider_credential_targets_save_replace_remove_and_restart(self):
        store={}
        api=Mock()
        def write(pointer,_):
            credential=ctypes.cast(pointer,ctypes.POINTER(credentials._Credential)).contents
            store[credential.TargetName]=ctypes.string_at(credential.CredentialBlob,credential.CredentialBlobSize).decode()
            return 1
        api.CredWriteW.side_effect=write
        api.CredDeleteW.side_effect=lambda name,*_: bool(store.pop(name,None))
        with patch.object(credentials,'_api',return_value=api), patch.object(credentials,'saved_breeze_key',side_effect=lambda target=credentials.TARGET:store.get(target,'')):
            credentials.save_openai_key('first'); credentials.save_breeze_key('breeze')
            self.assertEqual(credentials.saved_openai_key(),'first')
            credentials.save_openai_key('replacement')
            self.assertEqual(credentials.saved_openai_key(),'replacement')
            credentials.remove_openai_key()
            self.assertEqual(credentials.saved_openai_key(),'')
            self.assertEqual(store[credentials.TARGET],'breeze')

    def test_empty_scaffold_has_only_canonical_paths(self):
        spec=importlib.util.spec_from_file_location('packaging_script',self.project/'scripts/prepare-distribution.py')
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        module.scaffold(self.media)
        for channel in ('shared','_channel_template'):
            for scope in module.SCOPES:
                self.assertTrue((self.media/channel/'specials/holidays/halloween'/scope/'episodes').is_dir())
                self.assertTrue((self.media/channel/'graphics'/scope/'holidays/halloween/up-next').is_dir())
            self.assertFalse((self.media/channel/'specials/holidays/halloween/movies').exists())
            self.assertFalse((self.media/channel/'specials/holidays/halloween/weekly').exists())
        self.assertFalse(any(path.is_file() for path in self.media.rglob('*')))
        self.assertFalse((self.media/'shared/all-day/movies').exists())
        self.assertFalse((self.media/'shared/specials/holidays/halloween/all-day/blocks').exists())
        self.assertTrue((self.media/'_channel_template/all-day/movies').exists())

    def test_credential_endpoint_returns_status_only_and_handles_invalid_requests(self):
        import threading
        from http.server import ThreadingHTTPServer
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        from retrotv.web import make_handler
        with ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.config())) as server:
            worker=threading.Thread(target=server.serve_forever,daemon=True); worker.start()
            url=f'http://127.0.0.1:{server.server_port}/api/credentials/openai'
            try:
                with patch('retrotv.web.save_openai_key') as save, patch('retrotv.web.configured_openai_api_key',return_value='secret-test-value'):
                    request=Request(url,json.dumps({'action':'save','key':'secret-test-value'}).encode(),{'Content-Type':'application/json'})
                    with urlopen(request,timeout=10) as response: body=response.read().decode()
                    self.assertEqual(json.loads(body),{'configured':True})
                    self.assertNotIn('secret-test-value',body)
                    save.assert_called_once_with('secret-test-value')
                    with self.assertRaises(HTTPError) as invalid:
                        urlopen(Request(url,b'[]',{'Content-Type':'application/json'}),timeout=10)
                    self.assertEqual(invalid.exception.code,400)
            finally:
                server.shutdown(); worker.join(10)


if __name__=='__main__': unittest.main()
