// SPDX-License-Identifier: Apache-2.0
// Browser domain functions, composed before minification by the asset builder.
function buildSourceTree() {
  const host = el("tree"), filter = el("filter").value.trim().toLowerCase(), root = { path: "", children: /* @__PURE__ */ new Map(), signals: [] };
  const counts=new Map();walk(state.layout,item=>{if(item.type==='signal')counts.set(item.signalId,(counts.get(item.signalId)||0)+1);});
  const describe=signal=>signal.width+'b'+(counts.has(signal.id)?' · '+counts.get(signal.id)+' rows':'');
  if(state.browserCatalog===state.catalog&&state.browserFilter===filter){
    for(const row of host.querySelectorAll('.variable[data-signal-id]')){
      row.classList.toggle('selected',state.selectedSignalIds.has(row.dataset.signalId)||state.browserSelection.has(row.dataset.signalId));
      row.querySelector('.width').textContent=describe(state.byId.get(row.dataset.signalId));
    }
    return;
  }
  state.browserCatalog=state.catalog;state.browserFilter=filter;
  for (const signal of state.catalog) {
    if (filter && !signal.name.toLowerCase().includes(filter)) continue;
    let node = root;
    for (const segment of (signal.scope || scope(signal.name)).split(".").filter(Boolean)) {
      const nextPath = node.path ? node.path + "." + segment : segment;
      if (!node.children.has(segment)) node.children.set(segment, { path: nextPath, children: /* @__PURE__ */ new Map(), signals: [] });
      node = node.children.get(segment);
    }
    node.signals.push(signal);
  }
  host.replaceChildren();
  function descendantSignals(node, result = []) {
    result.push(...node.signals);
    for (const child of node.children.values()) descendantSignals(child, result);
    return result;
  }
  function signalRow(signal) {
    const row = document.createElement("div");
    row.className = "variable";
    row.draggable = true;row.tabIndex=0;
    row.title = signal.name;
    row.dataset.signalId=signal.id;
    row.classList.toggle('selected',state.selectedSignalIds.has(signal.id));
    row.addEventListener('click',event=>{
      if(event.target.closest('button'))return;
      event.preventDefault();
      row.focus({preventScroll:true});
      if(event.ctrlKey||event.metaKey||event.shiftKey){
        if(state.browserSelection.has(signal.id))state.browserSelection.delete(signal.id);
        else state.browserSelection.add(signal.id);
      }else{state.browserSelection.clear();state.browserSelection.add(signal.id);}
      buildSourceTree();
    });
    row.addEventListener('dblclick',event=>{if(!event.target.closest('button'))addSignal(signal.id);});
    row.addEventListener("dragstart", () => {
      dragged = { source: "catalog", id: signal.id,ids:state.browserSelection.has(signal.id)?[...state.browserSelection]:[signal.id] };
    });
    const add=document.createElement('button');add.textContent='＋';add.className='addSignal';
    add.title='Add another row for '+signal.name;add.setAttribute('aria-label','Add '+signal.name);
    add.addEventListener('click',event=>{event.stopPropagation();addSignal(signal.id);});
    const label = document.createElement("span");
    label.textContent = leaf(signal.name);
    const width = document.createElement("span");
    width.className = "width";
    width.textContent = describe(signal);
    row.append(add, label, width);
    return row;
  }
  function renderScope(node, label, parent) {
    const details = document.createElement("details");
    details.className = "scope";
    details.open = Boolean(filter) || state.openScopes.has(node.path) || node.path.split(".").length < 2;
    const summary = document.createElement("summary"), name = document.createElement("span");
    name.textContent = label || "<root>";
    const add = document.createElement("button");
    add.textContent = "\uFF0B";
    add.title = "Add signals in this scope";
    add.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      addSignals(node.signals);
    });
    const deep=document.createElement('button');deep.textContent='Deep';deep.title='Select Deep (up to 4096 signals)';
    deep.addEventListener('click',event=>{event.preventDefault();event.stopPropagation();addSignals(descendantSignals(node));});
    summary.append(name, add, deep);
    const body = document.createElement("div");
    body.className = "scopeBody";
    let populated = false;
    const populate = () => {
      if (populated) return;
      populated = true;
      const signals=node.signals.sort((a,b)=>a.name.localeCompare(b.name));let shown=0;
      const more=document.createElement('button');more.textContent='Show more signals';
      const page=()=>{more.remove();const end=Math.min(shown+200,signals.length);while(shown<end)body.append(signalRow(signals[shown++]));if(shown<signals.length)body.append(more);};
      more.addEventListener('click',page);page();
      for (const [childName, child] of [...node.children].sort(([a], [b]) => a.localeCompare(b))) renderScope(child, childName, body);
    };
    details.addEventListener("toggle", () => {
      if(details.open)state.openScopes.add(node.path);else state.openScopes.delete(node.path);
      if (details.open) populate();
    });
    details.append(summary, body);
    parent.append(details);
    if (details.open) populate();
  }
  for (const signal of root.signals) host.append(signalRow(signal));
  for (const [name, node] of [...root.children].sort(([a], [b]) => a.localeCompare(b))) renderScope(node, name, host);
}
function renderDisplay() {
  const host = el("displayRows");
  const scrollTop = host.scrollTop;
  const geometry = waveformRows(state.layout);
  const windowRows = waveformRowWindow(geometry.rows, scrollTop, host.clientHeight);
  host.replaceChildren();
  const spacer = height => { const node = document.createElement('div'); node.style.height = height + 'px'; host.append(node); };
  spacer(windowRows[0]?.top || 0);
  for (const {item, depth, height} of windowRows) {
      const row = document.createElement("div");
      row.style.height = height + 'px';
      row.className = "displayRow " + (item.type === "group" ? "groupRow " : "") + (item.type === "divider" ? "dividerRow " : "") + (state.selectedRowIds.has(item.id) ? "selected " : "") + (item.unresolved ? "unresolved " : "");
      row.draggable = true;
      row.dataset.id = item.id;
      row.classList.toggle('holderRow',Boolean(item.holder));
      row.style.paddingLeft = 7 + depth * 13 + "px";
      row.addEventListener("click", (event) => {
        if (!event.target.closest("button")) selectWaveformRow(item.id, true, event.ctrlKey || event.metaKey || event.shiftKey);
      });
      row.addEventListener("dragstart", () => {
        dragged = { source: "layout", id: item.id,ids:state.selectedRowIds.has(item.id)?[...state.selectedRowIds]:[item.id] };
      });
      row.addEventListener("dragover", (event) => event.preventDefault());
      row.addEventListener("drop", (event) => {
        event.preventDefault();
        dropOn(item.id);
      });
      const handle = document.createElement("span");
      handle.className = "dragHandle";
      handle.textContent = "\u283F";
      const name = document.createElement("span");
      name.className = "signalName";
      const valueA = document.createElement("span");
      valueA.className = "signalValue";
      const valueB = document.createElement("span");
      valueB.className = "signalValue";
      const delta = document.createElement("span");
      delta.className = "signalValue";
      const remove = document.createElement("button");
      remove.className = "remove";
      remove.textContent = "\xD7";
      remove.addEventListener("click", () => {
        if (item.type === "signal") {
          state.selectedRowIds.delete(item.id);
        }
        removeItem(state.layout, item.id);
        renderAll();
        save();
        scheduleQuery();
      });
      if (item.type === "group") {
        name.textContent = (item.collapsed ? "\u25B8 " : "\u25BE ") + item.name;
        name.addEventListener("dblclick", async () => {
          const next = await promptText("Group name", item.name);
          if (next) {
            item.name = next.slice(0,128);
            renderAll();
            save();
          }
        });
        name.addEventListener("click", () => {
          item.collapsed = !item.collapsed;
          renderAll();
          save();
          scheduleQuery();
        });
        valueA.textContent = item.children.length + " items";
      } else if (item.type === "divider") {
        name.textContent = "\u2014 " + (item.name || "divider") + " \u2014";
      } else {
        const signal = signalFor(item), label = item.alias || (signal ? leaf(signal.name) : item.unresolved ? leaf(item.unresolved.name) : "<missing>");
        name.textContent = label;
        if(item.holder){
          const badge=document.createElement('span');badge.className='holderBadge';badge.textContent='holder';name.append(' ',badge);
        }
        name.style.color=item.color||'';
        name.title = item.holder?'Signal missing from current trace; showing a faded previous snapshot':signal?.name || item.unresolved?.name || "";
        if(item.holder)remove.title='Delete holder';
        if (signal) row.waveformValueCells={signal,item,valueA,valueB,delta};
      }
      row.append(handle, name, valueA, valueB, delta, remove);
      updateWaveformRowValues(row.waveformValueCells);
      host.append(row);
  }
  const last = windowRows.at(-1);
  spacer(Math.max(0, geometry.height - (last ? last.top + last.height : 0)));
  host.scrollTop = scrollTop;
  el("empty").style.display = visibleSignals().length ? "none" : "grid";
}
function updateWaveformRowValues(cells){
  if(!cells)return;
  const {signal,item,valueA,valueB,delta}=cells;
  const bitsA=cursorValue(item.signalId),bitsB=cursorValue(item.signalId,true);
  const a=formatSignalBits(bitsA,signal,item,cursorDisplay(item.signalId));
  const b=state.marker===null?'\u2014':formatSignalBits(bitsB,signal,item,cursorDisplay(item.signalId,true));
  if(valueA.textContent!==a){valueA.textContent=a;valueA.title=a;}
  if(valueB.textContent!==b){valueB.textContent=b;valueB.title=b;}
  valueA.style.color=kind(bitsA)==='known'?item.color||colors.wave:colors[kind(bitsA)];
  valueB.style.color=kind(bitsB)==='known'?item.color||colors.wave:colors[kind(bitsB)];
  const difference=waveformBusDelta(bitsA,bitsB,signal.width,(item.format||state.format)==='signed')??'—';
  if(delta.textContent!==difference)delta.textContent=difference;
}
function renderDisplayValues(){
  for(const row of el('displayRows').querySelectorAll('.displayRow'))updateWaveformRowValues(row.waveformValueCells);
}
function dropOn(targetId) {
  if (!dragged) return;
  if(dragged.id===targetId){dragged=null;return;}
  const target = targetId && findItem(targetId);
  if (dragged.source === "catalog") {
    const selected=(dragged.ids||[dragged.id]).map(id=>state.byId.get(id)).filter(Boolean);
    dragged=null;addSignals(selected,target?.type==='group'?target:null);return;
  } else {
    if(!moveWaveformRows(state.layout,dragged.ids||[dragged.id],targetId))
      el('searchReadout').textContent='Cannot move rows into themselves or beyond eight group levels';
  }
  dragged = null;
  renderAll();
  save();
  scheduleQuery();
}
module.exports = { buildSourceTree, renderDisplay, updateWaveformRowValues, renderDisplayValues, dropOn };
