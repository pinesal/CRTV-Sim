import shutil
import subprocess
import unittest

from retrotv.web import INDEX_HTML


@unittest.skipUnless(shutil.which('node'), 'Node required for graphics behavior checks')
class GraphicGenerationUITests(unittest.TestCase):
    def run_script(self, assertions):
        start = INDEX_HTML.index('    function graphicScope(item)')
        end = INDEX_HTML.index('    function chooseGraphicReplacement(item)', start)
        harness = r'''
        const assert=require('assert');
        const elements={};
        const $=id=>elements[id] ||= {value:'',textContent:'',className:'',disabled:false};
        const buttons=[$('generate-graphic'),$('row-generate')];
        const document={querySelectorAll:()=>buttons};
        let graphicGenerationBusy=false,graphicWeeklyDestination='';
        $('graphic-channel').value='cbs-news';$('graphic-purpose').value='interstitials';
        $('graphic-art-direction').value='A newsroom';
        $('graphic-include-station-name').checked=true;
        function notice(message,kind){$('notice').textContent=message;$('notice').className=kind;}
        let confirmValue=true,requests=[],getResponse={items:[]},postError=null,refreshError=null,releaseGet=null;
        const confirm=()=>confirmValue;
        async function api(url,options){
          requests.push({url,options});
          if(!options)return releaseGet ? new Promise(resolve=>releaseGet.resolve=resolve) : getResponse;
          if(postError)throw Error(postError);
          return {ok:true};
        }
        let refreshes=0;
        async function loadGraphics(){refreshes++;if(refreshError)throw Error(refreshError);}
        const tick=()=>new Promise(resolve=>setImmediate(resolve));
        '''
        script = harness + INDEX_HTML[start:end] + '(async()=>{' + assertions + '})().catch(e=>{console.error(e);process.exitCode=1});'
        result = subprocess.run(['node'], input=script, capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejection_is_visible_in_graphics_panel_and_duplicate_clicks_do_not_resubmit(self):
        self.assertIn('id="graphic-generation-status"', INDEX_HTML)
        self.run_script(r'''
          releaseGet={};postError='Your request was rejected by the safety system';
          const pending=generateGraphic();await tick();
          assert($('graphic-generation-status').textContent.includes('Preparing'));
          assert(buttons.every(button=>button.disabled));
          await generateGraphic();assert.strictEqual(requests.length,1);
          releaseGet.resolve({items:[]});await pending;
          assert($('graphic-generation-status').textContent.includes('safety system'));
          assert.strictEqual($('graphic-generation-status').className,'notice error');
          assert(buttons.every(button=>!button.disabled));
          const payload=JSON.parse(requests[1].options.body);
          assert.strictEqual(payload.channel,'cbs-news');assert.strictEqual(payload.purpose,'interstitials');
          assert.strictEqual(payload.holiday,'');assert.strictEqual(payload.daypart,'');
          assert.strictEqual(payload.include_station_name,true);
          assert.strictEqual(refreshes,0);
        ''')

    def test_cancel_and_success_release_buttons_and_refresh_failure_preserves_success(self):
        self.run_script(r'''
          getResponse={items:[{provenance:'ai',channel_slug:'cbs-news',purpose:'interstitials'}]};
          confirmValue=false;await generateGraphic();
          assert($('graphic-generation-status').textContent.includes('cancelled'));
          assert.strictEqual(requests.length,1);assert(buttons.every(button=>!button.disabled));
          confirmValue=true;getResponse={items:[]};await generateGraphic();
          assert($('graphic-generation-status').textContent.includes('installed and ready'));
          refreshError='Catalog unavailable';await generateGraphic();
          assert($('graphic-generation-status').textContent.startsWith('Graphic installed, but'));
          assert(buttons.every(button=>!button.disabled));
          const before=requests.length;$('graphic-purpose').value='channel-music';await generateGraphic();
          assert.strictEqual(requests.length,before);
          assert($('graphic-generation-status').textContent.includes('not channel music'));
        ''')

    def test_unbranded_art_keeps_the_selected_channel_and_exact_destination(self):
        self.assertIn('id="graphic-include-station-name" type="checkbox" checked', INDEX_HTML)
        self.run_script(r'''
          $('graphic-include-station-name').checked=false;
          await generateGraphic({id:'g-1',channel_slug:'cbs-news',purpose:'interstitials',
            daypart_slug:'night',holiday_slug:null,weekly_collection_slug:null});
          const payload=JSON.parse(requests.find(item=>item.options).options.body);
          assert.strictEqual(payload.include_station_name,false);
          assert.strictEqual(payload.channel,'cbs-news');assert.strictEqual(payload.daypart,'night');
          assert.strictEqual(payload.holiday,'');assert.strictEqual(payload.art_note,'A newsroom');
        ''')


if __name__ == '__main__':
    unittest.main()
