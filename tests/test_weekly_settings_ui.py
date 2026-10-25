import shutil
import subprocess
import unittest

from retrotv.web import SETTINGS_HTML
from retrotv.settings import SettingsService, settings_override_path
import json
import test_settings as fixtures


class WeeklySettingsSaveTests(unittest.TestCase):
    setUp=fixtures.SettingsServiceTests.setUp
    tearDown=fixtures.SettingsServiceTests.tearDown

    def test_save_and_reload_preserves_modes_and_rejects_invalid_changes(self):
        window={'channel':'fox','collection':'snick','weekdays':['saturday'],
                'start':'20:00','end':'22:00','enabled':False,'schedule_mode':'recurring'}
        service=SettingsService(self.config)
        service.save({'windows':{'snick':window}})
        self.assertEqual(service.snapshot([])['windows']['snick']['schedule_mode'],'recurring')
        path=settings_override_path(self.config)
        self.assertEqual(json.loads(path.read_text())['seasonal']['windows']['snick']['schedule_mode'],'recurring')
        with self.assertRaises(ValueError):service.save({'windows':{'snick':dict(window,schedule_mode='invalid')}})
        self.assertEqual(service.snapshot([])['windows']['snick']['schedule_mode'],'recurring')
        service.save({'windows':{'snick':dict(window,schedule_mode='random')}})
        self.assertEqual(service.snapshot([])['windows']['snick']['schedule_mode'],'random')


@unittest.skipUnless(shutil.which('node'),'Node required for settings UI behavior')
class WeeklySettingsUITests(unittest.TestCase):
    def test_render_and_serialize_independent_weekly_modes(self):
        render=SETTINGS_HTML[SETTINGS_HTML.index('    function channelOptions('):SETTINGS_HTML.index('    function wireRemoveButtons(')]
        collect_line=next(line for line in SETTINGS_HTML.splitlines() if "rows('window-rows').forEach" in line)
        script=r'''
          const assert=require('assert');
          const state={channels:[{slug:'fox',display_name:'FOX'}],windows:{
            one:{channel:'fox',schedule_mode:'recurring'},two:{channel:'fox',schedule_mode:'random'}}};
          const element={innerHTML:''},$=()=>element,esc=value=>String(value||''),removeButton=()=>'';
        '''+render+r'''
          renderWindows();
          assert(element.innerHTML.includes('<select class="schedule-mode">'));
          assert(element.innerHTML.includes('value="recurring" selected'));
          assert(element.innerHTML.includes('value="random" selected'));
          let chosen='recurring';
          const values={'.slug':'one','.channel':'fox','.weekdays':'saturday','.start':'20:00',
            '.end':'22:00','.collection':'snick','.label':'SNICK','.priority':'300'};
          const rows=()=>[{querySelector:key=>key==='.enabled'?{checked:true}:
              {value:key==='.schedule-mode'?chosen:values[key]}}];
          function collect(){const windows={};
        '''+collect_line+r'''
          return windows;}
          assert.equal(collect().one.schedule_mode,'recurring');
          chosen='random';assert.equal(collect().one.schedule_mode,'random');
          assert.equal(collect().one.collection,'snick');
        '''
        result=subprocess.run(['node'],input=script,text=True,encoding='utf-8',capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn("weeklyModeOptions('random')",SETTINGS_HTML)


if __name__=='__main__':unittest.main()
