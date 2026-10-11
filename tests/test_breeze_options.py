import json
import ssl
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import Mock, patch

import httpx
import test_ai_announcements as fixture
from retrotv.tts import BreezeProviderError, BreezeTTSProvider
from retrotv.web import make_handler


class BreezeCatalogTests(unittest.TestCase):
    def test_large_catalog_fetches_one_page_and_uses_full_request_timeout(self):
        client = Mock()
        client.models.list.return_value = [{'model_id': 'breeze-tts-2'}]
        client.voices.search.return_value = {
            'voices': [{'voice_id': 'a', 'name': 'A'}],
            'has_more': True, 'next_page_token': 'page-two', 'total_count': 10000,
        }
        options = BreezeTTSProvider(client=client, timeout_seconds=90).options()
        self.assertEqual(options['voices'], [{'id': 'a', 'name': 'A'}])
        self.assertTrue(options['has_more'])
        client.voices.search.assert_called_once_with(
            page=1, next_page_token=None, search=None, page_size=50, timeout=90)
        client.models.list.assert_called_once_with(timeout=90)

    def test_search_and_page_only_token_pagination(self):
        client = Mock()
        client.voices.search.return_value = {'voices': [{'voice_id': 'a'}, {'voice_id': 'a'}],
                                              'has_more': True}
        options = BreezeTTSProvider(client=client).options(page=2, search='announcer', include_models=False)
        self.assertEqual(options['voices'], [{'id': 'a', 'name': 'a'}])
        self.assertTrue(options['has_more'])
        self.assertIsNone(options['next_page_token'])
        client.models.list.assert_not_called()
        client.voices.search.assert_called_once_with(
            page=2, next_page_token=None, search='announcer', page_size=50, timeout=90)

    def test_repeated_token_is_rejected(self):
        client = Mock()
        client.voices.search.return_value = {'voices': [], 'next_page_token': 'same'}
        with self.assertRaisesRegex(BreezeProviderError, 'repeated voice page'):
            BreezeTTSProvider(client=client).options(page=2, next_page_token='same', include_models=False)

    def test_models_and_voice_timeouts_are_distinguished_without_exposing_key(self):
        client = Mock()
        client.models.list.return_value = []
        for operation, resource in [('loading models', client.models.list),
                                    ('loading voices', client.voices.search)]:
            with self.subTest(operation=operation):
                resource.side_effect = httpx.ReadTimeout('private-api-key')
                with self.assertLogs('retrotv.tts') as captured:
                    with self.assertRaises(BreezeProviderError) as error:
                        BreezeTTSProvider(client=client).options()
                self.assertIn('request timed out', str(error.exception))
                self.assertIn(operation, str(error.exception))
                self.assertNotIn('private-api-key', str(error.exception) + ''.join(captured.output))
                resource.side_effect = None

    def test_certificate_and_connection_failures_are_distinguished(self):
        client = Mock()
        certificate = httpx.ConnectError('private-api-key')
        certificate.__cause__ = ssl.SSLCertVerificationError('CERTIFICATE_VERIFY_FAILED')
        for cause, expected in [(certificate, 'certificate validation failed'),
                                (httpx.ConnectError('private-api-key'), 'could not connect')]:
            client.models.list.side_effect = cause
            with self.assertLogs('retrotv.tts'):
                with self.assertRaisesRegex(BreezeProviderError, expected) as error:
                    BreezeTTSProvider(client=client).options()
            self.assertNotIn('private-api-key', str(error.exception))

    def test_real_sdk_uses_verified_context_and_preserves_key_header(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(request.headers['xi-api-key'], 'test-only-key')
            if request.url.path == '/v1/models':
                return httpx.Response(200, json=[{'model_id': 'breeze-tts-2'}])
            self.assertEqual(request.url.params['page_size'], '50')
            return httpx.Response(200, json={'voices': [{'voice_id': 'available-voice'}], 'has_more': False})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        context = ssl.create_default_context()
        with patch('retrotv.tts.configured_breeze_api_key', return_value='test-only-key'), \
             patch('retrotv.ai_announcements.verified_ssl_context', return_value=context), \
             patch('httpx.Client', return_value=client) as factory:
            provider = BreezeTTSProvider()
            try:
                options = provider.options()
                factory.assert_called_once_with(verify=context, timeout=90)
                self.assertEqual(options['voices'][0]['id'], 'available-voice')
                self.assertEqual(len(requests), 2)
            finally:
                provider.close()
        self.assertTrue(client.is_closed)


class BreezeOptionsAPITests(unittest.TestCase):
    setUp = fixture.AIAnnouncementTests.setUp
    tearDown = fixture.AIAnnouncementTests.tearDown

    def test_options_endpoint_routes_page_search_and_token_and_closes_connection(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.config))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch('retrotv.web.configured_breeze_api_key', return_value='test-only-key'), \
                 patch('retrotv.web.BreezeTTSProvider') as factory:
                factory.return_value.options.return_value = {'models': [], 'voices': [],
                                                              'has_more': False, 'page': 2}
                with urlopen(f'http://127.0.0.1:{server.server_port}/api/tts/provider-options?page=2&search=warm%20voice&next_page_token=next') as response:
                    result = json.load(response)
                self.assertTrue(result['configured'])
                factory.return_value.options.assert_called_once_with(
                    page=2, search='warm voice', next_page_token='next', include_models=False)
                factory.return_value.close.assert_called_once()
                with self.assertRaises(HTTPError) as error:
                    urlopen(f'http://127.0.0.1:{server.server_port}/api/tts/provider-options?page=0')
                self.assertEqual(error.exception.code, 400)
                factory.return_value.options.assert_called_once()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
