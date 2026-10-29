// SPDX-License-Identifier: Apache-2.0
// Bounded webview requests. The extension is the only transport to Rust.
function queryRequest(type, fields, purpose) {
  if(type!=='clipboard'&&(state.reloading||state.traceChanged))return Promise.resolve(null);
  const key = JSON.stringify(fields);
  const previous = state.querySlots.get(purpose);
  if (previous?.key === key) return previous.promise;
  if (previous?.resolve) {
    clearTimeout(previous.timer);
    previous.resolve(null);
  }
  const requestId = 'query-' + (++state.sequence);
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  const slot = { key, requestId, promise, resolve, timer: null };
  slot.timer = setTimeout(() => {
    if (state.querySlots.get(purpose) !== slot) return;
    state.querySlots.delete(purpose);
    resolve(null);
    el('searchReadout').textContent = 'Waveform query timed out';
  }, 30000);
  state.querySlots.set(purpose, slot);
  vscode.postMessage({type, requestId, purpose, ...fields});
  return promise;
}
function receiveQuery(message) {
  for (const [purpose, slot] of state.querySlots) {
    if (slot.requestId !== message.requestId) continue;
    clearTimeout(slot.timer);
    const resolve = slot.resolve;
    slot.resolve = null;
    if (message.error) {
      state.querySlots.delete(purpose);
      el('searchReadout').textContent = message.error;
      resolve?.(null);
    } else resolve?.(message.result);
    return true;
  }
  return false;
}
function requestedRows() {
  return waveformRowWindow(waveformDisplayRows().rows,
    el('wavePane').scrollTop, Math.max(1,el('wavePane').clientHeight-rulerHeight));
}
async function queryAll() {
  if(state.queryRunning)return;
  state.queryRunning=true;state.queryDirty=false;
  const epoch=state.queryEpoch;
  try{
    const ids = waveformSignalIds(requestedRows().map(row=>row.item));
    if (!ids.length) { state.rendered.clear();state.renderedRanges.clear();state.data.clear();return; }
    const viewports=state.viewports.map(view=>({...view}));
    const pixels = Math.max(1,Math.min(8192,Math.floor(262144/ids.length),
      Math.ceil(1.5*el('wavePane').clientWidth/Math.max(1,viewports.length))));
    for (const [index, viewport] of viewports.entries()) {
      // A bounded margin keeps modest pans covered while Rust fetches the next
      // tile. Keep pixel density and the existing response-size ceiling.
      const margin=(viewport.end-viewport.start)/4;
      const result = await queryRequest('renderRange',{
        viewport:index, signals:ids,start:Math.max(state.min,Math.floor(viewport.start-margin)),
        end:Math.min(state.max,Math.ceil(viewport.end+margin)),pixels,
      },'viewport-'+index);
      if(epoch!==state.queryEpoch)return;
      if (result && state.viewports[index]) receiveRender({viewport:index,result});
    }
    const overviewIds=waveformSignalIds(visibleSignals());
    const result = await queryRequest('renderRange',{
      overview:true,signals:overviewIds,start:Math.floor(state.min),end:Math.ceil(state.max),
      pixels:Math.max(1,Math.min(2048,Math.floor(262144/overviewIds.length),
        el('overview').clientWidth||512)),
    },'overview');
    if(epoch!==state.queryEpoch)return;
    if(result){state.overview=result;renderOverview();}
    void scheduleStatistics();
  }finally{
    state.queryRunning=false;
    if(state.queryDirty)scheduleQuery();
  }
}
function receiveRender(message) {
  const exact=new Map(), rendered=new Map();
  for(const signal of message.result.signals||[]){
    rendered.set(signal.signal,signal);
    if(signal.exact)exact.set(signal.signal,signal.points.map(point=>
      ({event:point.timestamp,bits:point.bits,display:point.display})));
  }
  state.data.set(message.viewport,exact);
  state.rendered.set(message.viewport,rendered);
  state.renderedRanges.set(message.viewport,{start:message.result.start,end:message.result.end});
  renderDisplayValues();renderViewports();
}
async function refreshCursorValues() {
  const signals=waveformSignalIds(requestedRows().map(row=>row.item));
  if(!signals.length)return;
  const a=state.cursor,b=state.marker;
  const valuesA=await queryRequest('valueAt',{signals,timestamp:Math.floor(a)},'value-a');
  if(valuesA&&state.cursor===a)state.cursorValuesA={timestamp:a,values:valuesA.values,display:valuesA.display_values};
  if(b!==null){
    const valuesB=await queryRequest('valueAt',{signals,timestamp:Math.floor(b)},'value-b');
    if(valuesB&&state.marker===b)state.cursorValuesB={timestamp:b,values:valuesB.values,display:valuesB.display_values};
  }
  renderDisplayValues();
}
async function scheduleStatistics() {
  void refreshCursorValues();
  if(state.marker===null||!state.selectedSignalId)return null;
  const a=Math.floor(state.cursor),b=Math.floor(state.marker);
  const selected=state.selectedSignalId;
  const result=await queryRequest('statistics',{
    signals:[selected],start:Math.min(a,b),end:Math.max(a,b),
  },'statistics');
  if(result&&state.selectedSignalId===selected&&Math.floor(state.cursor)===a
    &&Math.floor(state.marker)===b){state.statistics=result;renderInspector();}
  return result;
}
function cursorDisplay(signalId,baseline=false){
  const time=baseline?state.marker:state.cursor;
  if(time!==null&&time<(state.byId.get(signalId)?.history_start||0))return 'not recorded';
  const exact=baseline?state.cursorValuesB:state.cursorValuesA;
  if(exact?.timestamp===time)return exact.display?.[signalId];
  return undefined;
}
function cursorValue(signalId,baseline=false) {
  const time=baseline?state.marker:state.cursor;
  if(time===null)return null;
  const exact=baseline?state.cursorValuesB:state.cursorValuesA;
  if(exact?.timestamp===time&&Object.hasOwn(exact.values,signalId))
    return exact.values[signalId];
  return valueAt(valuesFor(state.active,signalId),time);
}
async function runSearch(reverse, kind=el('searchKind').value,
  value=el('searchValue').value||null, signalIds=null) {
  const ids=signalIds||signalIdsForChangeJump();
  if(!ids.length){el('searchReadout').textContent=state.selectedRowId?'Holder has no current trace data':'No signals displayed';return;}
  const bounds=waveformSearchBounds(state.cursor,state.min,state.max,reverse);
  if(!bounds){el('searchReadout').textContent='no match';return;}
  const query={signals:ids,...bounds,reverse,kind,value,limit:1};
  const result=await queryRequest('search',{query},'search');
  if(result)receiveSearch(result);
}
function receiveSearch(result) {
  const match=result.matches?.[0];
  if(!match){el('searchReadout').textContent='no matching event';return;}
  state.cursor=match.timestamp;state.statistics=null;
  if(state.live){state.followLive=false;el('followLive').classList.remove('enabled');}
  el('searchReadout').textContent=(state.byId.get(match.signal)?.name||match.signal)
    +' @ '+match.timestamp;
  revealTime(state.cursor);void scheduleStatistics();save();
}
function jumpChange(reverse,kind='any_change') {
  const ids=signalIdsForChangeJump();
  el('searchReadout').textContent=(reverse?'previous ':'next ')+kind.replaceAll('_',' ');
  void runSearch(reverse,kind,null,ids);
}
module.exports={queryRequest,receiveQuery,requestedRows,queryAll,receiveRender,
  refreshCursorValues,scheduleStatistics,cursorValue,cursorDisplay,runSearch,receiveSearch,jumpChange};
