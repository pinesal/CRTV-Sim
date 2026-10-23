from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from retrotv.graphics_lab import _png_rgba
from retrotv.web import make_handler
import test_graphics as fixtures
import test_channel_deletion as request_fixtures


class GraphicGenerationAPITests(unittest.TestCase):
    setUp = fixtures.GraphicsTests.setUp
    tearDown = fixtures.GraphicsTests.tearDown
    request = request_fixtures.ChannelDeletionAPITests.request

    def test_unbranded_request_is_forwarded_without_changing_channel_or_destination(self):
        self.config.data['storage_roots'].update(state=str(self.root/'state'),previews=str(self.root/'previews'))
        self.connection.execute("INSERT INTO channels(slug,display_name) VALUES('cbs-news','Cbs News')")
        self.connection.commit()
        lab = Mock()
        lab.root = self.root/'mock-lab'
        def generate(payload):
            self.assertEqual(payload['channel_name'], 'Cbs News')
            self.assertIs(payload['include_station_name'], False)
            self.assertEqual(payload['art_direction'], 'A generic newsroom')
            run = lab.root/'mock-run'
            run.mkdir(parents=True)
            (run/'background-640x480.png').write_bytes(_png_rgba(640,480,b'\x00\x00\x00\xff'*(640*480)))
            return {'id':'mock-run','model':'mock-image'}
        lab.generate.side_effect=generate
        with patch('retrotv.web.GraphicsLab', return_value=lab):
            status, result = self.request(make_handler(self.config), '/api/graphics/generate', {
                'action':'install','channel':'cbs-news','purpose':'interstitials','daypart':'', 'holiday':'',
                'include_station_name':False,'art_note':'A generic newsroom'})
        self.assertEqual(status,200, result)
        self.assertTrue(result['ok'])
        self.assertEqual(Path(result['path']),self.media/'cbs-news/graphics/interstitials/ai-station.png')
        self.assertEqual(self.connection.execute("SELECT display_name FROM channels WHERE slug='cbs-news'").fetchone()[0],'Cbs News')
        row=self.connection.execute("SELECT channel_id,purpose,daypart_slug,holiday_slug FROM graphic_assets WHERE relative_path='cbs-news/graphics/interstitials/ai-station.png'").fetchone()
        self.assertEqual(tuple(row)[1:],('interstitials',None,None))

    def test_invalid_name_option_rejected_before_cloud_request(self):
        self.config.data['storage_roots']['previews']=str(self.root/'previews')
        with patch('retrotv.web.GraphicsLab') as lab:
            status, result=self.request(make_handler(self.config), '/api/graphics/generate', {
                'action':'install','include_station_name':'false'})
        self.assertEqual(status,400)
        self.assertIn('true or false',result['error'])
        lab.assert_not_called()


if __name__=='__main__':
    unittest.main()
