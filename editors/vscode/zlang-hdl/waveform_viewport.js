// SPDX-License-Identifier: Apache-2.0
// Browser domain functions, composed before minification by the asset builder.
function activateViewport(index) {
  if (state.active === index) return;
  state.active = index;
  [...el("viewports").children].forEach((item, position) => item.classList.toggle("active", position === index));
  renderDisplay();
}
function setVerticalScroll(value) {
  const wave = el("wavePane"), rows = el("displayRows"), next = clamp(value, 0, Math.max(0, wave.scrollHeight - wave.clientHeight));
  if (wave.scrollTop !== next) wave.scrollTop = next;
  if (rows.scrollTop !== next) rows.scrollTop = next;
}
function beginMiddlePan(event, index) {
  if (event.button !== 1) return;
  event.preventDefault();
  activateViewport(index);
  el('wavePane').setPointerCapture(event.pointerId);
  document.body.classList.add("middlePanning");
  const view = state.viewports[index];
  state.middlePan = { index, pointerId: event.pointerId, x: event.clientX, y: event.clientY, start: view.start, end: view.end, scrollTop: el("wavePane").scrollTop };
}
function moveMiddlePan(event) {
  const pan = state.middlePan;
  if (!pan || event.pointerId !== pan.pointerId) return;
  if ((event.buttons & 4) === 0) {
    state.middlePan = null;
    document.body.classList.remove("middlePanning");
    return;
  }
  event.preventDefault();
  const width = Math.max(1, el("wavePane").clientWidth / state.viewports.length), delta = -(event.clientX - pan.x) * (pan.end - pan.start) / width;
  setRange(pan.index, pan.start + delta, pan.end + delta);
  setVerticalScroll(pan.scrollTop - (event.clientY - pan.y));
}
function endMiddlePan(event) {
  if (state.middlePan?.pointerId === event.pointerId) {
    state.middlePan = null;
    document.body.classList.remove("middlePanning");
    scheduleQuery();
  }
}
function bindViewport(canvas, index) {
  const container = canvas.parentElement;
  container.addEventListener("auxclick", (event) => {
    if (event.button === 1) event.preventDefault();
  });
  container.addEventListener("pointerdown", (event) => {
    activateViewport(index);
    beginMiddlePan(event, index);
  });
  container.addEventListener("wheel", (event) => {
    event.preventDefault();
    activateViewport(index);
    const view = state.viewports[index], rect = canvas.getBoundingClientRect(), anchor = xToTime(event.clientX - rect.left, canvas.clientWidth, view), modeScale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? Math.max(1, canvas.clientWidth) : 1, horizontal = event.shiftKey || Math.abs(event.deltaX) > Math.abs(event.deltaY);
    if (horizontal) {
      const raw = event.deltaX || event.deltaY, delta = raw * modeScale * (view.end - view.start) / Math.max(1, canvas.clientWidth);
      setRange(index, view.start + delta, view.end + delta);
    } else zoom(Math.exp(event.deltaY * modeScale * 2e-3), anchor);
  }, { passive: false });
  canvas.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    canvas.setPointerCapture(event.pointerId);
    const view = state.viewports[index];
    state.drag = { index, x: event.clientX, start: view.start, end: view.end, moved: false };
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!state.drag || state.drag.index !== index) return;
    if (Math.abs(event.clientX - state.drag.x) > 3) state.drag.moved = true;
    if(state.drag.moved){
      const drag=state.drag,delta=-(event.clientX-drag.x)*(drag.end-drag.start)/Math.max(1,canvas.clientWidth);
      setRange(index,drag.start+delta,drag.end+delta);
    }
  });
  canvas.addEventListener("pointerup", (event) => {
    const drag = state.drag;
    state.drag = null;
    if (!drag) return;
    if (drag.moved) {
      const delta = -(event.clientX - drag.x) * (drag.end - drag.start) / Math.max(1, canvas.clientWidth);
      setRange(index, drag.start + delta, drag.end + delta);
    } else {
      const view = state.viewports[index], rect = canvas.getBoundingClientRect(), time = clamp(xToTime(event.clientX - rect.left, canvas.clientWidth, view), state.min, state.max);
      selectRowAtY(event.clientY - rect.top);
      const snapped=snapVisibleTime(time,index,canvas.clientWidth);
      if (event.shiftKey) state.marker = snapped;
      else state.cursor = snapped;
      if(state.live){state.followLive=false;el('followLive').classList.remove('enabled');}
      state.statistics = null;
      renderAll();
      scheduleStatistics();
      save();
    }
  });
  canvas.addEventListener('pointercancel',()=>{state.drag=null;scheduleQuery();});
}
function setRange(index, start, end) {
  const full = Math.max(1, state.max - state.min), span = clamp(end - start, Math.max(full / 1e5, 1e-6), full), center = (start + end) / 2;
  let a = center - span / 2, b = center + span / 2;
  if (a < state.min) {
    b += state.min - a;
    a = state.min;
  }
  if (b > state.max) {
    a -= b - state.max;
    b = state.max;
  }
  const next = { start: Math.max(state.min, a), end: Math.min(state.max, b) };
  if (state.syncViewports) state.viewports = state.viewports.map(() => ({ ...next }));
  else state.viewports[index] = next;
  renderTimeView();
  save();
  scheduleQuery();
}
function fit() {
  setRange(state.active, state.min, state.max === state.min ? state.min + 1 : state.max);
}
function zoom(factor, anchor = state.cursor) {
  const view = state.viewports[state.active], span = (view.end - view.start) * factor, ratio = (anchor - view.start) / (view.end - view.start);
  setRange(state.active, anchor - span * ratio, anchor + span * (1 - ratio));
}
function overviewTime(event) {
  const rect = el("overview").getBoundingClientRect(), full = { start: state.min, end: Math.max(state.min + 1, state.max) };
  return clamp(xToTime(event.clientX - rect.left, rect.width, full), state.min, state.max);
}
function moveOverview(event) {
  if (!state.overviewDrag || event.pointerId !== state.overviewDrag.pointerId) return;
  const center = overviewTime(event), span = state.overviewDrag.span;
  setRange(state.active, center - span / 2, center + span / 2);
}
function endOverview(event) {
  if (state.overviewDrag?.pointerId === event.pointerId) {
    state.overviewDrag = null;
    scheduleQuery();
  }
}
function applyPanelLayout() {
  const main = el("main");
  main.style.setProperty("--browser-width", state.panelLayout.browser + "px");
  main.style.setProperty("--signals-width", state.panelLayout.signals + "px");
  main.style.setProperty("--inspector-width", state.panelLayout.inspector + "px");
  main.classList.toggle('inspectorClosed',state.inspectorCollapsed);
  el('toggleInspector').classList.toggle('enabled',!state.inspectorCollapsed);
}
function snapVisibleTime(time,index,width) {
  if(!state.snapCursor)return time;
  const view=state.viewports[index],radius=(view.end-view.start)*8/Math.max(1,width-2*pad);
  let best=Math.round(time),distance=Infinity;
  for(const id of signalIdsForChangeJump()){
    const points=valuesFor(index,id);
    for(let position=1;position<points.length;position++){
      const point=points[position];if(point.bits===points[position-1].bits)continue;
      const delta=Math.abs(point.event-time);
      if(delta<=radius&&delta<distance){best=point.event;distance=delta;}
    }
  }
  return clamp(best,state.min,state.max);
}
function moveSplitter(event) {
  const drag = state.splitterDrag;
  if (!drag || event.pointerId !== drag.pointerId) return;
  const delta = event.clientX - drag.startX;
  if (drag.name === "browser") state.panelLayout.browser = clamp(drag.startWidth + delta, 160, 520);
  else if (drag.name === "signals") state.panelLayout.signals = clamp(drag.startWidth + delta, 260, 700);
  else state.panelLayout.inspector = clamp(drag.startWidth - delta, 190, 520);
  applyPanelLayout();
  renderViewports();
}
function endSplitter(event) {
  if (state.splitterDrag?.pointerId === event.pointerId) {
    state.splitterDrag.element.classList.remove("dragging");
    state.splitterDrag = null;
    scheduleQuery();
    save();
  }
}
module.exports = { activateViewport, setVerticalScroll, beginMiddlePan, moveMiddlePan, endMiddlePan, bindViewport, setRange, fit, zoom, overviewTime, moveOverview, endOverview, applyPanelLayout, snapVisibleTime, moveSplitter, endSplitter };
