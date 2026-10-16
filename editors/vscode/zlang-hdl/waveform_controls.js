// SPDX-License-Identifier: Apache-2.0
// Browser domain functions, composed before minification by the asset builder.
async function control(message) {
  const command = message.command;
  let result = { command: "ack" };
  try {
    if (command.command === "get_item_list") {
      result = { command: "get_item_list", ids: visibleItems().map((item) => item.id) };
    } else if(command.command==='edit_rows'){
      if(!['cut','copy','paste'].includes(command.action))throw new Error('Unknown waveform row edit action');
      const handled=!waveformTextEditing(document.activeElement)&&!document.activeElement?.closest('#browser');
      if(handled)await editWaveformRows(command.action);
      result={command:'edit_rows',handled};
    } else if (command.command === "get_item_info") {
      result = { command: "get_item_info", results: (command.ids || []).map((id) => {
        const item = findItem(String(id));
        if (!item) throw new Error("Unknown item " + id);
        return { id: item.id, name: item.type !== 'signal' ? item.name : signalFor(item)?.name || item.unresolved?.name || "", type: item.type === "group" ? "Group" : item.type==='divider'?'Divider':"Variable" };
      }) };
    } else if (command.command === "add_items") {
      const ids = addCatalogPaths(command.items, Boolean(command.recursive));
      if (!ids.length) throw new Error("No requested waveform item was added");
      result = { command: "add_items", ids };
    } else if (command.command === "remove_items") {
      for (const id of command.ids || []) removeItem(state.layout, String(id));
      renderAll();
      save();
      scheduleQuery();
    } else if (command.command === "clear") {
      state.layout = [];
      renderAll();
      save();
      scheduleQuery();
    } else if (command.command === "set_cursor") {
      state.cursor = clamp(finiteTimestamp(command.timestamp, "cursor"), state.min, state.max);
      state.statistics = null;
      renderAll();
      scheduleStatistics();
    } else if (command.command === "set_cursors") {
      state.cursor = clamp(finiteTimestamp(command.cursorA, "cursor A"), state.min, state.max);
      state.marker = command.cursorB === null || command.cursorB === void 0 ? null : clamp(finiteTimestamp(command.cursorB, "cursor B"), state.min, state.max);
      state.statistics = null;
      renderAll();
      scheduleStatistics();
      save();
    } else if (command.command === "set_selection") {
      const requested = (command.signals || []).map(String);
      const matches=visibleItems().filter(item=>item.type==='signal'&&(requested.includes(item.id)
        ||requested.includes(item.signalId)||requested.includes(state.byId.get(item.signalId)?.name)));
      state.selectedRowIds=new Set(matches.map(item=>item.id));state.selectedRowId=matches.at(-1)?.id||null;
      syncWaveformSelection();
      state.statistics = null;
      renderAll();
      scheduleStatistics();
    } else if (command.command === "set_bookmarks") {
      state.bookmarks = normalizeBookmarks(command.bookmarks, state.min, state.max);
      renderAll();
      save();
    } else if (command.command === "set_viewport_to") {
      const view = state.viewports[0], center = finiteTimestamp(command.timestamp, "viewport center"), span = view.end - view.start;
      setRange(0, center - span / 2, center + span / 2);
    } else if (command.command === "set_viewport_range") {
      const start=finiteTimestamp(command.start,'viewport start'),end=finiteTimestamp(command.end,'viewport end');
      if(start>=end)throw new Error('Viewport start must precede end');
      setRange(0,start,end);
    } else if (command.command === "zoom_to_fit") {
      fit();
    } else if (command.command === "reload") {
      requestTraceReload();
    } else if (command.command === "load") {
      if (typeof command.source !== "string" || !command.source) throw new Error("load source is required");
      vscode.postMessage({ type: "load", source: command.source });
    } else if (command.command === "focus_item") {
      const item = findItem(String(command.id));
      if (!item) throw new Error("Unknown item " + command.id);
      if (item.type === "signal") {
        selectWaveformRow(item.id);
        const signal = signalFor(item);
        el("filter").value = signal?.name || "";
        buildSourceTree();
        el('displayRows').focus({preventScroll:true});
      }
    } else if (command.command === "set_item_color") {
      const item = findItem(String(command.id));
      if (!item) throw new Error("Unknown item " + command.id);
      item.color = sessionColor(command.color);
      renderAll();
      save();
    } else if (command.command === "export_session") {
      result={command:'export_session',session:exportWaveformSession(),traceChanged:state.traceChanged,
        reloading:state.reloading,layoutPending:state.savedLayoutRevision<state.layoutRevision};
    } else if(command.command==='import_session') {
      result=applyWaveformSession(command.session);
      renderAll();scheduleQuery();save();
    } else if (command.command === "get_measurements") {
      const exact=await scheduleStatistics();
      const selected = state.selectedSignalId && state.byId.get(state.selectedSignalId);
      const record=exact?.signals?.find(item=>item.signal===selected?.id);
      const measurement=record&&state.marker!==null?waveformMeasurement(record,Math.floor(state.cursor),Math.floor(state.marker),state.metadata):null;
      result = { command: "get_measurements", cursorA: state.cursor, cursorB: state.marker, signal: selected?.name || null, measurement };
    } else if(command.command==='set_viewport_sync') {
      state.syncViewports=Boolean(command.enabled);el('syncViewports').checked=state.syncViewports;
      if(state.syncViewports){const view={...state.viewports[state.active]};state.viewports=state.viewports.map(()=>({...view}));}
      renderAll();scheduleQuery();save();
    } else {
      throw new Error('Unsupported waveform control command: '+command.command);
    }
    vscode.postMessage({ type: "controlResult", requestId: message.requestId, result });
  } catch (error) {
    vscode.postMessage({ type: "controlResult", requestId: message.requestId, error: error.message });
  }
}
function finiteTimestamp(value, label) {
  if(typeof value==='string'&&/^\d+(?:\.\d+)?$/.test(value))value=Number(value);
  return sessionTimestamp(value,state.min,state.max,label);
}
module.exports = { control, finiteTimestamp };
