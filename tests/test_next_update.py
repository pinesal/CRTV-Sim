import io
import json
import threading
import unittest
import wave
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch
import test_ai_announcements as ai_fixture
import test_settings as settings_fixture
import test_graphics as graphics_fixture
import test_runtime as runtime_fixture
from retrotv.announcements import UpNextRenderer, AnnouncementPlan, AnnouncementLine
from retrotv.runtime import ApplicationRuntime
from retrotv.db import apply_migrations, connect_database
from retrotv.bumper_cues import classify_bumper, effective_bumper_cue, configured_bumper_clues
from retrotv.holiday_review import HolidayReviewService, clue_settings
from retrotv.settings import SettingsService
from retrotv.config import load_config
from retrotv.ai_announcements import AnnouncementProfileService, OpenAITTSProvider
from retrotv.tts import BreezeTTSProvider, BreezeProviderError, provider_for_profile
from retrotv.graphics import GraphicsService
from retrotv.graphics_lab import write_interstitial_blueprint
from retrotv.web import make_handler


def wav_bytes():
    data = io.BytesIO()
    with wave.open(data, 'wb') as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(24000)
        audio.writeframes(b'\0\0' * 2400)
    return data.getvalue()


class ClueTests(unittest.TestCase):
    setUp = settings_fixture.SettingsServiceTests.setUp
    tearDown = settings_fixture.SettingsServiceTests.tearDown

    def test_brb_boundaries_empty_lists_cache_and_manual_override(self):
        for filename in ['BRB.mp4', 'Nick B.R.B..mp4', 'B R B.mp4', 'Be-right-back.mp4']:
            self.assertEqual(classify_bumper(filename).position, 'break_start')
        for filename in ['BRBrian.mp4', 'abrb.mp4', 'BRB welcome back.mp4']:
            self.assertEqual(classify_bumper(filename).position, 'general')
        empty = ((), ())
        for filename in ['BRB.mp4', 'Doug will return.mp4', 'Back to Doug.mp4']:
            self.assertEqual(classify_bumper(filename, empty).position, 'general')
        self.assertEqual(effective_bumper_cue('BRB.mp4', 'exclude', None, empty).position, 'exclude')
        self.assertEqual(classify_bumper('BRB.mp4').position, 'break_start')

    def test_saved_lists_replace_baseline_and_survive_reload(self):
        self.profile.write_text(self.profile.read_text() + '\n[library.holiday_filename_clues]\nold = ["ancient"]\n')
        self.config.data['library'] = {'holiday_filename_clues': {'old': ['ancient']}}
        service = SettingsService(self.config)
        service.save({'filename_clues': {'holidays': {'halloween': [], 'summer': ['Summer Time', 'summer-time']},
                                        'bumpers': {'break_start': ['Pause'], 'break_end': []}}})
        loaded = load_config(self.profile)
        self.assertNotIn('old', loaded.data['library']['holiday_filename_clues'])
        self.assertEqual(loaded.data['library']['holiday_filename_clues']['summer'], ['Summer Time'])
        review = HolidayReviewService(None, loaded)
        self.assertEqual(review.matches('Haunted.mp4'), {})
        self.assertIn('summer', review.matches('SUMMER.time.mp4'))
        self.assertEqual(classify_bumper('BRB.mp4', configured_bumper_clues(loaded)).position, 'general')
        self.assertEqual(classify_bumper('Pause.mp4', configured_bumper_clues(loaded)).position, 'break_start')

    def test_configured_holidays_always_match_and_new_groups_are_seeded(self):
        self.config.data['seasonal'] = {'holidays': {'summer': {}, 'founders-day': {}}}
        self.config.data['library'] = {'holiday_filename_clues': {'summer': [], 'founders-day': []}}
        service = HolidayReviewService(None, self.config)
        self.assertIn('summer', service.matches('Summer Special.mp4'))
        for filename in ['FoundersDay.mp4', 'FOUNDERS.DAY.mp4', 'founders_day.mp4']:
            self.assertIn('founders-day', service.matches(filename))
        self.assertEqual(service.matches('Summertime.mp4'), {})
        groups = clue_settings(self.config)
        self.assertIn('summer', groups['mandatory_holidays'])
        self.assertEqual(groups['defaults']['holidays']['summer'], ['summer'])


class ProviderTests(unittest.TestCase):
    setUp = ai_fixture.AIAnnouncementTests.setUp
    tearDown = ai_fixture.AIAnnouncementTests.tearDown

    def test_profile_provider_persistence_switching_and_fingerprints_all_scopes(self):
        for daypart, weekly in [(None,None), ('night',None), (None,'snick-main')]:
            original = self.service.scoped_profile('nickelodeon', daypart, weekly)
            updated = self.service.update('nickelodeon', {'tts_provider':'breeze', 'breeze_speech_model':'breeze-tts-2',
                'breeze_voice_id':'Voc_CaSeID', 'override_enabled':True}, daypart, weekly)
            self.assertEqual(updated.breeze_voice_id, 'Voc_CaSeID')
            self.assertEqual(updated.tts_provider, 'breeze')
            self.assertNotEqual(original.fingerprint, updated.fingerprint)
            switched = self.service.update('nickelodeon', {'tts_provider':'openai'}, daypart, weekly)
            self.assertEqual(switched.voice, original.voice)
            self.assertEqual(switched.breeze_voice_id, 'Voc_CaSeID')

    def test_synthesis_mapping_wav_and_failure_preserves_output(self):
        client = Mock(); client.text_to_speech.convert.return_value.content = wav_bytes()
        provider = BreezeTTSProvider(model='breeze-tts-2', client=client)
        target = self.root / 'speech.wav'
        provider.synthesize('Hello', target, voice='Voc_CaSe', instructions='Warm', rate=0)
        client.text_to_speech.convert.assert_called_once_with(voice_id='Voc_CaSe', text='Hello',
            model_id='breeze-tts-2', instructions='Warm', output_format='wav', timeout=90)
        original = target.read_bytes()
        client.text_to_speech.convert.return_value.content = b'not audio'
        with self.assertRaises(BreezeProviderError):
            provider.synthesize('Hello', target, voice='Voc_CaSe')
        self.assertEqual(target.read_bytes(), original)
        client.text_to_speech.convert.side_effect = RuntimeError('sensitive-key')
        with self.assertRaisesRegex(BreezeProviderError, 'request failed'):
            provider.synthesize('Hello', target, voice='Voc_CaSe')

    def test_options_pagination_deduplication_and_error(self):
        client = Mock(); client.models.list.return_value = [{'model_id':'breeze-tts-2','name':'Breeze 2'}]
        client.voices.search.side_effect = [
            {'voices':[{'voice_id':'CaseA','name':'A'}], 'has_more':True, 'next_page_token':'next'},
            {'voices':[{'voice_id':'CaseA','name':'A'},{'voice_id':'CaseB','name':'B'}], 'has_more':False}]
        provider = BreezeTTSProvider(client=client)
        options = provider.options()
        self.assertEqual([v['id'] for v in options['voices']], ['CaseA'])
        self.assertTrue(options['has_more'])
        self.assertEqual(options['next_page_token'], 'next')
        client.voices.search.assert_called_once()
        next_options = provider.options(page=2, next_page_token='next', include_models=False)
        self.assertEqual([v['id'] for v in next_options['voices']], ['CaseA','CaseB'])
        self.assertEqual(client.voices.search.call_args.kwargs['next_page_token'], 'next')
        client.models.list.assert_called_once_with(timeout=90)
        client.voices.search.side_effect = RuntimeError('private data')
        with self.assertRaises(BreezeProviderError): provider.options()

    def test_generic_audio_records_selected_provider(self):
        for daypart, weekly, table in [(None,None,'announcement_generic_audio'),
            ('night',None,'announcement_daypart_generic_audio'),(None,'snick-main','announcement_weekly_generic_audio')]:
            self.service.update('nickelodeon', {'tts_provider':'breeze','breeze_voice_id':'CaseID',
                'generic_script':'Welcome to Nickelodeon.'}, daypart, weekly)
            client = Mock(); client.text_to_speech.convert.return_value.content = wav_bytes()
            output = self.service.generate_generic('nickelodeon', BreezeTTSProvider(client=client), daypart, weekly)
            self.assertTrue(output.is_file())
            self.assertEqual(self.connection.execute(f'SELECT provider FROM {table}').fetchone()['provider'], 'breeze')
            self.assertEqual(client.text_to_speech.convert.call_args.kwargs['voice_id'], 'CaseID')

    def test_renderer_generates_openai_script_with_breeze_speech(self):
        self.config.data['storage_roots'] = {'media':str(self.root/'media')}
        self.service.update('nickelodeon', {'enabled':True,'tts_provider':'breeze','breeze_voice_id':'CaseID'})
        client = Mock(); client.text_to_speech.convert.return_value.content = wav_bytes()
        provider = BreezeTTSProvider(client=client)
        renderer = UpNextRenderer(self.connection,self.config,tts_provider=provider, ai_client=ai_fixture.FakeOpenAIClient())
        renderer._encode = lambda work, ass, audio, output, color, duration, **kwargs: output.write_bytes(b'video')
        renderer._validate = lambda path, expected_duration=None: float(expected_duration or 30)
        renderer._audio_duration = lambda path: 0.1
        when=datetime.fromisoformat('2026-08-03T15:00:00-07:00')
        plan=AnnouncementPlan('nickelodeon','Nickelodeon',when,(AnnouncementLine(when,'Doug'),),'Tag','0xF58220','Original script')
        result=renderer.render('nickelodeon',plan_override=plan,force=True)
        self.assertTrue(result.output.is_file())
        self.assertIn('three back-to-back',client.text_to_speech.convert.call_args.kwargs['text'])
        self.assertEqual(client.text_to_speech.convert.call_args.kwargs['voice_id'],'CaseID')

    def test_migration_preserves_existing_models_and_voices(self):
        db=connect_database(self.root/'old.sqlite3')
        try:
            for migration in sorted((self.project/'migrations').glob('*.sql')):
                if migration.name.startswith('034_'): break
                db.executescript(migration.read_text(encoding='utf-8'))
            db.execute("INSERT INTO channels(id,slug,display_name,enabled) VALUES(1,'old','Old',1)")
            db.execute("INSERT INTO announcement_channel_profiles(channel_id,speech_model,voice,updated_at) VALUES(1,'old-model','old-voice','now')")
            db.executescript((self.project/'migrations'/'034_profile_tts_providers.sql').read_text())
            row=db.execute('SELECT * FROM announcement_channel_profiles').fetchone()
            self.assertEqual((row['tts_provider'],row['speech_model'],row['voice']),('openai','old-model','old-voice'))
        finally: db.close()

    def test_breeze_tts_test_does_not_require_openai_or_save_profile(self):
        server = ThreadingHTTPServer(('127.0.0.1',0), make_handler(self.config))
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            with patch('retrotv.web.provider_for_profile') as factory, patch('retrotv.ai_announcements.configured_openai_api_key', return_value=''):
                def synthesize(text, output, **kwargs):
                    output.parent.mkdir(parents=True, exist_ok=True); output.write_bytes(wav_bytes())
                factory.return_value.synthesize.side_effect = synthesize
                payload = {'kind':'tts','tts_provider':'breeze','breeze_speech_model':'breeze-tts-2','breeze_voice_id':'CaseID','generic_script':'Hello'}
                request = Request(f'http://127.0.0.1:{server.server_port}/api/announcement-profiles/nickelodeon/test',
                                  data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
                try:
                    with urlopen(request) as response: result = json.load(response)
                except HTTPError as exc:
                    raise AssertionError(exc.read().decode()) from exc
                self.assertEqual(result['voice'], 'CaseID')
                self.assertEqual(factory.call_args.args[0].tts_provider, 'breeze')
                self.assertEqual(self.service.scoped_profile('nickelodeon').tts_provider,'openai')
        finally: server.shutdown(); server.server_close(); thread.join()


class CredentialTests(unittest.TestCase):
    def test_windows_save_uses_credential_manager_not_settings(self):
        import ctypes
        from retrotv.credentials import save_breeze_key, _Credential, TARGET
        with patch('retrotv.credentials._api') as api:
            api.return_value.CredWriteW.return_value = 1
            def capture(pointer, flags):
                item=ctypes.cast(pointer,ctypes.POINTER(_Credential)).contents
                self.assertEqual(item.TargetName,TARGET)
                self.assertEqual(ctypes.string_at(item.CredentialBlob,item.CredentialBlobSize),b'test-only-key')
                self.assertEqual(item.Persist,2)
                return 1
            api.return_value.CredWriteW.side_effect=capture
            save_breeze_key('test-only-key')
        with self.assertRaises(ValueError): save_breeze_key('')

    def test_environment_override_does_not_read_saved_secret(self):
        from retrotv.credentials import configured_breeze_api_key
        with patch.dict('os.environ',{'BREEZE_API_KEY':'test-only-override'}), patch('retrotv.credentials.saved_breeze_key') as saved:
            self.assertEqual(configured_breeze_api_key(),'test-only-override'); saved.assert_not_called()


class ReadinessTests(unittest.TestCase):
    setUp = runtime_fixture.ApplicationRuntimeTests.setUp
    tearDown = runtime_fixture.ApplicationRuntimeTests.tearDown

    def test_dynamic_narration_requires_both_keys_and_global_off_wins(self):
        runtime=ApplicationRuntime(self.config)
        with connect_database(self.config.database_path) as db:
            AnnouncementProfileService(db,self.config).update('nickelodeon',{'enabled':True,'tts_provider':'breeze','breeze_voice_id':'CaseID'})
            with patch('retrotv.ai_announcements.configured_openai_api_key',return_value='script-key'), patch('retrotv.tts.configured_breeze_api_key',return_value='speech-key'):
                self.assertEqual(runtime._automatic_tts_policy('nickelodeon',db),'cloud')
                self.config.data['announcements']={'tts_provider':'none'}
                self.assertEqual(runtime._automatic_tts_policy('nickelodeon',db),'disabled')
            self.config.data['announcements']={'tts_provider':'openai'}
            with patch('retrotv.ai_announcements.configured_openai_api_key',return_value=''):
                self.assertEqual(runtime._automatic_tts_policy('nickelodeon',db),'disabled')
            with patch('retrotv.ai_announcements.configured_openai_api_key',return_value='script-key'), patch('retrotv.tts.configured_breeze_api_key',return_value=''):
                self.assertEqual(runtime._automatic_tts_policy('nickelodeon',db),'disabled')


class VariationTests(unittest.TestCase):
    setUp = graphics_fixture.GraphicsTests.setUp
    tearDown = graphics_fixture.GraphicsTests.tearDown

    def test_variation_endpoint_scope_rotation_replace_and_failure(self):
        self.config.data['storage_roots']['state'] = str(self.root / 'state')
        self.config.data['storage_roots']['previews'] = str(self.root / 'previews')
        service = GraphicsService(self.connection, self.config)
        server = ThreadingHTTPServer(('127.0.0.1',0), make_handler(self.config))
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        lab_root = self.root / 'lab'; lab_root.mkdir()
        sequence = 0
        def generate(payload):
            nonlocal sequence
            sequence += 1
            run = lab_root / str(sequence); run.mkdir()
            write_interstitial_blueprint(run / 'background-640x480.png')
            return {'id':str(sequence),'model':'mock-image'}
        def post(payload, endpoint='/api/graphics/generate'):
            request = Request(f'http://127.0.0.1:{server.server_port}{endpoint}',
                data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
            try:
                with urlopen(request) as response: return json.load(response)
            except HTTPError as exc:
                exc.read(); raise
        try:
            with patch('retrotv.web.GraphicsLab') as lab:
                lab.return_value.root = lab_root; lab.return_value.generate.side_effect = generate
                for purpose in ['up-next','interstitials']:
                    for scope in [{}, {'daypart':'night'}, {'holiday':'halloween'}, {'weekly':'snick'}, {'holiday':'halloween','weekly':'snick'}]:
                        original_path = Path(post({'action':'install','channel':'nickelodeon','purpose':purpose,**scope})['path'])
                        if scope == {'holiday':'halloween'}:
                            legacy=self.media/'nickelodeon'/'specials'/'holidays'/'halloween'/'graphics'/purpose/'ai-station.png'
                            legacy.parent.mkdir(parents=True,exist_ok=True); original_path.rename(legacy)
                            original_path=legacy; service.scan_catalog()
                        original = original_path.read_bytes()
                        row = self.connection.execute('SELECT id FROM graphic_assets WHERE relative_path=?',
                            (original_path.relative_to(self.media).as_posix(),)).fetchone()
                        variation = Path(post({'action':'variation','graphic_id':f"g-{row['id']}", 'channel':'malicious-other','purpose':'bad'})['path'])
                        self.assertNotEqual(variation,original_path)
                        self.assertEqual(variation.parent,original_path.parent)
                        self.assertEqual(original_path.read_bytes(),original)
                        self.assertTrue(variation.name.startswith('ai-variation-'))
                        self.assertEqual(self.connection.execute('SELECT provenance FROM graphic_assets WHERE relative_path=?',
                            (variation.relative_to(self.media).as_posix(),)).fetchone()['provenance'],'ai')
                        class CaptureChoices:
                            def choice(self, choices):
                                self.choices=choices; return choices[0]
                        rng=CaptureChoices()
                        when=datetime.fromisoformat('2026-10-31T21:00:00-07:00' if scope.get('holiday') else '2026-09-26T21:00:00-07:00')
                        service.select_asset('nickelodeon',purpose,when,daypart_slug='night' if scope.get('daypart') else 'afternoon',
                                       weekly_collection_slug=scope.get('weekly'),rng=rng)
                        self.assertEqual({r['relative_path'] for r in rng.choices},
                            {p.relative_to(self.media).as_posix() for p in (original_path,variation)})
                        variation_id=self.connection.execute('SELECT id FROM graphic_assets WHERE relative_path=?',
                            (variation.relative_to(self.media).as_posix(),)).fetchone()['id']
                        post({'action':'delete-ai','graphic_id':f'g-{variation_id}'},'/api/graphics/manage')
                        self.assertFalse(variation.exists()); self.assertEqual(original_path.read_bytes(),original)
                        variation=Path(post({'action':'variation','graphic_id':f"g-{row['id']}"})['path'])
                        lab.return_value.generate.side_effect = RuntimeError('offline')
                        with self.assertRaises(HTTPError): post({'action':'variation','graphic_id':f"g-{row['id']}"})
                        self.assertEqual(original_path.read_bytes(), original); self.assertTrue(variation.is_file())
                        lab.return_value.generate.side_effect = generate
                        replacement=Path(post({'action':'install','channel':'nickelodeon','purpose':purpose,**scope})['path'])
                        self.assertFalse(variation.exists()); self.assertTrue(replacement.is_file())
                        if original_path != replacement: self.assertFalse(original_path.exists())
        finally: server.shutdown(); server.server_close(); thread.join()
