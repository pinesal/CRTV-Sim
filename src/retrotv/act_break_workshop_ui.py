"""Embedded workshop client, served from the Python package in portable builds."""

ACT_BREAK_WORKSHOP_JS = r"""
class ActBreakWorkshopUI {
  static current = null;
  static open(row, item, options) {
    this.current?.close();
    this.current = new ActBreakWorkshopUI(row, item, options);
    return this.current;
  }
  constructor(row, item, options) {
    this.item = item; this.options = options; this.session = null; this.sequence = 0;
    this.closed = false; this.busy = false; this.playing = false; this.readyFrame = false;
    this.duration = Number(item.duration_seconds) * 1000;
    this.targetIndex = (item.targets.find(t => ['midpoint','quiet_pause'].includes(t.selected?.method)) || item.targets[0])?.index;
    const node = (tag, text, props = {}) => {
      const result = document.createElement(tag);
      if (text != null) result.textContent = text;
      Object.assign(result, props); return result;
    };
    this.detailRow = node('tr'); const cell = node('td', null, {colSpan:row.children.length});
    this.panel = node('section', null, {className:'act-workshop'});
    cell.append(this.panel); this.detailRow.append(cell); row.after(this.detailRow);
    this.panel.append(node('h3', `${item.series_title ? item.series_title + ' — ' : ''}${item.display_title || item.original_filename}`));
    this.targetSelect = node('select', null, {ariaLabel:'Act-break target'});
    this.saved = node('p'); this.candidate = node('p');
    const top = node('div', null, {className:'workshop-controls'});
    const button = (text, click) => {const b=node('button', text, {type:'button'});b.onclick=click;return b;};
    top.append(node('label','Target'),this.targetSelect,button('Close workshop',()=>this.close()));
    this.panel.append(top,this.saved,node('p','Commercials begin before this frame; the program resumes with this frame.'));
    this.video = node('video',null,{preload:'metadata',playsInline:true,hidden:true});
    this.image = node('img',null,{alt:'Selected source frame',hidden:true});
    const media=node('div',null,{className:'workshop-media'});media.append(this.video,this.image);
    this.panel.append(media,this.candidate);
    const label=(text,input)=>{const l=node('label',text);l.append(input);this.panel.append(l);};
    this.timeline=node('input',null,{type:'range',min:0,max:this.duration,step:1,value:0,ariaLabel:'Full program timeline'});
    label('Full program',this.timeline);
    this.markers=node('div',null,{className:'workshop-markers'});this.panel.append(this.markers);
    this.zoom=node('input',null,{type:'range',min:0,max:this.duration,step:1,value:0,ariaLabel:'Loaded preview timeline'});
    label('Loaded preview — fine adjustment',this.zoom);
    this.windowRange=node('p',null,{className:'muted'});this.panel.append(this.windowRange);
    const controls=node('div',null,{className:'workshop-controls'});
    this.play=button('Play',()=>this.togglePlay());
    this.previous=button('Previous Frame',()=>this.prepare('step',-1));
    this.next=button('Next Frame',()=>this.prepare('step',1));
    this.around=button('Play around this point',()=>this.playAround());
    controls.append(this.play,button('−5 seconds',()=>this.seek(this.position-5000,false)),
      button('+5 seconds',()=>this.seek(this.position+5000,false)),this.previous,this.next,this.around);
    this.panel.append(controls);
    this.timestamp=node('input',null,{type:'text',placeholder:'HH:MM:SS.mmm',ariaLabel:'Candidate timestamp'});
    this.timestamp.oninput=()=>{
      if(this.playing){this.playing=false;this.video.pause();this.stopAt=null;}
      this.readyFrame=false;this.updateButtons();this.message('Use Go to timestamp to verify the frame before placing a break.');
    };
    const placement=node('div',null,{className:'workshop-controls'});
    this.place=button('Place Act Break',()=>this.save('manual'));
    this.place.className='primary';
    this.reset=button('Reset to automatic',()=>this.save('reset'));
    placement.append(this.timestamp,button('Go to timestamp',()=>{
      const parts=this.timestamp.value.trim().split(':').map(Number);
      const seconds=parts.length===3?parts[0]*3600+parts[1]*60+parts[2]:parts.length===2?parts[0]*60+parts[1]:parts[0];
      if(parts.length>3 || !Number.isFinite(seconds) || seconds<0) return this.message('Enter HH:MM:SS, MM:SS, or seconds.');
      this.seek(seconds*1000,false);
    }),this.place,this.reset);
    this.panel.append(placement);
    this.status=node('p',null,{role:'status'});this.panel.append(this.status);
    this.timeline.oninput=()=>this.seek(Number(this.timeline.value));
    this.zoom.oninput=()=>this.seek(Number(this.zoom.value));
    this.targetSelect.onchange=()=>{this.targetIndex=Number(this.targetSelect.value);this.showSaved();this.seekTarget();};
    this.video.ontimeupdate=()=>{
      if(!this.playing || !this.result)return;
      this.position=this.sourceAt(this.video.currentTime);this.showPosition();
      if(this.stopAt!=null && this.position>=this.stopAt)this.pauseAtCurrent();
    };
    this.video.onended=()=>{if(this.playing)this.pauseAtCurrent();};
    this.video.onerror=()=>{this.playing=false;this.message('Preview playback failed. Reload this workshop or use the existing typed timestamp controls.');this.updateButtons();};
    this.renderTargets();this.seekTarget();
  }
  get target(){return this.item.targets.find(t=>t.index===this.targetIndex);}
  renderTargets(){
    this.targetSelect.replaceChildren(...this.item.targets.map(t=>{
      const o=document.createElement('option');o.value=t.index;
      o.textContent=`Break ${t.index} — ${t.selected?.method || 'no selected break'}`;return o;
    }));
    this.targetSelect.value=this.targetIndex;this.showSaved();
    this.markers.replaceChildren();
    for(const target of this.item.targets){
      if(!target.selected)continue;
      const marker=document.createElement('span');marker.textContent=`▼ ${target.index}`;
      marker.style.left=`${target.selected.offset_ms/this.duration*100}%`;
      marker.title=`Break ${target.index}: ${this.options.format(target.selected.offset_ms)} — ${target.selected.method}`;
      this.markers.append(marker);
    }
  }
  showSaved(){
    const selected=this.target?.selected;
    const signals=selected?.signals || {};const evidence=[];
    if(signals.black_duration)evidence.push(`black ${Number(signals.black_duration).toFixed(2)}s`);
    if(signals.scene_change)evidence.push('scene change');
    if(signals.silence)evidence.push('silence');
    if(signals.silence_duration_ms)evidence.push(`quiet ${(signals.silence_duration_ms/1000).toFixed(2)}s`);
    if(signals.chapter_supported)evidence.push(`chapter ${this.options.format(signals.chapter_marker_ms)}`);
    if(signals.reason)evidence.push(signals.reason);
    this.saved.textContent=selected
      ? `Saved Break ${this.targetIndex}: ${this.options.format(selected.offset_ms)} — ${selected.method}${evidence.length ? '; '+evidence.join('; ') : ''}`
      : `Break ${this.targetIndex}: no saved breakpoint; target ${this.options.format(this.target?.target_ms)}`;
    this.reset.hidden=!this.target?.manual;
  }
  seekTarget(){this.seek(Math.max(0,(this.target?.selected?.offset_ms??this.target?.target_ms??0)-3000),false);}
  showPosition(){
    this.timeline.value=this.position;this.zoom.value=this.position;
    this.timestamp.value=this.options.format(this.position);
    this.candidate.textContent=`Candidate: ${this.options.format(this.position)} — ${this.target?.selected?.offset_ms===Math.round(this.position)?'saved position':'not saved'}`;
  }
  message(text){this.status.textContent=text;}
  updateButtons(){
    this.place.disabled=this.busy || this.playing || !this.readyFrame;
    this.previous.disabled=this.next.disabled=this.place.disabled;
    this.play.disabled=this.around.disabled=this.busy || !this.result;
    this.reset.disabled=this.busy;
    this.targetSelect.disabled=this.saving===true;
    this.play.textContent=this.playing?'Pause':'Play';
  }
  seek(offset,debounce=true){
    if(this.closed || this.saving)return;
    this.playing=false;this.video.pause();this.stopAt=null;
    this.position=Math.max(0,Math.min(this.duration-1,offset));
    this.readyFrame=false;this.busy=true;this.sequence++;clearTimeout(this.debounce);
    this.showPosition();this.updateButtons();this.message('Loading selected frame…');
    // Show an already loaded browser frame immediately while the exact source
    // selection is verified. Never make scrubbing wait for another video encode.
    if(this.playbackResult && this.position>=this.playbackResult.start_ms && this.position<=this.playbackResult.end_ms){
      this.video.currentTime=this.previewAt(this.position);this.video.hidden=false;this.image.hidden=true;
    }
    const sequence=this.sequence;
    this.debounce=setTimeout(()=>this.prepare('preview',0,sequence),debounce?200:0);
  }
  async ensureSession(){
    if(!this.session){
      this.starting ||= this.options.api(`/api/assets/${this.item.id}/act-break-workshop`,{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'open'})
      }).then(data=>{
        this.session=data.session_id;
        this.heartbeat=setInterval(async()=>{
          if(this.closed)return;
          try{
            const state=await this.options.api(`/api/assets/${this.item.id}/act-break-workshop?session_id=${this.session}`);
            if(state.state==='error' && !this.busy){this.readyFrame=false;this.updateButtons();this.message(state.error);}
          }catch(error){if(!this.closed && !this.busy){this.readyFrame=false;this.updateButtons();this.message(`Preview unavailable: ${error.message}`);}}
        },30000);
        return data.session_id;
      });
      try{await this.starting;}finally{this.starting=null;}
    }
    if(this.closed){this.closeRemote();return false;}return true;
  }
  async prepare(action,direction=0,sequence=null){
    if(this.closed || this.saving)return;
    if(sequence==null){
      this.playing=false;this.video.pause();this.sequence++;sequence=this.sequence;
      this.readyFrame=false;this.busy=true;this.updateButtons();
      this.message(action==='playback'?'Preparing playback clip… Frame browsing does not need this clip.':'Loading selected frame…');
    }
    try{
      if(!await this.ensureSession() || sequence!==this.sequence)return;
      let data=await this.options.api(`/api/assets/${this.item.id}/act-break-workshop`,{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({action,direction,session_id:this.session,offset_ms:this.position})
      });
      const generation=data.generation;
      while(!this.closed && sequence===this.sequence && ['queued','running'].includes(data.state)){
        await new Promise(resolve=>setTimeout(resolve,100));
        if(this.closed || sequence!==this.sequence)return;
        data=await this.options.api(`/api/assets/${this.item.id}/act-break-workshop?session_id=${this.session}`);
        if(data.generation!==generation)return;
      }
      if(this.closed || sequence!==this.sequence)return;
      if(data.state!=='ready')throw Error(data.error || 'Preview was not completed');
      this.result=data.result;this.duration=this.result.duration_ms;
      this.position=this.result.offset_ms;this.timeline.max=this.duration;
      if(this.result.clip_url){
        this.playbackResult=this.result;
        this.zoom.min=this.result.start_ms;this.zoom.max=this.result.end_ms;this.zoom.disabled=false;
        this.windowRange.textContent=`Loaded window: ${this.options.format(this.result.start_ms)}–${this.options.format(this.result.end_ms)}`;
        if(this.video.getAttribute('src')!==this.result.clip_url){
          this.video.src=this.result.clip_url;this.video.load();
        }
      }else if(!this.playbackResult){
        this.zoom.disabled=true;
        this.windowRange.textContent='Playback preview loads when you press Play. Frame browsing is available now.';
      }
      this.video.hidden=true;this.image.hidden=false;
      this.image.onload=()=>{
        if(this.closed || sequence!==this.sequence || this.playing)return;
        this.readyFrame=true;this.busy=false;this.updateButtons();
        this.message('Frame ready. Place Act Break saves this candidate; browsing does not change the break.');
      };
      this.image.onerror=()=>{
        if(this.closed || sequence!==this.sequence)return;
        this.busy=false;this.readyFrame=false;this.updateButtons();this.message('Selected frame could not be loaded. Reload the workshop.');
      };
      this.image.src=this.result.frame_url;
      if(this.image.complete && this.image.naturalWidth>0)this.image.onload();
      this.showPosition();this.renderTargets();
      return true;
    }catch(error){
      if(this.closed || sequence!==this.sequence)return;
      this.busy=false;this.readyFrame=false;this.updateButtons();
      this.message(`Preview unavailable: ${error.message}. The existing typed timestamp controls remain available above.`);
    }
  }
  sourceAt(previewSeconds){
    const map=this.playbackResult.mapping;let selected=map[0];
    for(const entry of map){if(entry[0]>previewSeconds+.000001)break;selected=entry;}
    return selected[1]*1000;
  }
  previewAt(sourceMs){
    return this.playbackResult.mapping.reduce((best,e)=>Math.abs(e[1]*1000-sourceMs)<Math.abs(best[1]*1000-sourceMs)?e:best)[0];
  }
  async togglePlay(){
    if(this.playing){this.pauseAtCurrent();return;}
    if(!this.result || this.busy)return;
    this.stopAt=null;await this.startPlayback(this.position);
  }
  async startPlayback(position){
    try{
      if(!this.playbackResult || position<this.playbackResult.start_ms
          || Math.min(this.duration,this.stopAt??position+5000)>this.playbackResult.end_ms){
        if(!await this.prepare('playback') || this.closed)return;
      }
      this.busy=false;
      this.video.currentTime=this.previewAt(position);this.image.hidden=true;this.video.hidden=false;
      this.playing=true;this.readyFrame=false;this.updateButtons();await this.video.play();
    }catch(error){this.playing=false;this.updateButtons();this.message(`Playback failed: ${error.message}`);}
  }
  pauseAtCurrent(){
    const position=this.sourceAt(this.video.currentTime);
    this.playing=false;this.video.pause();this.seek(position,false);
  }
  async playAround(){
    if(!this.result || this.busy)return;
    this.stopAt=Math.min(this.duration,this.position+5000);
    await this.startPlayback(Math.max(0,this.position-5000));
  }
  async save(action){
    if(this.closed || this.busy || (action==='manual' && (!this.readyFrame || this.playing)))return;
    this.saving=true;this.busy=true;this.updateButtons();this.message('Saving act break…');
    try{
      const payload={action,target_index:this.targetIndex,workshop_session:this.session};
      if(action==='manual')Object.assign(payload,{offset_ms:this.result.offset_ms,
        workshop_session:this.session,frame_token:this.result.frame_token,working_identity:this.result.working_identity});
      await this.options.api(`/api/assets/${this.item.id}/breakpoint`,{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)
      });
      if(this.closed)return;
      const data=await this.options.api(`/api/breakpoints?asset_id=${this.item.id}`);
      if(this.closed)return;
      if(data.items[0]){Object.assign(this.item,data.items[0]);this.options.onSaved(this.item);this.renderTargets();}
      this.showPosition();this.message(action==='reset'?'Automatic choice restored.':'Manual act break saved. This entry stays visible until the list is refreshed.');
    }catch(error){if(!this.closed){this.readyFrame=false;this.message(`Could not complete save: ${error.message}`);}}
    finally{this.saving=false;this.busy=false;if(!this.closed)this.updateButtons();}
  }
  closeRemote(){
    if(!this.session || this.remoteClosed)return;this.remoteClosed=true;
    this.options.api(`/api/assets/${this.item.id}/act-break-workshop`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'close',session_id:this.session})
    }).catch(()=>{});
  }
  close(){
    if(this.closed)return;this.closed=true;this.sequence++;clearTimeout(this.debounce);clearInterval(this.heartbeat);
    this.playing=false;this.video.pause();this.video.removeAttribute('src');this.video.load();
    this.image.removeAttribute('src');this.detailRow.remove();this.closeRemote();
    if(ActBreakWorkshopUI.current===this)ActBreakWorkshopUI.current=null;
  }
}
window.ActBreakWorkshopUI=ActBreakWorkshopUI;
"""
