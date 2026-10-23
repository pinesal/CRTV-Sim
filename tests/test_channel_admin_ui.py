import json
import re
import shutil
import subprocess
import unittest

from retrotv.channel_admin_ui import CHANNEL_ADMIN_JS
from retrotv.web import INDEX_HTML, INTERNET_MEDIA_HTML


@unittest.skipUnless(shutil.which('node'), 'Node required for Dashboard behavior checks')
class ChannelAdministrationUITests(unittest.TestCase):
    def run_js(self, script):
        result = subprocess.run(['node'], input=script, capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_folder_actions_job_status_cancel_and_retry_are_independent(self):
        script = r'''
        const assert = require('assert');
        class Element {
          constructor(tag='div'){this.tag=tag;this.children=[];this.textContent='';this.open=false;this.style={};}
          append(...nodes){for(const node of nodes){if(typeof node==='object')node.parent=this;this.children.push(node);}}
          replaceChildren(){this.children=[];}
          remove(){this.parent.children=this.parent.children.filter(node=>node!==this);}
        }
        const elements={}, document={hidden:false,createElement:tag=>new Element(tag),getElementById:id=>elements[id]||=new Element()};
        const window={}; const setInterval=()=>1;
        let missing={available:true,items:[{slug:'nick',display_name:'Nick',path:'Media/Nick',management_identity:'token'}]};
        let jobs={jobs:[{id:'job',slug:'nick',display_name:'Nick',state:'waiting',detail:'Waiting for database'}]};
        let failFolders=false;const calls=[];
        const fetch=async(url,options)=>{
          calls.push([url,options]);
          if(url.endsWith('missing-folders')){if(failFolders)throw Error('busy');return {ok:true,json:async()=>missing};}
          if(url.endsWith('/cancel')){jobs.jobs[0].state='cancelled';jobs.jobs[0].detail='Deletion cancelled';return {ok:true,json:async()=>({cancelled:true})};}
          if(url.endsWith('recreate-folders')){missing.items=[];return {ok:true,json:async()=>({detail:'Folders recreated'})};}
          return {ok:true,json:async()=>jobs};
        };
        const settle=()=>new Promise(resolve=>setImmediate(resolve));
        ''' + CHANNEL_ADMIN_JS + r'''
        (async()=>{
          await settle();
          assert.strictEqual(elements['channel-folder-count'].textContent,'1');
          assert(elements['channel-folder-review'].open);
          const p=elements['channel-folder-items'].children[0];
          const recreate=p.children.find(x=>x.textContent==='Recreate folders');
          const link=p.children.find(x=>x.textContent==='Review deletion');
          assert.strictEqual(link.href,'/internet-media?delete=nick');
          const job=elements['channel-deletion-jobs'].children[0];
          assert(job.textContent.includes('waiting'));
          await job.children.find(x=>x.textContent==='Cancel deletion').onclick();
          assert(elements['channel-deletion-jobs'].children[0].textContent.includes('cancelled'));
          await recreate.onclick();
          const save=calls.find(x=>x[0].endsWith('recreate-folders'));
          assert.strictEqual(JSON.parse(save[1].body).channel_identity,'token');
          assert.strictEqual(elements['channel-folder-count'].textContent,'0');
          failFolders=true;await window.refreshChannelAdministration();
          assert(elements['channel-folder-message'].textContent.includes('Refresh channel status'));
          assert(elements['channel-deletion-jobs'].children[0].textContent.includes('cancelled'));
          failFolders=false;await elements['refresh-channel-admin'].onclick();
          assert.strictEqual(elements['channel-folder-message'].textContent,'');
        })().catch(e=>{console.error(e);process.exitCode=1});
        '''
        self.run_js(script)

    def test_both_pages_have_controls_and_every_script_parses(self):
        for page in (INDEX_HTML, INTERNET_MEDIA_HTML):
            self.assertIn('id="channel-folder-review"', page)
            self.assertIn('/api/channels/missing-folders', page)
            for script in re.findall(r'<script[^>]*>(.*?)</script>', page, re.S):
                self.run_js('new Function(' + json.dumps(script) + ');')
        self.assertIn('Queue channel deletion', INTERNET_MEDIA_HTML)
        self.assertIn('channel_identity:deletePreview.channel_identity', INTERNET_MEDIA_HTML)


if __name__ == '__main__':
    unittest.main()
