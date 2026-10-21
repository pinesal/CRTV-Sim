IPTV_HTML = '''
<style>
#iptv-panel .iptv-source {align-items:end;}
#iptv-panel .iptv-source > label:first-child {display:flex;flex-direction:column;gap:6px;flex:1;min-width:260px;}
#iptv-panel .iptv-source > label:last-child {display:flex;align-items:center;gap:8px;}
#iptv-panel input[type=checkbox] {width:16px;min-width:16px;flex:0 0 16px;margin:0;}
#iptv-panel .iptv-fields {display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;}
#iptv-panel .iptv-fields label {display:flex;flex-direction:column;gap:6px;}
#iptv-panel .iptv-fields input,#iptv-panel .iptv-fields select {width:100%;}
#iptv-panel .iptv-assignment {display:grid;grid-template-columns:minmax(150px,1fr) minmax(180px,1fr) minmax(180px,2fr) auto;gap:10px;}
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
  <button id="iptv-inspect">Inspect URL</button><label><input id="iptv-direct" type="checkbox">Direct stream (save without inspection)</label></div>
  <div class="toolbar iptv-fields"><label>Feed <select id="iptv-selection"></select></label><label>Display name <input id="iptv-name"></label>
  <label>New channel (optional) <input id="iptv-new-channel"></label><label>Number <input id="iptv-number" type="number" min="1" max="999"></label></div>
  <div id="iptv-new-assignment"></div><div class="actions"><button id="iptv-add">Add feed</button><button id="iptv-refresh">Refresh status</button>
  <button id="iptv-stop-test">Close preview</button></div>
  <p id="iptv-message" role="status"></p><p id="iptv-preview-state" role="status"></p>
  <div id="iptv-conflicts"></div><div id="iptv-feeds"></div>
  </div></details>
'''

IPTV_JS = r'''
let iptvData=null, iptvInspection=null;
const iptvPost=(path,body={})=>api('/api/iptv'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
function iptvEditor(host, assignments=[]) {
  host.replaceChildren();
  function row(a={}) {
    const div=document.createElement('div');div.className='toolbar iptv-assignment';
    const channel=document.createElement('select');channel.setAttribute('aria-label','Channel');
    channel.replaceChildren(...iptvData.channels.map(c=>new Option(c.display_name,c.slug)));
    if(a.channel_slug)channel.value=a.channel_slug;
    const scope=document.createElement('select');scope.setAttribute('aria-label','Scope or weekly special');
    function scopes(){scope.replaceChildren(...iptvData.scopes.map(s=>new Option(s,s)),...iptvData.weekly.filter(w=>w.channel===channel.value).map(w=>new Option('Weekly: '+w.name,'weekly:'+w.slug)));}
    scopes();channel.onchange=scopes;scope.value=a.weekly_slug?'weekly:'+a.weekly_slug:a.scope||'all-day';
    const name=document.createElement('input');name.placeholder='Optional Guide label';name.value=a.display_name||'';name.setAttribute('aria-label','Guide label');
    const remove=document.createElement('button');remove.textContent='Remove assignment';remove.onclick=()=>div.remove();
    div.append(channel,scope,name,remove);host.append(div);
  }
  assignments.forEach(row);
  const add=document.createElement('button');add.textContent='Add assignment';add.onclick=()=>row();host.append(add);
}
function iptvAssignments(host){return [...host.querySelectorAll('.iptv-assignment')].map(r=>{const selects=r.querySelectorAll('select'),v=selects[1].value;return {channel:selects[0].value,scope:v.startsWith('weekly:')?'all-day':v,weekly_slug:v.startsWith('weekly:')?v.slice(7):'',display_name:r.querySelector('input').value};});}
async function iptvAction(action){try{const result=await action();$('iptv-message').textContent=result.message||result.error||'Saved.';await loadIPTV();}catch(e){$('iptv-message').textContent=e.message;}}
async function loadIPTV(){
  iptvData=await api('/api/iptv');
  if(!$('iptv-new-assignment').children.length)iptvEditor($('iptv-new-assignment'),[{scope:'all-day'}]);
  const host=$('iptv-feeds');host.replaceChildren();
  for(const f of iptvData.feeds){
    const card=document.createElement('section');card.className='panel';
    const title=document.createElement('h3');title.textContent=f.name;card.append(title);
    const source=document.createElement('p');source.textContent=f.source_url;card.append(source);
    const status=document.createElement('p');status.textContent=`${f.enabled?'Enabled':'Disabled'} · ${f.availability} · ${f.detail||f.refresh_error||'Not yet tested'}`;card.append(status);
    const name=document.createElement('input');name.value=f.name;name.setAttribute('aria-label','Feed display name');card.append(name);
    const assignments=document.createElement('div');iptvEditor(assignments,f.assignments);card.append(assignments);
    const actions=document.createElement('div');actions.className='actions';card.append(actions);
    function button(label,fn){const b=document.createElement('button');b.textContent=label;b.onclick=()=>iptvAction(fn);actions.append(b);}
    button('Save',()=>iptvPost('/'+f.id,{name:name.value,assignments:iptvAssignments(assignments)}));
    button(f.enabled?'Disable':'Enable',()=>iptvPost('/'+f.id,{enabled:!f.enabled}));
    button('Refresh playlist',()=>iptvPost('/'+f.id+'/refresh'));
    button('Test feed',()=>iptvPost('/'+f.id+'/test'));
    button('Remove',async()=>{if(!confirm('Remove this feed and its assignments? Affected unlocked schedules will be rebuilt. No media files are deleted.'))return {};return iptvPost('/'+f.id+'/remove');});
    host.append(card);
  }
  const conflicts=$('iptv-conflicts');conflicts.replaceChildren();
  for(const c of iptvData.conflicts){const p=document.createElement('p');p.className='warning';const facts=JSON.parse(c.explanation_json).iptv;p.textContent=`Conflict: ${c.slug} · ${facts.weekly||c.daypart_slug} · ${c.starts_at} — ${c.ends_at}. ${(facts.local_conflicts||[]).map(v=>v.placement).join('; ')||'Competing feed assignments'}`;conflicts.append(p);}
}
$('iptv-inspect').onclick=async()=>{try{iptvInspection=await iptvPost('/inspect',{url:$('iptv-url').value});$('iptv-selection').replaceChildren(...iptvInspection.feeds.map(f=>{const o=new Option(f.name+(f.ambiguous?' (ambiguous)':''),f.key);o.disabled=!!f.ambiguous;return o;}));const skipped=iptvInspection.skipped_entries?.length||0;$('iptv-message').textContent=`${iptvInspection.feeds.length} feed(s). ${skipped?`${skipped} unsupported or invalid entries skipped. `:''}Select one, add assignments, then save and test.`;}catch(e){$('iptv-message').textContent=e.message+' You can save a known direct stream without inspection.';}};
$('iptv-add').onclick=()=>iptvAction(async()=>{if(!$('iptv-direct').checked&&(!iptvInspection||iptvInspection.url!==$('iptv-url').value.trim()))throw new Error('Inspect this URL first, or select Direct stream.');return iptvPost('',{url:$('iptv-url').value,direct:$('iptv-direct').checked,selection_key:$('iptv-selection').value,name:$('iptv-name').value,new_channel:$('iptv-new-channel').value,channel_number:$('iptv-number').value,scope:iptvAssignments($('iptv-new-assignment'))[0]?.scope||'all-day',assignments:iptvAssignments($('iptv-new-assignment'))});});
$('iptv-refresh').onclick=()=>iptvAction(()=>Promise.resolve({}));
$('iptv-stop-test').onclick=()=>iptvAction(()=>iptvPost('/preview-stop'));
$('iptv-panel').addEventListener('toggle',()=>{if($('iptv-panel').open&&!iptvData)loadIPTV().catch(e=>$('iptv-message').textContent=e.message);});
setInterval(async()=>{if(!$('iptv-panel').open)return;try{const p=await api('/api/iptv/preview');$('iptv-preview-state').textContent=`Preview: ${p.state} · ${p.detail||''}`;}catch(e){}},2000);
'''
