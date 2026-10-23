"""Exercise the actual IPTV script without relying on a details toggle event."""
import shutil
import subprocess
import unittest

from retrotv.iptv_ui import IPTV_HTML, IPTV_JS


HARNESS = r'''
const assert=require('assert');
class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.listeners={};this._value=null;
    this._text='';this.className='';this.disabled=false;this.hidden=false;this.checked=false;this.open=false;}
  get textContent(){return this._text}
  set textContent(v){this._text=v;this.children=[]}
  get options(){return this.children.filter(c=>c.tag==='option')}
  get value(){if(this.tag!=='select')return this._value||'';
    return this._value===null?(this.options[0]?.value||''):this.options.some(o=>o.value===this._value)?this._value:''}
  set value(v){this._value=String(v)}
  append(...nodes){for(const node of nodes){node.parent=this;this.children.push(node)}}
  replaceChildren(...nodes){this.children=[];if(this.tag==='select')this._value=null;this.append(...nodes)}
  setAttribute(name,value){this[name]=value}
  addEventListener(name,fn){(this.listeners[name]||=[]).push(fn)}
  emit(name){for(const fn of this.listeners[name]||[])fn()}
  remove(){this.parent.children=this.parent.children.filter(c=>c!==this)}
  querySelectorAll(selector){const result=[];
    const matches=node=>selector.startsWith('.')?node.className.split(' ').includes(selector.slice(1)):node.tag===selector;
    for(const child of this.children){if(matches(child))result.push(child);result.push(...child.querySelectorAll(selector))}return result;}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null}
}
const elements={};const $=id=>elements[id]||=new Element(['iptv-selection','iptv-presentation','iptv-new-scope'].includes(id)?'select':'div');
const document={createElement:tag=>new Element(tag)};
function Option(text,value){const o=new Element('option');o.textContent=text;o.value=value;return o}
$('iptv-presentation').replaceChildren(new Option('Preserve','preserve'));
$('iptv-new-scope').replaceChildren(new Option('all-day','all-day'));
const setInterval=()=>1,confirm=()=>true;
const timers=new Map();let nextTimer=0;
const setTimeout=(fn,delay)=>{const id=++nextTimer;timers.set(id,{fn,delay});return id};
const clearTimeout=id=>timers.delete(id);
let catalog={feeds:[],channels:[{slug:'fox',display_name:'FOX'},{slug:'pbs',display_name:'PBS'}],
  scopes:['all-day','daytime','morning','afternoon','night','custom'],
  weekly:[{slug:'late',channel:'fox',name:'Late Special'}],conflicts:[]};
let failCatalog=false,hangOptions=false,hangConflicts=false,conflictReplies=[];const requests=[];
function stalled(signal){return new Promise((resolve,reject)=>{signal.addEventListener('abort',()=>reject(Error('aborted')),{once:true})})}
async function api(path,options){requests.push({path,payload:options?.body?JSON.parse(options.body):null});
  if(path==='/api/iptv/options'){if(hangOptions)return stalled(options.signal);if(failCatalog)throw Error('Catalog unavailable');return catalog}
  if(path==='/api/iptv/conflicts'){
    if(hangConflicts)return stalled(options.signal);
    if(conflictReplies.length)return conflictReplies.shift();
    return {items:[],complete:true,detail:null};}
  return {id:1,message:'Saved.'};}
function rows(){return $('iptv-new-assignment').querySelectorAll('.iptv-assignment')}
function passed(){iptvTestId='ticket';iptvPreviewState({test_id:'ticket',usable:true,state:'passed'})}
function saved(){return requests.filter(r=>r.path==='/api/iptv'&&r.payload)}
'''


@unittest.skipUnless(shutil.which('node'), 'Node required for IPTV UI behavior tests')
class IPTVUITests(unittest.TestCase):
    def run_ui(self, assertions, *, setup=''):
        script = HARNESS + setup + IPTV_JS + '\n(async()=>{\n' + assertions + r'''
        })().catch(error=>{console.error(error);process.exitCode=1});
        '''
        result = subprocess.run(['node', '-'], input=script, capture_output=True,
                                text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_initial_page_load_populates_labelled_assignments_without_toggle(self):
        self.run_ui(r'''
          assert.equal(requests.filter(r=>r.path==='/api/iptv/options').length,1);
          await loadIPTV(); // Coalesces startup and an immediately opened panel.
          assert.equal(requests.filter(r=>r.path==='/api/iptv/options').length,1);
          assert.equal(rows().length,1);
          const row=rows()[0],selects=row.querySelectorAll('select');
          assert.deepEqual(selects[0].options.map(o=>o.value),['','fox','pbs']);
          assert.ok(selects[1].options.some(o=>o.value==='custom'));
          assert.equal(selects[1].value,'all-day');
          assert.deepEqual(row.querySelectorAll('label').map(n=>n.textContent),
            ['Existing channel','Daypart or weekly special','Guide label']);
          assert.equal($('iptv-add').disabled,true); // A catalog is not a playback test.
          assert.equal($('iptv-existing-destination').hidden,false);
        ''')

    def test_existing_channel_and_daypart_are_submitted_and_refresh_keeps_unsaved_edits(self):
        self.run_ui(r'''
          await loadIPTV();
          let selects=rows()[0].querySelectorAll('select');
          selects[0].value='fox';selects[0].onchange();selects[1].value='morning';
          rows()[0].querySelector('input').value='Morning feed';
          // New channels become selectable after refreshing, without losing edits.
          catalog.channels.push({slug:'tnt',display_name:'TNT'});await loadIPTV();
          selects=rows()[0].querySelectorAll('select');
          assert.equal(selects[0].value,'fox');assert.equal(selects[1].value,'morning');
          assert.ok(selects[0].options.some(o=>o.value==='tnt'));
          assert.equal(rows()[0].querySelector('input').value,'Morning feed');
          $('iptv-url').value='http://lan/live.ts';$('iptv-direct').checked=true;passed();
          await $('iptv-add').onclick();
          assert.deepEqual(saved()[0].payload.assignments,[{channel:'fox',scope:'morning',weekly_slug:'',display_name:'Morning feed'}]);
          assert.equal(saved()[0].payload.new_channel,'');
          assert.equal(saved()[0].payload.test_id,'ticket');
        ''')

    def test_multiple_assignments_and_weekly_options_remain_supported(self):
        self.run_ui(r'''
          await loadIPTV();let selects=rows()[0].querySelectorAll('select');
          selects[0].value='fox';selects[0].onchange();selects[1].value='weekly:late';
          $('iptv-new-assignment').children.at(-1).onclick();
          selects=rows()[1].querySelectorAll('select');selects[0].value='pbs';selects[0].onchange();selects[1].value='custom';
          passed();await $('iptv-add').onclick();
          assert.deepEqual(saved()[0].payload.assignments.map(a=>[a.channel,a.scope,a.weekly_slug]),
            [['fox','all-day','late'],['pbs','custom','']]);
        ''')

    def test_new_channel_has_explicit_scope_even_when_no_existing_channels(self):
        self.run_ui(r'''
          await loadIPTV();assert.equal(rows()[0].querySelector('select').value,'');
          $('iptv-new-channel').value='New Live Channel';$('iptv-new-channel').emit('input');
          assert.equal($('iptv-existing-destination').hidden,true);
          assert.equal($('iptv-new-destination').hidden,false);
          $('iptv-new-scope').value='night';$('iptv-number').value='53';
          await loadIPTV();assert.equal($('iptv-new-scope').value,'night');
          passed();await $('iptv-add').onclick();
          assert.equal(saved()[0].payload.new_channel,'New Live Channel');
          assert.equal(saved()[0].payload.scope,'night');assert.equal(saved()[0].payload.channel_number,'53');
          assert.deepEqual(saved()[0].payload.assignments,[]);
          assert.equal($('iptv-existing-destination').hidden,false);
        ''', setup='catalog.channels=[];\n')

    def test_missing_assignment_is_not_silently_saved_and_scopes_survive_channel_change(self):
        self.run_ui(r'''
          await loadIPTV();passed();await $('iptv-add').onclick();
          assert.equal(saved().length,0);assert.match($('iptv-message').textContent,/Choose an existing channel/);
          const selects=rows()[0].querySelectorAll('select');
          selects[0].value='fox';selects[0].onchange();selects[1].value='morning';
          selects[0].value='pbs';selects[0].onchange();assert.equal(selects[1].value,'morning');
          selects[0].value='fox';selects[0].onchange();selects[1].value='weekly:late';
          selects[0].value='pbs';selects[0].onchange();assert.equal(selects[1].value,'all-day');
          rows()[0].remove();await $('iptv-add').onclick();assert.equal(saved().length,0);
        ''')

    def test_failed_initial_options_load_is_visible_and_refresh_retries(self):
        self.run_ui(r'''
          await iptvLoadPromise.catch(()=>{});await Promise.resolve();
          assert.equal(iptvData,null);assert.match($('iptv-assignment-status').textContent,/Refresh status to retry/);
          passed();assert.equal($('iptv-add').disabled,true);
          failCatalog=false;await $('iptv-refresh').onclick();
          assert.equal(rows().length,1);assert.equal($('iptv-add').disabled,false);
        ''', setup='failCatalog=true;\n')

    def test_slow_conflicts_cannot_block_assignment_options_or_saving(self):
        self.run_ui(r'''
          await loadIPTV();assert.equal(rows().length,1);
          assert.ok(!$('iptv-assignment-status').textContent.includes('Loading'));
          assert.match($('iptv-conflict-status').textContent,/Checking/);
          const selects=rows()[0].querySelectorAll('select');
          selects[0].value='fox';selects[0].onchange();selects[1].value='morning';
          passed();assert.equal($('iptv-add').disabled,false);
          await $('iptv-add').onclick();assert.equal(saved().length,1);
          // Abort the current diagnostics; the independently loaded editor stays usable.
          iptvConflictController.abort();await iptvConflictPromise;
          assert.equal(rows()[0].querySelector('select').value,'fox');
          assert.match($('iptv-conflict-status').textContent,/unavailable/);
        ''', setup='hangConflicts=true;\n')

    def test_options_timeout_is_visible_and_does_not_poison_retry(self):
        self.run_ui(r'''
          const pending=iptvLoadPromise;passed();assert.equal($('iptv-add').disabled,true);
          assert.equal([...timers.values()][0].delay,10000);
          [...timers.values()][0].fn();await pending.catch(()=>{});
          assert.equal(iptvLoadPromise,null);
          assert.match($('iptv-assignment-status').textContent,/timed out/);
          hangOptions=false;await $('iptv-refresh').onclick();
          assert.equal(rows().length,1);assert.equal($('iptv-add').disabled,false);
        ''', setup='hangOptions=true;\n')

    def test_refresh_cancels_a_stalled_options_request_without_waiting_for_timeout(self):
        self.run_ui(r'''
          hangOptions=false;await $('iptv-refresh').onclick();
          assert.equal(requests.filter(r=>r.path==='/api/iptv/options').length,2);
          assert.equal(rows().length,1);assert.equal($('iptv-refresh').disabled,false);
        ''', setup='hangOptions=true;\n')

    def test_incomplete_diagnostics_are_not_reported_as_no_conflicts(self):
        self.run_ui(r'''
          await loadIPTV();await iptvConflictPromise;
          assert.equal(rows().length,1);
          assert.match($('iptv-conflict-status').textContent,/timed out/);
        ''', setup="conflictReplies=[{items:[],complete:false,detail:'Conflict check timed out; retry.'}];\n")


class IPTVFormTests(unittest.TestCase):
    def test_loading_state_and_new_channel_scope_are_present_in_html(self):
        self.assertIn('id="iptv-assignment-status"', IPTV_HTML)
        self.assertIn('Loading channel and daypart options', IPTV_HTML)
        self.assertIn('id="iptv-new-scope"', IPTV_HTML)


if __name__ == '__main__':
    unittest.main()
