// SPDX-License-Identifier: Apache-2.0
// Portable sessions and autosave use one normalization and rebinding path.
function exportWaveformSession() {
  const selected=state.byId.get(state.selectedSignalId);
  return {
    schema:WAVEFORM_SESSION_SCHEMA,catalogSignature:state.metadata.catalog_signature,
    items:serializeSessionLayout(state.layout,state.byId),panelLayout:state.panelLayout,
    viewports:state.viewports,activeViewport:state.active,syncViewports:state.syncViewports,
    cursorA:state.cursor,cursorB:state.marker,bookmarks:state.bookmarks,
    selectedSignal:selected?{name:selected.name,width:selected.width}:null,
    selectedRow:state.selectedRowId,selectedRows:[...state.selectedRowIds],
    format:state.format,search:{kind:el('searchKind').value,value:el('searchValue').value},
    inspectorCollapsed:state.inspectorCollapsed,followLive:state.followLive,
  };
}
function applyWaveformSession(session) {
  const normalized=normalizeWaveformSession(session,state.catalog,state.min,state.max);
  state.layout=normalized.items;state.panelLayout=normalized.panelLayout;
  state.viewports=normalized.viewports;state.active=normalized.activeViewport;
  state.syncViewports=normalized.syncViewports;state.cursor=normalized.cursorA;
  state.marker=normalized.cursorB;state.bookmarks=normalized.bookmarks;
  state.selectedRowId=normalized.selectedRowId;
  state.selectedRowIds=new Set(normalized.selectedRowIds);
  syncWaveformSelection();
  state.format=normalized.format;state.statistics=null;
  state.followLive=normalized.followLive;state.inspectorCollapsed=normalized.inspectorCollapsed;
  el('format').value=findItem(state.selectedRowId)?.format||state.format;
  el('format').disabled=Boolean(state.selectedRowId&&findItem(state.selectedRowId)?.type!=='signal');
  el('syncViewports').checked=state.syncViewports;
  el('searchKind').value=normalized.search.kind;el('searchValue').value=normalized.search.value;
  applyPanelLayout();
  let unresolved=0;walk(state.layout,item=>{if(item.unresolved)unresolved+=1;});
  const result={command:'import_session',unresolved,
    catalogMismatch:Boolean(session.catalogSignature&&session.catalogSignature!==state.metadata.catalog_signature)};
  if(unresolved||result.catalogMismatch)el('searchReadout').textContent=
    'Catalog changed; '+unresolved+' unresolved signal(s). Exact name and width required.';
  return result;
}
module.exports={exportWaveformSession,applyWaveformSession};
