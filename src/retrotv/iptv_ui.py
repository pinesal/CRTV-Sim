IPTV_HTML = '''
<style>
#iptv-panel {background:var(--panel);border:1px solid var(--line);border-radius:12px;margin-bottom:16px;}
#iptv-panel summary {cursor:pointer;padding:18px;}
#iptv-panel summary h2 {display:inline;margin:0;}
#iptv-panel .section-body {padding:0 18px 18px;}
#iptv-panel .toolbar {display:flex;flex-wrap:wrap;gap:10px;margin-bottom:12px;}
#iptv-panel button:disabled {opacity:.5;cursor:default;}
#iptv-panel [hidden] {display:none!important;}
#iptv-panel .iptv-source {align-items:end;}
#iptv-panel .iptv-source > label:first-child {display:flex;flex-direction:column;gap:6px;flex:1;min-width:260px;}
#iptv-panel .iptv-source > label:last-child {display:flex;align-items:center;gap:8px;}
#iptv-panel input[type=checkbox] {width:16px;min-width:16px;flex:0 0 16px;margin:0;}
#iptv-panel .iptv-fields {display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;}
#iptv-panel .iptv-fields label {display:flex;flex-direction:column;gap:6px;}
#iptv-panel .iptv-fields input,#iptv-panel .iptv-fields select {width:100%;}
#iptv-panel .iptv-assignment {display:grid;grid-template-columns:minmax(150px,1fr) minmax(180px,1fr) minmax(180px,2fr) auto;gap:10px;}
#iptv-panel .iptv-assignment label {display:flex;flex-direction:column;gap:6px;min-width:0;}
#iptv-panel .iptv-assignment input,#iptv-panel .iptv-assignment select {width:100%;min-width:0;}
#iptv-panel .actions {display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;}
#iptv-panel p {overflow-wrap:anywhere;}
@media(max-width:750px) {#iptv-panel .iptv-assignment {grid-template-columns:1fr;}}
</style>
<details class="collapsible" data-section="iptv" id="iptv-panel">
  <summary><h2>IPTV Live Feeds</h2></summary><div class="section-body">
  <p>Receive a live HTTP/HTTPS feed on this PC. No CRTV Sim ads or other programming are inserted while live.
  Shared programs are ignored; channel-specific programs in the same active window cause a conflict.</p>
  <div class="toolbar iptv-source"><label>Stream or M3U playlist URL <input id="iptv-url" type="url" size="65"></label>
  <button id="iptv-inspect">Inspect URL</button><label><input id="iptv-direct" type="checkbox">Direct stream (skip playlist inspection)</label></div>
  <div class="toolbar iptv-fields"><label>Feed <select id="iptv-selection"></select></label><label>Display name <input id="iptv-name"></label>
  <label>Presentation <select id="iptv-presentation"><option value="preserve">Preserve original</option>
  <option value="fill_4_3">Fill 4:3 — center crop</option><option value="stretch_4_3">Stretch to 4:3</option></select></label>
  <label>New channel (optional) <input id="iptv-new-channel"></label><label>Number <input id="iptv-number" type="number" min="1" max="999"></label></div>
  <p>Test the selected feed before saving. A separate preview opens on the PC running CRTV Sim.
  No channel or folders are created until the test passes and you choose Add feed.</p>
  <p>Fill 4:3 crops edges without stretching; logos or subtitles near the edges may be cut off.
  Stretch to 4:3 keeps the full picture but changes its proportions. These are playback-only settings.</p>
  <div id="iptv-existing-destination"><h3>Assign to existing channels</h3>
  <div id="iptv-new-assignment"></div></div>
  <div id="iptv-new-destination" hidden><label>New channel daypart
  <select id="iptv-new-scope"><option value="all-day">all-day</option></select></label></div>
  <p id="iptv-assignment-status" role="status">Loading channel and daypart options…</p>
  <div class="actions"><button id="iptv-test-selection">Test selected feed</button>
  <button id="iptv-add" disabled>Add feed</button><button id="iptv-refresh">Refresh status</button>
  <button id="iptv-stop-test">Close preview</button></div>
  <p id="iptv-message" role="status"></p><p id="iptv-preview-state" role="status"></p>
  <p id="iptv-conflict-status" role="status"></p><div id="iptv-conflicts"></div><div id="iptv-feeds"></div>
  </div></details>
'''

IPTV_JS = r'''
let iptvData=null, iptvInspection=null, iptvTestId=null, iptvTestUsable=false, iptvRevision=0, iptvLoadPromise=null;
let iptvLoadController=null, iptvConflictController=null, iptvConflictRevision=0, iptvConflictPromise=null;
const iptvPost=(path,body={})=>api('/api/iptv'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
function iptvSelection(){return {url:$('iptv-url').value.trim(),direct:$('iptv-direct').checked,
  selection_key:$('iptv-direct').checked?'direct':$('iptv-selection').value,presentation:$('iptv-presentation').value};}
function iptvInvalidateTest(){iptvRevision++;iptvTestId=null;iptvTestUsable=false;$('iptv-add').disabled=true;
  $('iptv-preview-state').textContent='Test the selected feed before saving.';}
function iptvPreviewState(p){$('iptv-preview-state').textContent=`Preview: ${p.state} · ${p.detail||''}`;
  iptvTestUsable=!!iptvTestId&&p.test_id===iptvTestId&&!!p.usable;
  iptvUpdateDestination();}
function iptvUpdateDestination(){
  const creating=!!$('iptv-new-channel').value.trim();
  $('iptv-existing-destination').hidden=creating;
  $('iptv-new-destination').hidden=!creating;
  $('iptv-number').disabled=!creating;
  $('iptv-add').disabled=!iptvTestUsable||!iptvData;
}
function iptvEditor(host, assignments=[]) {
  host.replaceChildren();
  function row(a={}) {
    const div=document.createElement('div');div.className='toolbar iptv-assignment';
    const channel=document.createElement('select');channel.setAttribute('aria-label','Channel');
    channel.replaceChildren(new Option('Choose a scheduled channel',''),...iptvData.channels.map(c=>new Option(c.display_name,c.slug)));
    const previousChannel=a.channel_slug||a.channel;
    if(previousChannel)channel.value=previousChannel;
    const scope=document.createElement('select');scope.setAttribute('aria-label','Scope or weekly special');
    function scopes(){const previous=scope.value;scope.replaceChildren(...iptvData.scopes.map(s=>new Option(s,s)),...iptvData.weekly.filter(w=>w.channel===channel.value).map(w=>new Option('Weekly: '+w.name,'weekly:'+w.slug)));
      scope.value=[...scope.options].some(o=>o.value===previous)?previous:'all-day';}
    scopes();channel.onchange=scopes;scope.value=a.weekly_slug?'weekly:'+a.weekly_slug:a.scope||'all-day';
    const name=document.createElement('input');name.placeholder='Optional Guide label';name.value=a.display_name||'';name.setAttribute('aria-label','Guide label');
    const remove=document.createElement('button');remove.textContent='Remove assignment';remove.onclick=()=>div.remove();
    for(const [text,control] of [['Existing channel',channel],['Daypart or weekly special',scope],['Guide label',name]]){
      const label=document.createElement('label');label.textContent=text;label.append(control);div.append(label);
    }
    div.append(remove);host.append(div);
  }
  assignments.forEach(row);
  const add=document.createElement('button');add.textContent='Add assignment';add.onclick=()=>row();host.append(add);
}
function iptvAssignments(host){return [...host.querySelectorAll('.iptv-assignment')].map(r=>{const selects=r.querySelectorAll('select'),v=selects[1].value;return {channel:selects[0].value,scope:v.startsWith('weekly:')?'all-day':v,weekly_slug:v.startsWith('weekly:')?v.slice(7):'',display_name:r.querySelector('input').value};});}
async function iptvAction(action){try{const result=await action();$('iptv-message').textContent=result.message||result.error||'Saved.';await loadIPTV();}catch(e){$('iptv-message').textContent=e.message;}}
function loadIPTV(){
  if(iptvLoadPromise)return iptvLoadPromise;
  const controller=new AbortController();iptvLoadController=controller;
  const timer=setTimeout(()=>controller.abort(),10000);
  iptvLoadPromise=refreshIPTV(controller.signal).catch(error=>{
    const message=controller.signal.aborted?'Loading options timed out or was cancelled.':error.message;
    $('iptv-assignment-status').textContent='Could not load channel and daypart options. Choose Refresh status to retry. '+message;
    throw new Error(message);
  }).finally(()=>{clearTimeout(timer);iptvLoadPromise=null;iptvLoadController=null;});
  return iptvLoadPromise;
}
async function refreshIPTV(signal){
  // Assignment controls must not wait for schedule-conflict diagnostics.
  const data=await api('/api/iptv/options',{signal});
  if(!['channels','scopes','weekly','feeds'].every(key=>Array.isArray(data[key])))
    throw new Error('The server returned invalid IPTV options.');
  const editor=$('iptv-new-assignment'),previous=editor.children.length?iptvAssignments(editor):[{scope:'all-day'}];
  iptvData=data;
  iptvEditor(editor,previous);
  const newScope=$('iptv-new-scope'),previousScope=newScope.value;
  newScope.replaceChildren(...iptvData.scopes.map(s=>new Option(s,s)));
  newScope.value=iptvData.scopes.includes(previousScope)?previousScope:'all-day';
  $('iptv-assignment-status').textContent=iptvData.channels.length
    ? 'Choose an existing scheduled channel and daypart, or enter a new channel name above.'
    : 'No scheduled channels are available. Enter a new channel name and choose its daypart.';
  iptvUpdateDestination();
  const host=$('iptv-feeds');host.replaceChildren();
  for(const f of iptvData.feeds){
    const card=document.createElement('section');card.className='panel';
    const title=document.createElement('h3');title.textContent=f.name;card.append(title);
    const source=document.createElement('p');source.textContent=f.source_url;card.append(source);
    const status=document.createElement('p');status.textContent=`${f.enabled?'Enabled':'Disabled'} · ${f.availability} · ${f.detail||f.refresh_error||'Not yet tested'}`;card.append(status);
    const name=document.createElement('input');name.value=f.name;name.setAttribute('aria-label','Feed display name');card.append(name);
    const presentationLabel=document.createElement('label');presentationLabel.textContent='Presentation';
    const presentation=document.createElement('select');presentation.setAttribute('aria-label','Feed presentation');
    presentation.replaceChildren(...[['preserve','Preserve original'],['fill_4_3','Fill 4:3 — center crop'],['stretch_4_3','Stretch to 4:3']].map(([value,label])=>new Option(label,value)));
    presentation.value=f.presentation||'preserve';presentationLabel.append(presentation);card.append(presentationLabel);
    const assignments=document.createElement('div');iptvEditor(assignments,f.assignments);card.append(assignments);
    const actions=document.createElement('div');actions.className='actions';card.append(actions);
    function button(label,fn){const b=document.createElement('button');b.textContent=label;b.onclick=()=>iptvAction(fn);actions.append(b);}
    button('Save',()=>iptvPost('/'+f.id,{name:name.value,presentation:presentation.value,assignments:iptvAssignments(assignments)}));
    button(f.enabled?'Disable':'Enable',()=>iptvPost('/'+f.id,{enabled:!f.enabled}));
    button('Refresh playlist',()=>iptvPost('/'+f.id+'/refresh'));
    button('Test feed',()=>iptvPost('/'+f.id+'/test',{presentation:presentation.value}));
    button('Remove',async()=>{if(!confirm('Remove this feed and its assignments? Affected unlocked schedules will be rebuilt. No media files are deleted.'))return {};return iptvPost('/'+f.id+'/remove');});
    host.append(card);
  }
  iptvConflictPromise=loadIPTVConflicts();
}
async function loadIPTVConflicts(){
  iptvConflictController?.abort();
  const controller=new AbortController(),revision=++iptvConflictRevision;iptvConflictController=controller;
  const timer=setTimeout(()=>controller.abort(),10000);
  $('iptv-conflict-status').textContent='Checking schedule conflicts…';
  try{
    const data=await api('/api/iptv/conflicts',{signal:controller.signal});
    if(revision!==iptvConflictRevision)return;
    if(!data.complete){$('iptv-conflict-status').textContent=data.detail||'Conflict status unavailable. Choose Refresh status to retry.';return;}
    const conflicts=$('iptv-conflicts');conflicts.replaceChildren();
    for(const c of data.items){const p=document.createElement('p');p.className='warning';const facts=JSON.parse(c.explanation_json).iptv;p.textContent=`Conflict: ${c.slug} · ${facts.weekly||c.daypart_slug} · ${c.starts_at} — ${c.ends_at}. ${(facts.local_conflicts||[]).map(v=>v.placement).join('; ')||'Competing feed assignments'}`;conflicts.append(p);}
    $('iptv-conflict-status').textContent='';
  }catch(error){if(revision===iptvConflictRevision)$('iptv-conflict-status').textContent='Conflict status unavailable. Channel and daypart options are ready. Choose Refresh status to retry.';}
  finally{clearTimeout(timer);if(revision===iptvConflictRevision)iptvConflictController=null;}
}
$('iptv-url').addEventListener('input',()=>{iptvInspection=null;iptvInvalidateTest();});
$('iptv-direct').onchange=()=>{iptvInvalidateTest();$('iptv-selection').disabled=$('iptv-direct').checked;};
$('iptv-selection').onchange=iptvInvalidateTest;
$('iptv-presentation').onchange=iptvInvalidateTest;
$('iptv-new-channel').addEventListener('input',iptvUpdateDestination);
$('iptv-inspect').onclick=async()=>{iptvInvalidateTest();const revision=iptvRevision;try{const inspected=await iptvPost('/inspect',{url:$('iptv-url').value});if(revision!==iptvRevision)return;iptvInspection=inspected;$('iptv-selection').replaceChildren(...iptvInspection.feeds.map(f=>{const o=new Option(f.name+(f.ambiguous?' (ambiguous)':''),f.key);o.disabled=!!f.ambiguous;return o;}));const skipped=iptvInspection.skipped_entries?.length||0;$('iptv-message').textContent=`${iptvInspection.feeds.length} feed(s). ${skipped?`${skipped} unsupported or invalid entries skipped. `:''}Select one and test it before saving.`;}catch(e){if(revision===iptvRevision)$('iptv-message').textContent=e.message+' You can test a known direct stream without inspection.';}};
$('iptv-test-selection').onclick=async()=>{iptvInvalidateTest();const revision=iptvRevision;const button=$('iptv-test-selection');button.disabled=true;try{
  if(!$('iptv-direct').checked&&(!iptvInspection||iptvInspection.url!==$('iptv-url').value.trim()))throw new Error('Inspect this URL first, or select Direct stream.');
  $('iptv-message').textContent='Testing selected feed…';const p=await iptvPost('/preview-test',iptvSelection());if(revision!==iptvRevision)return;
  iptvTestId=p.test_id;iptvPreviewState(p);$('iptv-message').textContent='Once video decodes successfully, Add feed becomes available.';
}catch(e){if(revision===iptvRevision)$('iptv-message').textContent=e.message;}finally{button.disabled=false;}};
$('iptv-add').onclick=()=>iptvAction(async()=>{
  if(!iptvTestUsable)throw new Error('Test the selected feed successfully before saving.');
  if(!iptvData)throw new Error('Load channel and daypart options with Refresh status before saving.');
  const newChannel=$('iptv-new-channel').value.trim(),assignments=newChannel?[]:iptvAssignments($('iptv-new-assignment'));
  if(!newChannel&&(!assignments.length||assignments.some(a=>!a.channel||!a.scope)))
    throw new Error('Choose an existing channel and daypart, or enter a new channel name.');
  const scope=$('iptv-new-scope').value;
  if(newChannel&&!iptvData.scopes.includes(scope))throw new Error('Choose a daypart for the new channel.');
  const result=await iptvPost('',{...iptvSelection(),test_id:iptvTestId,name:$('iptv-name').value,
    new_channel:newChannel,channel_number:newChannel?$('iptv-number').value:'',scope,assignments});
  $('iptv-new-channel').value='';$('iptv-number').value='';iptvUpdateDestination();iptvInvalidateTest();return result;
});
$('iptv-refresh').onclick=async()=>{
  const button=$('iptv-refresh');button.disabled=true;
  try{iptvLoadController?.abort();await iptvLoadPromise?.catch(()=>{});await loadIPTV();$('iptv-message').textContent='IPTV options refreshed.';}
  catch(error){$('iptv-message').textContent=error.message;}
  finally{button.disabled=false;}
};
$('iptv-stop-test').onclick=()=>iptvAction(()=>iptvPost('/preview-stop'));
$('iptv-panel').addEventListener('toggle',()=>{if($('iptv-panel').open&&!iptvData)loadIPTV().catch(e=>$('iptv-message').textContent=e.message);});
// This page has its own lifecycle. Load options even if the browser restored an
// already-open panel before the toggle listener was attached.
iptvUpdateDestination();
loadIPTV().catch(e=>$('iptv-message').textContent=e.message);
setInterval(async()=>{if(!$('iptv-panel').open)return;try{const revision=iptvRevision,p=await api('/api/iptv/preview');if(revision===iptvRevision)iptvPreviewState(p);}catch(e){}},1000);
'''
