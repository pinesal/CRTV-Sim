import shutil
import subprocess
import unittest

from retrotv.web import INDEX_HTML


@unittest.skipUnless(shutil.which('node'), 'Node required for duplicate UI behavior checks')
class DuplicateDeletionUITests(unittest.TestCase):
    def run_js(self, assertions):
        start = INDEX_HTML.index('    let duplicateDeletePreview = null;')
        end = INDEX_HTML.index('    async function reviewAsset(',start)
        harness = r'''
          const assert=require('assert');
          function element(){return {children:[],value:'all',textContent:'',disabled:false,hidden:false,
            append(...nodes){this.children.push(...nodes)},replaceChildren(...nodes){this.children=nodes},
            showModal(){this.open=true},close(){this.open=false}}}
          const elements={}, $=id=>elements[id] ||= element();
          const document={createElement:()=>element()};
          const addCell=(row,text)=>{const cell=element();cell.textContent=text;row.append(cell);return cell};
          const card=()=>element(), byteSize=String;
          const preview={asset_id:1,sha256:'abc',token:'confirmed-token',path:'C:/Media/one.mp4',
            shadows:[{id:5}],remaining:[{id:2,path:'C:/Media/two.mp4'}]};
          let requests=[],failure=null,refreshFailure=false,release=null,refreshes=0;
          const group={sha256:'abc',member_count:2,status:'pending',duplicate_bytes:20,
            members:[{id:1,source_kind:'managed',source_relative_path:'one.mp4',channel_slug:'fox'},
                     {id:2,source_kind:'external',source_relative_path:'two.mp4',channel_slug:'fox'}]};
          async function api(url,options){requests.push({url,options});
            if(failure)throw Error(failure);
            if(url.endsWith('/deletion-preview'))return release ? new Promise(resolve=>release.resolve=resolve) : preview;
            if(url.endsWith('/delete'))return {deleted:preview.path,maintenance_queued:true};
            if(url==='/api/duplicates')return {hashed:2,total:2,groups:[group],pending:1,intentional:0};
            return {};
          }
          const notice=()=>{};
          async function loadShadows(){refreshes++;if(refreshFailure)throw Error('Refresh failed')}
          async function loadStats(){}
          async function loadInventory(){}
          const tick=()=>new Promise(resolve=>setImmediate(resolve));
        '''
        script=harness+INDEX_HTML[start:end]+'(async()=>{'+assertions+'})().catch(e=>{console.error(e);process.exitCode=1});'
        result=subprocess.run(['node'],input=script,text=True,encoding='utf-8',capture_output=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_each_copy_has_preview_and_only_managed_copies_offer_delete(self):
        self.run_js(r'''
          await loadDuplicates();
          const locations=$('duplicates').children[0].children[3].children;
          assert.equal(locations.length,2);
          assert.equal(locations[0].children[0].href,'/api/assets/1/play');
          assert.equal(locations[0].children[0].target,'_blank');
          assert.equal(locations[0].children[2].textContent,'Delete copy');
          assert.equal(locations[1].children[0].href,'/api/assets/2/play');
          assert.equal(locations[1].children[2].textContent,'External — read-only');
          await locations[0].children[2].onclick();
          assert($('duplicate-delete-dialog').open);
          assert.equal($('duplicate-delete-path').textContent,preview.path);
          assert($('duplicate-delete-remaining').textContent.includes('two.mp4'));
          assert(!requests.some(r=>r.url.endsWith('/delete')));
        ''')

    def test_confirm_deletes_only_selected_copy_and_refreshes_without_closing_on_errors(self):
        self.run_js(r'''
          await previewDuplicateDeletion({id:1},'abc');
          failure='Stop TV before deleting';await deleteDuplicateCopy();
          assert($('duplicate-delete-dialog').open);
          assert($('duplicate-delete-error').textContent.includes('Stop TV'));
          assert(!$('duplicate-delete-confirm').disabled);
          failure=null;refreshFailure=true;await deleteDuplicateCopy();
          assert(!$('duplicate-delete-dialog').open);
          assert($('duplicate-message').textContent.startsWith('Deleted'));
          assert($('duplicate-message').textContent.includes('Could not refresh'));
          const sent=JSON.parse(requests.find(r=>r.url.endsWith('/delete')).options.body);
          assert.deepEqual(sent,{asset_id:1,sha256:'abc',token:'confirmed-token'});
          assert.equal(duplicateDeletePreview,null);
          const count=requests.length;await deleteDuplicateCopy();assert.equal(requests.length,count);
        ''')

    def test_preview_failure_and_repeated_clicks_do_not_delete_or_open_wrong_file(self):
        self.run_js(r'''
          release={};const pending=previewDuplicateDeletion({id:1},'abc');await tick();
          await previewDuplicateDeletion({id:2},'abc');assert.equal(requests.length,1);
          release.resolve(preview);await pending;release=null;
          $('duplicate-delete-dialog').close();failure='File changed';
          await previewDuplicateDeletion({id:2},'abc');
          assert.equal(duplicateDeletePreview,null);
          assert(!$('duplicate-delete-dialog').open);
          assert.equal($('duplicate-message').textContent,'File changed');
          assert(!requests.some(r=>r.url.endsWith('/delete')));
        ''')


if __name__=='__main__':
    unittest.main()
