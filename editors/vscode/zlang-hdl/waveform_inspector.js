// SPDX-License-Identifier: Apache-2.0
// Browser domain functions, composed before minification by the asset builder.
function appendMetric(host, label, value) {
  const row = document.createElement("div");
  row.className = "metric";
  const key = document.createElement("span");
  key.textContent = label;
  const out = document.createElement("span");
  out.textContent = value;
  row.append(key, out);
  host.append(row);
}
function timeScaleSeconds() {
  return waveformTimeScale(state.metadata);
}
function renderInspector() {
  const host = el("inspectorBody");
  if(host.contains(document.activeElement)&&document.activeElement.matches('input,textarea,select'))return;
  host.replaceChildren();
  const selected = state.selectedSignalId && state.byId.get(state.selectedSignalId);
  const selectedItem=findItem(state.selectedRowId);
  const signalCard = document.createElement("section");
  signalCard.className = "card";
  const signalTitle = document.createElement("h3");
  signalTitle.textContent = "Selection";
  signalCard.append(signalTitle);
  appendMetric(signalCard, "Signal", selected?.name || selectedItem?.unresolved?.name || "All displayed");
  if(selectedItem?.holder){
    appendMetric(signalCard,'Status','holder — previous trace');
    const notice=document.createElement('div');notice.className='muted';
    notice.textContent='Missing from the current trace. The faded diagram is a saved visible tile, not current data. Delete this row with ×.';
    signalCard.append(notice);
    for(const frame of selectedItem.holder.frames)appendMetric(signalCard,'Saved trace ticks',frame.start+' … '+frame.end);
  }
  if (selected) {
    if(!waveformNumericType(selected).real)appendMetric(signalCard, "Width", selected.width + " bits");
    appendMetric(signalCard, "Kind", selected.kind || "signal");
    if(selected.type_hint)appendMetric(signalCard,'Type',selected.type_hint);
    if(selected.direction)appendMetric(signalCard,'Direction',selected.direction);
    if(selected.domain)appendMetric(signalCard,'Clock domain',selected.domain);
    const actions = document.createElement("div");
    actions.className = "inspectorActions";
    for (const [label, handler] of [["Alias", async () => {
      const next = await promptText("Signal alias", selectedItem.alias || "");
      if (next !== null) {
        selectedItem.alias = String(next).slice(0, 128);
        renderAll();
        save();
      }
    }],['Mnemonics',async()=>{
      const next=await promptText('Mnemonic map (JSON bit string to label)',JSON.stringify(selectedItem.mnemonic||{}));
      if(next!==null)try{selectedItem.mnemonic=sessionPresentation({mnemonic:JSON.parse(next)}).mnemonic;selectedItem.format='enum';renderAll();save();}catch(error){el('searchReadout').textContent=error.message;}
    }]]) {
      const button = document.createElement("button");
      button.textContent = label;
      button.addEventListener("click", handler);
      actions.append(button);
    }
    if(selectedItem){signalCard.append(actions);renderRowPresentation(signalCard,selectedItem);}
    if(selected.history_start>0)appendMetric(signalCard,'History begins',String(selected.history_start/2)+' events (restart for full history)');
  }
  if(selectedItem){
    const hint=document.createElement('div');hint.className='muted';
    hint.textContent='Right-click rows for Cut / Copy / Paste. Ctrl/Cmd+X, C, V also work.';signalCard.append(hint);
  }
  host.append(signalCard);
  const measurementCard = document.createElement("section");
  measurementCard.className = "card";
  const measurementTitle = document.createElement("h3");
  measurementTitle.textContent = "A \u2194 B";
  measurementCard.append(measurementTitle);
  if (state.marker === null) {
    const hint = document.createElement("div");
    hint.className = "muted";
    hint.textContent = "Shift+click to place cursor B.";
    measurementCard.append(hint);
  } else {
    const delta = Math.abs(state.marker - state.cursor);
    appendMetric(measurementCard, "Trace ticks", String(delta));
    if (state.metadata.time_domain === "zlang_event_ticks") appendMetric(measurementCard, "Events", String(delta / Math.max(1, state.metadata.ticks_per_event || 2)));
    const seconds = timeScaleSeconds();
    if (seconds !== null) {
      const physical = delta * seconds;
      appendMetric(measurementCard, "Physical time", physical.toExponential(4) + " s");
      appendMetric(measurementCard, "1 / Δt", physical > 0 ? (1 / physical).toPrecision(6) + " Hz" : "\u2014");
    }
    if (selected) {
      const exact = state.statistics?.signals?.find((item) => item.signal === selected.id);
      const measurement = exact ? waveformMeasurement(exact,Math.floor(state.cursor),Math.floor(state.marker),state.metadata) : null;
      appendMetric(measurementCard, "Transitions", measurement?String(measurement.transitions):'Querying…');
      if (measurement && selected.width === 1) {
        appendMetric(measurementCard, "Rising / falling", measurement.rising + " / " + measurement.falling);
        appendMetric(measurementCard, "High / low", measurement.highTicks + " / " + measurement.lowTicks);
        appendMetric(measurementCard, "Duty", measurement.dutyCycle === null ? "\u2014" : (measurement.dutyCycle * 100).toFixed(1) + "%");
      }
      if(measurement&&selected.width>1){appendMetric(measurementCard,'Unsigned Δ',measurement.unsignedDelta??'—');appendMetric(measurementCard,'Signed Δ',measurement.signedDelta??'—');}
    }
  }
  host.append(measurementCard);
  const bookmarkCard = document.createElement("section");
  bookmarkCard.className = "card";
  const bookmarkTitle = document.createElement("h3");
  bookmarkTitle.textContent = "Bookmarks";
  bookmarkCard.append(bookmarkTitle);
  if (!state.bookmarks.length) {
    const empty = document.createElement("div");
    empty.className = "muted";
    empty.textContent = "No bookmarks.";
    bookmarkCard.append(empty);
  }
  for (const bookmark of state.bookmarks) {
    const row = document.createElement("button");
    row.className = "bookmark";
    row.title = "Jump to " + bookmark.label;
    const swatch = document.createElement("span");
    swatch.className = "swatch";
    swatch.style.background = bookmark.color;
    const label = document.createElement("span");
    label.textContent = bookmark.label;
    const time = document.createElement("span");
    time.textContent = String(bookmark.time);
    row.append(swatch, label, time);
    row.addEventListener("click", () => {
      state.cursor = bookmark.time;
      state.statistics=null;
      revealTime(state.cursor);
      scheduleStatistics();
      save();
    });
    row.addEventListener('dblclick',async()=>{
      const next=await promptText('Bookmark label',bookmark.label);
      if(next!==null){bookmark.label=String(next).slice(0,64);renderAll();save();}
    });
    const remove=document.createElement('button');remove.textContent='×';remove.title='Remove '+bookmark.label;
    remove.addEventListener('click',()=>{state.bookmarks=state.bookmarks.filter(item=>item.id!==bookmark.id);renderAll();save();});
    const entry=document.createElement('div');entry.className='bookmarkEntry';entry.append(row,remove);bookmarkCard.append(entry);
  }
  host.append(bookmarkCard);
}
async function addBookmark() {
  if (state.bookmarks.length >= MAX_BOOKMARKS) {
    el("searchReadout").textContent = "bookmark limit reached";
    return;
  }
  const label = await promptText("Bookmark label", "M" + (state.bookmarks.length + 1));
  if (label === null) return;
  state.bookmarks.push({ id: uid("bookmark"), time: state.cursor, label: String(label || "M" + (state.bookmarks.length + 1)).slice(0, 64), color: ["#d7a84d", "#b78cff", "#ff9d45", "#77d84b"][state.bookmarks.length % 4] });
  renderAll();
  save();
}
function jumpBookmark(reverse) {
  if (!state.bookmarks.length) return;
  const ordered = [...state.bookmarks].sort((a, b) => a.time - b.time || a.id.localeCompare(b.id)), candidates = ordered.filter((item) => reverse ? item.time < state.cursor : item.time > state.cursor), bookmark = reverse ? candidates.at(-1) : candidates[0];
  if (bookmark) {
    state.cursor = bookmark.time;
    revealTime(state.cursor);
    scheduleStatistics();
    save();
  }
}
function zoomAB() {
  if (state.marker === null) return;
  setRange(state.active, Math.min(state.cursor, state.marker), Math.max(state.cursor, state.marker));
}
module.exports = { appendMetric, timeScaleSeconds, renderInspector, addBookmark, jumpBookmark, zoomAB };
