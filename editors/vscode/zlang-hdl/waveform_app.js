// SPDX-License-Identifier: Apache-2.0
// One webview owns this state and its event bindings.
"use strict";
const vscode = acquireVsCodeApi(), el = (id) => document.getElementById(id), colors = { bg: "#11161c", alt: "#151c23", selection: "#20394d", grid: "#29333d", wave: "#77d84b", x: "#ff5a62", u: "#e9b949", z: "#b78cff", cursor: "#66b8ff", marker: "#ff9d45", muted: "#81909d" };
const state = { catalog: [], metadata: { time_domain: "physical", ticks_per_event: null, catalog_signature: "" }, byId: /* @__PURE__ */ new Map(), layout: [], panelLayout: { browser: 240, signals: 400, inspector: 270 }, viewports: [{ start: 0, end: 1 }], syncViewports: false, data: /* @__PURE__ */ new Map(), rendered: /* @__PURE__ */ new Map(), overview: null, active: 0, min: 0, max: 1, cursor: 0, marker: null, bookmarks: [], selectedSignalId: null, selectedSignalIds: /* @__PURE__ */ new Set(), format: "hex", sequence: 0, drag: null, middlePan: null, overviewDrag: null, splitterDrag: null, statistics: null, live: false, followLive: true };
state.querySlots=new Map();
colors.inactive=colors.muted;
state.queryRunning=false;state.queryDirty=false;state.queryEpoch=0;
state.selectedRowId=null;state.selectedRowIds=new Set();
state.clipboardBusy=false;state.clipboardHolderKeys=new Set();
state.traceChanged=false;state.reloading=false;
state.holders=new WaveformHolderCache();state.renderedRanges=new Map();
state.cursorValuesA=null;state.cursorValuesB=null;
state.browserSelection=new Set();state.openScopes=new Set();
for(const [id,label] of [['saveSession','Save Session'],['loadSession','Load Session'],
  ['addTraceSignals','Add signals'],['exportTrace','Export Trace']]){
  const button=document.createElement('button');button.id=id;button.textContent=label;
  button.hidden=['addTraceSignals','exportTrace'].includes(id);
  button.addEventListener('click',()=>vscode.postMessage({type:id}));
  el('toolbar').insertBefore(button,el('summary'));
}
state.liveMarker=null;state.liveEpoch=null;
state.layoutRevision=0;state.savedLayoutRevision=0;
state.inspectorCollapsed=window.innerWidth<1100;state.snapCursor=true;
const rulerHeight = 32, pad = 10;
let dragged = null, persistTimer = null, queryTimer = null;
function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v));
}
function uid(prefix) {
  return prefix + "-" + Date.now().toString(36) + "-" + ++state.sequence;
}
function leaf(name) {
  return name.split(".").pop();
}
function scope(name) {
  const p = name.split(".");
  p.pop();
  return p.join(".") || "<root>";
}
function kind(bits) {
  const s = String(bits).toLowerCase();
  if (/^[-?]+$/.test(s)) return "inactive"; // Presentation-only, never U/X/Z.
  return s.includes("x") ? "x" : s.includes("u") ? "u" : s.includes("z") ? "z" : "known";
}
el("filter").addEventListener("input", buildSourceTree);
const typedFormatOption=document.createElement('option');
typedFormatOption.value='typed';typedFormatOption.textContent='Type';
el('format').append(typedFormatOption);
el("format").addEventListener("change", (event) => {
  if (state.selectedSignalId) {
    walk(state.layout, (item) => {
      if (item.type === "signal" && state.selectedRowIds.has(item.id)) item.format = event.target.value;
    });
  } else state.format = event.target.value;
  renderAll();
  save();
});
el("fit").addEventListener("click", fit);
el('reload').addEventListener('click',requestTraceReload);
el('followLive').addEventListener('click',()=>{
  state.followLive=!state.followLive;el('followLive').classList.toggle('enabled',state.followLive);
  if(state.followLive&&state.liveMarker!==null){state.cursor=state.liveMarker;revealTime(state.cursor);}
  save();
});
el('toggleInspector').addEventListener('click',()=>{state.inspectorCollapsed=!state.inspectorCollapsed;applyPanelLayout();renderAll();scheduleQuery();save();});
el('snapCursor').addEventListener('change',event=>{state.snapCursor=event.target.checked;});
el("zoomIn").addEventListener("click", () => zoom(0.65));
el("zoomOut").addEventListener("click", () => zoom(1.55));
el("zoomAB").addEventListener("click", zoomAB);
el("prev").addEventListener("click", () => jumpChange(true));
el("next").addEventListener("click", () => jumpChange(false));
el("searchPrev").addEventListener("click", () => runSearch(true));
el("searchNext").addEventListener("click", () => runSearch(false));
el("searchValue").addEventListener("keydown", (event) => {
  if (event.key === "Enter") runSearch(event.shiftKey);
});
el("newGroup").addEventListener("click", async () => {
  if(!canAddWaveformRows(1))return;
  const name = await promptText("Group name", "Group");
  if (name) {
    state.layout.push({ id: uid("group"), type: "group", name:name.slice(0,128), collapsed: false, children: [] });
    renderAll();
    save();
  }
});
el("newDivider").addEventListener("click", async () => {
  if(!canAddWaveformRows(1))return;
  const name = await promptText("Divider label", "");
  if (name !== null) {
    state.layout.push({ id: uid("divider"), type: "divider", name: String(name).slice(0, 128) });
    renderAll();
    save();
  }
});
el("addBookmark").addEventListener("click", addBookmark);
el("previousBookmark").addEventListener("click", () => jumpBookmark(true));
el("nextBookmark").addEventListener("click", () => jumpBookmark(false));
el("addViewport").addEventListener("click", () => {
  if (state.viewports.length < 4) {
    state.viewports.push({ ...state.viewports[state.active] });
    state.active = state.viewports.length - 1;
    renderAll();
    scheduleQuery();
    save();
  }
});
el("removeViewport").addEventListener("click", () => {
  if (state.viewports.length > 1) {
    state.viewports.splice(state.active, 1);
    state.data.clear();state.rendered.clear();
    state.active = Math.max(0, state.active - 1);
    renderAll();
    scheduleQuery();
    save();
  }
});
el("syncViewports").addEventListener("change", (event) => {
  state.syncViewports = event.target.checked;
  if (state.syncViewports) {
    const view = { ...state.viewports[state.active] };
    state.viewports = state.viewports.map(() => ({ ...view }));
    renderAll();
    scheduleQuery();
  }
  save();
});
el("viewports").addEventListener("dblclick", (event) => {
  const viewport = event.target.closest?.(".viewport");
  if (viewport) {
    state.active = [...viewport.parentElement.children].indexOf(viewport);
    fit();
  }
});
el("wavePane").addEventListener("scroll", () => {
  const rows = el("displayRows");
  if (rows.scrollTop !== el("wavePane").scrollTop) rows.scrollTop = el("wavePane").scrollTop;
  renderDisplay();renderViewports();scheduleQuery();
});
el("displayRows").addEventListener("scroll", () => {
  const wave = el("wavePane");
  if (wave.scrollTop !== el("displayRows").scrollTop) wave.scrollTop = el("displayRows").scrollTop;
});
el('displayRows').addEventListener('dragover', event => event.preventDefault());
el('displayRows').addEventListener('drop', event => {
  if (!event.target.closest('.displayRow')) { event.preventDefault();dropOn(null); }
});
window.addEventListener("pointermove", moveMiddlePan);
window.addEventListener("pointermove", moveSplitter);
window.addEventListener("pointerup", endMiddlePan);
window.addEventListener("pointerup", endSplitter);
window.addEventListener("pointercancel", endMiddlePan);
window.addEventListener("pointercancel", endSplitter);
for (const splitter of document.querySelectorAll("[data-splitter]")) splitter.addEventListener("pointerdown", (event) => {
  if (event.button !== 0) return;
  event.preventDefault();
  splitter.setPointerCapture(event.pointerId);
  const name = splitter.dataset.splitter;
  state.splitterDrag = { pointerId: event.pointerId, name, element: splitter, startX: event.clientX, startWidth: state.panelLayout[name] };
  splitter.classList.add("dragging");
});
el("overview").addEventListener("pointerdown", (event) => {
  if (event.button !== 0) return;
  event.preventDefault();
  el("overview").setPointerCapture(event.pointerId);
  const view = state.viewports[state.active];
  state.overviewDrag = { pointerId: event.pointerId, span: view.end - view.start };
  moveOverview(event);
});
el("overview").addEventListener("pointermove", moveOverview);
el("overview").addEventListener("pointerup", endOverview);
el("overview").addEventListener("pointercancel", endOverview);
window.addEventListener("blur", () => {
  state.middlePan = null;
  document.body.classList.remove("middlePanning");
  if(persistTimer!==null)persistLayout();
});
window.addEventListener('pagehide',()=>{if(persistTimer!==null)persistLayout();});
window.addEventListener("keydown", (event) => {
  if(waveformEditKey(event))return;
  if (["INPUT", "SELECT", "TEXTAREA"].includes(event.target?.tagName)) return;
  if (event.key === "ArrowLeft") {
    event.preventDefault();
    jumpChange(true, event.shiftKey ? "rising" : event.ctrlKey ? "falling" : "any_change");
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    jumpChange(false, event.shiftKey ? "rising" : event.ctrlKey ? "falling" : "any_change");
  }
});
new ResizeObserver(renderViewports).observe(el("wavePane"));
bindWaveformClipboard();
window.addEventListener("message", (event) => receive(event.data));
applyPanelLayout();
vscode.postMessage({ type: "ready" });
renderAll();
