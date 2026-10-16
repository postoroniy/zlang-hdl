// SPDX-License-Identifier: Apache-2.0
// Live status changes metadata and query revisions, never simulator state.
function receiveLiveStatus(message) {
  const first=!state.live;
  const changedSession=state.liveSession&&state.liveSession!==message.sessionId;
  const reset=first||message.reset||state.liveEpoch!==message.epoch;
  const layout=first||changedSession?message.layout:exportWaveformSession();
  const previousCatalog=state.catalog;
  state.live=true;state.liveEpoch=message.epoch;state.liveMarker=message.event;
  state.liveSession=message.sessionId;
  state.reloading=false;state.traceChanged=false;updateTraceStatus();
  state.metadata={time_domain:'zlang_event_ticks',ticks_per_event:2,catalog_signature:'live'};
  const previousIds=new Set(state.catalog.map(signal=>signal.id));
  state.catalog=(message.signals||[]).map(signal=>({
    id:signal.name,name:signal.name,width:signal.width,scope:scope(signal.name),
    kind:signal.kind||'logic',type_hint:signal.type_hint,layout:signal.layout,
    enum_values:signal.layout?.enum_values||[],groups:signal.groups||[],
    direction:signal.direction,domain:signal.domain,history_start:signal.history_start||0,
  }));
  state.byId=new Map(state.catalog.map(signal=>[signal.id,signal]));
  state.min=0;state.max=Math.max(1,message.endTick??message.event);
  cancelWaveformQueries();
  if(reset){
    state.holders.clear();state.renderedRanges.clear();
    state.data.clear();state.rendered.clear();state.overview=null;
    state.statistics=null;state.cursorValuesA=null;state.cursorValuesB=null;
  }
  if(first||changedSession){
    state.layout=[];state.selectedRowId=null;state.selectedRowIds.clear();
    state.selectedSignalId=null;state.selectedSignalIds.clear();
    state.cursor=0;state.marker=null;state.bookmarks=[];
    state.viewports=[{start:0,end:state.max}];state.active=0;
    if(layout)restore(layout);else appendTypedLiveRows(state.catalog);
  }else{
    const changedCatalog=previousCatalog.length!==state.catalog.length
      ||previousCatalog.some(signal=>state.byId.get(signal.id)?.width!==signal.width);
    if(reset||changedCatalog)restore(layout);
    if(changedCatalog){
      const existing=new Set();walk(state.layout,item=>{
        if(item.type==='signal')existing.add(item.signalId||item.unresolved?.name);
      });
      appendTypedLiveRows(state.catalog.filter(signal=>!previousIds.has(signal.id)&&!existing.has(signal.id)));
    }
  }
  if(state.followLive){
    state.cursor=message.event;
    const viewport=state.viewports[state.active],span=viewport.end-viewport.start;
    if(state.max>viewport.end)state.viewports[state.active]={start:Math.max(0,state.max-span),end:state.max};
  }
  el('followLive').hidden=false;el('followLive').classList.toggle('enabled',state.followLive);
  el('summary').textContent=state.catalog.length+' signals · event '+message.event/2;
  renderAll();scheduleQuery();
}
function appendTypedLiveRows(signals){
  const groups=new Map();
  walk(state.layout,item=>{if(item.type==='group'&&item.name)groups.set(item.name,item);});
  const usage=waveformLayoutUsage(state.layout);
  for(const signal of signals){
    if(usage.signals>=MAX_SESSION_ITEMS){
      el('searchReadout').textContent='Live layout reached 4096 signal rows; remove rows to add more';break;
    }
    const names=(signal.groups||[]).slice(0,7);
    const missing=[...new Set(names.filter(name=>!groups.has(name)))];
    if(usage.rows+missing.length+1>MAX_LAYOUT_ITEMS){
      el('searchReadout').textContent='Live layout reached 8192 rows; remove groups or dividers';break;
    }
    let children=state.layout;
    // Session files intentionally cap nesting. Deeper typed paths remain
    // exact signal identities, without creating unsaveable presentation trees.
    for(const name of names){
      let group=groups.get(name);
      if(!group){
        group={id:uid('group'),type:'group',name,collapsed:false,children:[]};children.push(group);groups.set(name,group);
        usage.rows++;
      }
      children=group.children;
    }
    children.push({id:uid('signal'),type:'signal',signalId:signal.id,format:signal.layout?'typed':undefined});
    usage.signals++;usage.rows++;
  }
}
module.exports={receiveLiveStatus,appendTypedLiveRows};
