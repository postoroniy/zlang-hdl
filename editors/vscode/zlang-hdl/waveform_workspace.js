// SPDX-License-Identifier: Apache-2.0
// Browser domain functions, composed before minification by the asset builder.
function walk(items, visit) {
  for (const item of items) {
    visit(item);
    if (item.type === "group") walk(item.children, visit);
  }
}
function findItem(id) {
  let found;
  walk(state.layout, (item) => {
    if (item.id === id) found = item;
  });
  return found;
}
function visibleItems(items = state.layout, result = []) {
  for (const item of items) {
    result.push(item);
    if (item.type === 'group' && !item.collapsed) visibleItems(item.children, result);
  }
  return result;
}
function visibleSignals() {
  return visibleItems().filter((item) => item.type === "signal" && item.signalId);
}
function removeItem(items, id) {
  for (let i = 0; i < items.length; i++) {
    if (items[i].id === id) return items.splice(i, 1)[0];
    if (items[i].type === "group") {
      const found = removeItem(items[i].children, id);
      if (found) return found;
    }
  }
  return null;
}
function parentArray(items, id) {
  for (const item of items) {
    if (item.id === id) return items;
    if (item.type === "group") {
      const found = parentArray(item.children, id);
      if (found) return found;
    }
  }
  return null;
}
function moveWaveformRows(layout,ids,targetId) {
  const selected=new Set(ids),moving=[];let target=null,invalid=false;
  walk(layout,item=>{if(selected.has(item.id))moving.push(item);if(item.id===targetId)target=item;});
  if(selected.has(targetId))return false;
  for(const item of moving)if(item.type==='group')walk(item.children,child=>{
    if(child.id===targetId||selected.has(child.id))invalid=true;
  });
  if(invalid||!moving.length)return false;
  const depths=new Map();
  function measure(items,depth){for(const item of items){depths.set(item.id,depth);if(item.type==='group')measure(item.children,depth+1);}}
  function height(item){return item.type==='group'?1+Math.max(0,...item.children.map(height)):0;}
  measure(layout,0);
  const depth=target?(depths.get(target.id)+(target.type==='group'?1:0)):0;
  if(moving.some(item=>depth+height(item)>8))return false;
  for(const item of moving)removeItem(layout,item.id);
  if(target?.type==='group')target.children.push(...moving);
  else if(target){const owner=parentArray(layout,target.id);owner.splice(owner.indexOf(target),0,...moving);}
  else layout.push(...moving);
  return true;
}
function syncWaveformSelection() {
  const rows=new Map(visibleItems().map(item=>[item.id,item]));
  for(const id of state.selectedRowIds)if(!rows.has(id))state.selectedRowIds.delete(id);
  if(!state.selectedRowIds.has(state.selectedRowId))state.selectedRowId=[...state.selectedRowIds].at(-1)||null;
  state.selectedSignalId=rows.get(state.selectedRowId)?.signalId||null;
  state.selectedSignalIds=new Set([...state.selectedRowIds].map(id=>rows.get(id)?.signalId).filter(Boolean));
  const active=rows.get(state.selectedRowId);
  el('displayMode').value=active?.display||state.display;
  el('displayMode').disabled=Boolean(active&&active.type!=='signal');
  el('format').value=active?.format||state.format;el('format').disabled=Boolean(active&&active.type!=='signal');
}
function waveformDisplayRows() {
  return waveformRows(state.layout,item=>waveformDefaultRowHeight(item,signalFor(item)||item.holder?.signal,state.display));
}
function selectWaveformRow(rowId, render=true, toggle=false) {
  const item=rowId&&findItem(rowId);
  if(!item){state.selectedRowIds.clear();state.selectedRowId=null;}
  else {
    if(!toggle)state.selectedRowIds.clear();
    if(toggle&&state.selectedRowIds.has(rowId))state.selectedRowIds.delete(rowId);
    else {state.selectedRowIds.add(rowId);state.selectedRowId=rowId;}
  }
  syncWaveformSelection();
  state.statistics = null;
  if (render) {
    renderAll();
    scheduleStatistics();
  }
}
function waveformItemAtY(y) {
  const position = y - rulerHeight + el('wavePane').scrollTop;
  return waveformDisplayRows().rows.find(row =>
    row.top <= position && position < row.top + row.height)?.item;
}
function selectRowAtY(y) {
  const item=waveformItemAtY(y);if(item)selectWaveformRow(item.id,false);
}
function signalIdsForChangeJump() {
  if(findItem(state.selectedRowId)?.unresolved)return [];
  const visible = waveformSignalIds(visibleSignals());
  return state.selectedSignalId && visible.includes(state.selectedSignalId) ? [state.selectedSignalId] : visible;
}
function addSignal(signalId, target) {
  const signal = state.byId.get(signalId);
  if (signal) addSignals([signal], target);
}
function canAddWaveformRows(count, usage=waveformLayoutUsage(state.layout)) {
  if(usage.rows+count<=MAX_LAYOUT_ITEMS)return true;
  el('searchReadout').textContent='Session exceeds 8192 rows; remove groups or dividers';
  return false;
}
function addSignals(signals, target) {
  let added = 0;
  let lastRow=null;
  const usage=waveformLayoutUsage(state.layout);
  const incoming=[...new Map(signals.map(signal=>[signal.id,signal])).values()];
  if(!canAddWaveformRows(incoming.length,usage))return 0;
  if(usage.signals+incoming.length>MAX_SESSION_ITEMS){
    el('searchReadout').textContent='Selection exceeds 4096 signals; select a smaller scope';return 0;
  }
  for (const signal of incoming) {
    const item = { id: uid("signal"), type: "signal", signalId: signal.id };
    lastRow=item.id;
    if (target?.type === "group") target.children.push(item);
    else state.layout.push(item);
    added++;
  }
  if (added) {
    state.selectedRowId=lastRow;state.selectedRowIds=new Set([lastRow]);
    renderAll();
    save();
    scheduleQuery();
  }
  return added;
}
function restore(layout) {
  if (!layout || typeof layout !== 'object') return;
  try {
    if(layout.schema===WAVEFORM_SESSION_SCHEMA)applyWaveformSession(layout);
    else {
      const session={...exportWaveformSession(),...layout,
        items:serializeSessionLayout(layout.items||[],state.byId),
        cursorA:layout.cursor??state.min,cursorB:layout.marker??null};
      applyWaveformSession(session);
    }
  } catch(error) {el('searchReadout').textContent='Cannot restore session: '+error.message;}
}
function addCatalogPaths(paths, recursive) {
  const selected = [];
  for (const rawPath of paths || []) {
    const path = String(rawPath), exact = state.catalog.find((item) => item.name === path), matches = exact ? [exact] : state.catalog.filter((item) => item.name.startsWith(path + ".") && (recursive || scope(item.name) === path));
    selected.push(...matches);
  }
  const before=new Set();walk(state.layout,item=>before.add(item.id));
  if(addSignals(selected)===0)return [];
  const ids=[];walk(state.layout,item=>{if(!before.has(item.id))ids.push(item.id);});return ids;
}
function save() {
  clearTimeout(persistTimer);
  state.layoutRevision++;
  persistTimer = setTimeout(persistLayout,150);
}
function persistLayout(){
  clearTimeout(persistTimer);persistTimer=null;
  vscode.postMessage({type:'persist',layout:exportWaveformSession(),revision:state.layoutRevision,
    sessionId:state.live?state.liveSession:undefined});
}
function scheduleQuery() {
  state.queryDirty=true;
  // Throttle, not debounce: continuous pointer movement must not starve data.
  // One active batch consumes the latest viewport before starting another.
  if(queryTimer!==null||state.queryRunning)return;
  queryTimer=setTimeout(()=>{queryTimer=null;void queryAll();},32);
}
function revealTime(time) {
  const view = state.viewports[state.active];
  if (time < view.start || time > view.end) {
    const span = view.end - view.start;
    setRange(state.active, time - span / 2, time + span / 2);
    renderDisplayValues();renderInspector();
  } else renderAll();
}
function renderAll() {
  syncWaveformHolders();
  syncWaveformSelection();
  buildSourceTree();
  renderDisplay();
  renderTimeView();
  renderInspector();
  const selected = state.selectedSignalId && state.byId.get(state.selectedSignalId);
  const holder=findItem(state.selectedRowId)?.holder;
  el("selectionReadout").textContent = holder?'holder: '+holder.signal.name+' (previous trace)':selected ? "signal: " + selected.name + (state.selectedRowIds.size > 1 ? " +" + (state.selectedRowIds.size - 1) + ' rows' : "") : "signal: all displayed";
}
// Time navigation changes no layout, selection, cursor value or inspector data.
function renderTimeView(){
  renderViewports();renderOverview();
  const view=state.viewports[state.active];
  el('range').textContent='range '+Math.round(view.start)+' \u2026 '+Math.round(view.end);
  el('cursorReadout').textContent='cursor A '+Math.round(state.cursor);
  el('markerReadout').textContent=state.marker===null?'marker B \u2014'
    :'marker B '+Math.round(state.marker)+' \u0394 '+Math.round(Math.abs(state.marker-state.cursor));
}
function updateTraceStatus(){
  const button=el('reload');button.hidden=false;
  button.disabled=state.reloading;
  button.textContent=state.live?(state.reloading?'Refreshing…':'Refresh'):state.reloading?'Reloading…':state.traceChanged?'Reload •':'Reload';
  button.title=state.live?'Refresh recorded history without advancing or restarting simulation'
    :'Reload the saved trace, preserving signal rows and workspace settings';
  button.classList.toggle('enabled',state.traceChanged);
  el('followLive').hidden=!state.live;
  el('addTraceSignals').hidden=!state.live;el('exportTrace').hidden=!state.live;
  el('switchDebugSession').hidden=!state.live;
  el('traceStatus').textContent=state.live?'':state.reloading?'Reading trace…':state.traceChanged?'File changed — reload to update':'';
}
function cancelWaveformQueries(){
  clearTimeout(queryTimer);queryTimer=null;state.queryDirty=false;state.queryEpoch++;
  for(const [purpose,slot] of state.querySlots){
    if(purpose==='clipboard')continue;
    clearTimeout(slot.timer);slot.resolve?.(null);state.querySlots.delete(purpose);
  }
}
function beginTraceReload(){
  state.reloading=true;cancelWaveformQueries();updateTraceStatus();
}
function requestTraceReload(){
  if(state.reloading)return;
  beginTraceReload();vscode.postMessage({type:'reload'});
}
function receive(message) {
  if(message.requestId && receiveQuery(message))return;
  if (message.type === "catalog") {
    const metadata=message.catalog?.metadata;
    if(metadata?.schema!=='zlang-waveform-query-v3'){
      state.reloading=false;updateTraceStatus();
      el('searchReadout').textContent='Unsupported waveform query protocol; install matching editor binaries';return;
    }
    if(!metadata||![metadata.start,metadata.end].every(value=>Number.isSafeInteger(value)&&value>=0)){
      state.reloading=false;updateTraceStatus();
      el('searchReadout').textContent='Trace timestamp exceeds the exact integer range supported by this viewer';return;
    }
    const layout=message.preserveLayout?exportWaveformSession():message.layout;
    if(message.preserveLayout)rememberMissingWaveforms(message.catalog.signals);
    else state.holders.clear();
    cancelWaveformQueries();state.data.clear();state.rendered.clear();state.renderedRanges.clear();state.overview=null;
    state.statistics=null;state.cursorValuesA=null;state.cursorValuesB=null;state.live=false;
    state.selectedRowId=null;state.selectedRowIds.clear();syncWaveformSelection();state.marker=null;state.bookmarks=[];
    state.liveMarker=null;
    state.layout=[];state.active=0;state.viewports=[{start:metadata.start,end:Math.max(metadata.start+1,metadata.end)}];
    state.catalog = message.catalog.signals;
    state.metadata = message.catalog.metadata || state.metadata;
    state.byId = new Map(state.catalog.map((signal) => [signal.id, signal]));
    state.min = message.catalog.metadata.start;
    state.max = Math.max(state.min + 1, message.catalog.metadata.end);
    state.cursor = state.min;
    restore(layout);
    if (!layout&&!state.layout.length) for (const signal of state.catalog.slice(0, 8)) state.layout.push({ id: uid("signal"), type: "signal", signalId: signal.id });
    if(!layout)state.viewports = state.viewports.map(() => ({ start: state.min, end: state.max }));
    state.reloading=false;state.traceChanged=Boolean(message.fileChanged);updateTraceStatus();
    el("summary").textContent = message.catalog.metadata.format.toUpperCase() + " \xB7 " + state.catalog.length + " signals \xB7 " + (state.metadata.time_domain === "zlang_event_ticks" ? "event domain" : state.metadata.timescale || "trace ticks");
    renderAll();
    scheduleQuery();
  } else if(message.type==='fileChanged'){
    state.traceChanged=true;cancelWaveformQueries();updateTraceStatus();
  } else if(message.type==='reloadStarted')beginTraceReload();
  else if(message.type==='reloadFailed'){
    state.reloading=false;state.traceChanged=!state.live;updateTraceStatus();
    el('traceStatus').textContent=message.error+(state.live?'':' — retry after simulation finishes');
  } else if (message.type === "queryError") {
    if(!message.requestId){state.reloading=false;updateTraceStatus();}
    el("summary").textContent = message.error;
  } else if (message.type === "control") control(message);
  else if(message.type==='layoutSaved'){
    if((!state.live&&message.sessionId===undefined)||(state.live&&message.sessionId===state.liveSession))
      state.savedLayoutRevision=Math.max(state.savedLayoutRevision,message.revision||0);
  }
  else if(message.type==='liveStatus')receiveLiveStatus(message);
}

module.exports = {waveformDisplayRows, walk, findItem, visibleItems, visibleSignals, removeItem, parentArray, moveWaveformRows, syncWaveformSelection, selectWaveformRow, waveformItemAtY, selectRowAtY, signalIdsForChangeJump, addSignal, canAddWaveformRows, addSignals, restore, addCatalogPaths, save, persistLayout, scheduleQuery, revealTime, renderAll, renderTimeView, updateTraceStatus, cancelWaveformQueries, beginTraceReload, requestTraceReload, receive };
