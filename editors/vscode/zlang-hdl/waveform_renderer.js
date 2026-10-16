// SPDX-License-Identifier: Apache-2.0
// Browser domain functions, composed before minification by the asset builder.
function formatBits(bits, width, format = state.format) {
  const s = String(bits || "").toLowerCase();
  if (!s) return "\u2014";
  if (format === "binary" || kind(s) !== "known") return s;
  if (!/^[01]+$/.test(s)) return s;
  const raw = BigInt("0b" + s);
  if (format === "hex") return "0x" + raw.toString(16).padStart(Math.ceil((width || s.length) / 4), "0");
  if (format === "unsigned") return String(raw);
  const w = BigInt(width || s.length), sign = 1n << w - 1n;
  return String(raw & sign ? raw - (1n << w) : raw);
}
function formatSignalBits(bits, signal, item, display) {
  const normalized = String(bits || "").toLowerCase();
  if (!normalized) return display||"\u2014";
  if (/^-+$/.test(normalized)) return 'inactive variant';
  if (/^\?+$/.test(normalized)) return 'unknown variant';
  const custom = item?.mnemonic && item.mnemonic[normalized], declared = (signal?.enum_values || []).find((value) => String(value.bits).toLowerCase() === normalized)?.name;
  const format=item?.format||state.format;
  if(format==='typed')return display||formatBits(normalized,signal?.width,'binary');
  if(format==='enum'&&(custom||declared))return custom||declared;
  return formatBits(normalized, signal?.width, format==='enum'?'hex':format);
}
function signalFor(item) {
  return state.byId.get(item.signalId);
}
function valuesFor(viewport, signalId) {
  return state.data.get(viewport)?.get(signalId) || [];
}
function valueAt(values, time) {
  let lo = 0, hi = values.length - 1, best = null;
  while (lo <= hi) {
    const m = lo + hi >> 1;
    if (values[m].event <= time) {
      best = values[m].bits;
      lo = m + 1;
    } else hi = m - 1;
  }
  return best;
}
function timeToX(time, width, view) {
  return pad + (time - view.start) / (view.end - view.start) * Math.max(1, width - pad * 2);
}
function xToTime(x, width, view) {
  return view.start + (x - pad) / Math.max(1, width - pad * 2) * (view.end - view.start);
}
function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1e-9)))), n = raw / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
}
function waveformRulerTicks(view) {
  const step=Math.max(1,niceStep((view.end-view.start)/8));
  const first=Math.ceil(view.start/step)*step,ticks=[];
  for(let index=0;index<64;index++){
    const timestamp=first+index*step;
    if(timestamp>view.end||!Number.isSafeInteger(timestamp))break;
    ticks.push(timestamp);
  }
  return ticks;
}
function drawWaveformSignal(ctx,item,signal,rendered,values,view,width,y,rowSize){
  const alpha=ctx.globalAlpha,top=y+6,bottom=y+rowSize-6,mid=(top+bottom)/2;
  if(rendered&&!rendered.exact){
    for(const bucket of rendered.buckets||[]){
      const x1=timeToX(bucket.start,width,view),x2=timeToX(bucket.end,width,view);
      ctx.fillStyle=bucket.has_x?colors.x:bucket.has_u?colors.u:bucket.has_z?colors.z:item.color||colors.wave;
      if(!bucket.transitions&&!bucket.has_u&&!bucket.has_x&&!bucket.has_z&&/^[01]+$/.test(bucket.last_bits||'')){
        const level=signal.width===1?(bucket.last_bits==='1'?top:bottom):mid;
        ctx.strokeStyle=ctx.fillStyle;ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(x1,level);ctx.lineTo(x2+1,level);ctx.stroke();continue;
      }
      ctx.globalAlpha=alpha*0.28;ctx.fillRect(x1,top,Math.max(1,x2-x1),bottom-top);ctx.globalAlpha=alpha;
      ctx.strokeStyle=ctx.fillStyle;ctx.lineWidth=1.4;ctx.beginPath();ctx.moveTo(x1,top);ctx.lineTo(x1,bottom);ctx.stroke();
    }
    return;
  }
  const points=[];let prior;
  for(const point of values){
    if(point.event<view.start)prior=point;else if(point.event<=view.end)points.push(point);
  }
  if(prior)points.unshift(prior);
  for(let i=0;i<points.length;i++){
    const point=points[i],previous=i>0?points[i-1]:null,next=i+1<points.length?points[i+1].event:view.end;
    const x1=timeToX(Math.max(point.event,view.start),width,view),x2=timeToX(next,width,view),k=kind(point.bits);
    ctx.strokeStyle=k==='known'?item.color||colors.wave:colors[k];ctx.fillStyle=ctx.strokeStyle;
    if(signal.width===1&&k==='known'){
      const level=point.bits.endsWith('1')?top:bottom;
      ctx.lineWidth=2.4;ctx.beginPath();
      if(previous&&kind(previous.bits)==='known'&&point.event>=view.start){ctx.moveTo(x1,previous.bits.endsWith('1')?top:bottom);ctx.lineTo(x1,level);}
      ctx.moveTo(x1,level);ctx.lineTo(x2,level);ctx.stroke();
      if(previous&&point.event>=view.start)ctx.fillRect(x1-1.5,top,3,bottom-top);
    }else{
      ctx.lineWidth=1.7;ctx.beginPath();ctx.moveTo(x1,mid);ctx.lineTo(x1+4,top);ctx.lineTo(x2-4,top);
      ctx.lineTo(x2,mid);ctx.lineTo(x2-4,bottom);ctx.lineTo(x1+4,bottom);ctx.closePath();ctx.stroke();
      if(k!=='known'){ctx.globalAlpha=alpha*0.12;ctx.fill();ctx.globalAlpha=alpha;}
      if(x2-x1>25){
        ctx.save();ctx.beginPath();ctx.rect(x1+5,top,Math.max(0,x2-x1-10),bottom-top);ctx.clip();
        ctx.font='11px monospace';ctx.textBaseline='middle';ctx.fillText(formatSignalBits(point.bits,signal,item,point.display),x1+7,mid);ctx.restore();
      }
    }
  }
}
function drawWaveformHolder(ctx,item,view,width,y,rowSize){
  const frame=waveformHolderFrame(item.holder,view);
  if(frame){
    ctx.save();ctx.globalAlpha=0.32;
    const start=Math.max(view.start,frame.start),end=Math.min(view.end,frame.end);
    ctx.beginPath();ctx.rect(timeToX(start,width,view),y,timeToX(end,width,view)-timeToX(start,width,view),rowSize);ctx.clip();
    // Clip the retained tile; never extrapolate it into unrecorded history.
    drawWaveformSignal(ctx,item,item.holder.signal,frame.rendered,frame.values,
      view,width,y,rowSize);
    ctx.restore();
  }
  ctx.font='11px sans-serif';ctx.textBaseline='top';
  const label=frame?'holder':item.holder.frames.length?'holder — outside saved range':'holder — snapshot unavailable';
  ctx.fillStyle=colors.bg;ctx.fillRect(pad,y+2,ctx.measureText(label).width+8,14);
  ctx.fillStyle=colors.muted;ctx.fillText(label,pad+4,y+3);
}
function drawViewport(canvas, index) {
  const view = state.viewports[index], ctx = canvas.getContext("2d"), width = canvas.clientWidth, height = canvas.clientHeight, dpr = devicePixelRatio || 1;
  if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = colors.bg;
  ctx.fillRect(0, 0, width, height);
  ctx.font = "11px sans-serif";
  ctx.textBaseline = "top";
  for (const t of waveformRulerTicks(view)) {
    const x = timeToX(t, width, view);
    ctx.strokeStyle = colors.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, rulerHeight - 8);
    ctx.lineTo(x, height);
    ctx.stroke();
    ctx.fillStyle = colors.muted;
    ctx.fillText(Number.isInteger(t) ? String(t) : t.toPrecision(4), x + 3, 7);
  }
  const scrollTop = el('wavePane').scrollTop;
  ctx.save();ctx.beginPath();ctx.rect(0, rulerHeight, width, height-rulerHeight);ctx.clip();
  const rows = waveformRowWindow(waveformRows(state.layout).rows, scrollTop, height-rulerHeight);
  rows.forEach(({item, top: rowTop, height: rowSize}, rowIndex) => {
    const signal = signalFor(item), y = rulerHeight + rowTop - scrollTop;
    const selected = state.selectedRowIds.has(item.id);
    ctx.fillStyle = selected ? colors.selection : rowIndex % 2 ? colors.alt : colors.bg;
    ctx.fillRect(0, y, width, rowSize);
    if (selected) {
      ctx.fillStyle = colors.cursor;
      ctx.fillRect(0, y, 3, rowSize);
    }
    if(item.holder){drawWaveformHolder(ctx,item,view,width,y,rowSize);return;}
    if (!signal) { ctx.fillStyle=colors.muted;ctx.font='11px sans-serif';ctx.fillText(item.name||item.unresolved?.name||'', pad,y+8);return; }
    const rendered = state.rendered.get(index)?.get(item.signalId);
    const range=state.renderedRanges.get(index);
    if(!range||range.end<view.start||range.start>view.end)return;
    // Never extend an old tile's last value into history not yet fetched.
    const left=timeToX(Math.max(range.start,view.start),width,view);
    const right=timeToX(Math.min(range.end,view.end),width,view);
    ctx.save();ctx.beginPath();ctx.rect(left,y,Math.max(0,right-left),rowSize);ctx.clip();
    drawWaveformSignal(ctx,item,signal,rendered,valuesFor(index,item.signalId),view,width,y,rowSize);
    ctx.restore();
  });
  ctx.restore();
  for (const bookmark of state.bookmarks) {
    if (bookmark.time < view.start || bookmark.time > view.end) continue;
    const x = timeToX(bookmark.time, width, view);
    ctx.strokeStyle = bookmark.color;
    ctx.setLineDash([3, 4]);
    ctx.beginPath();
    ctx.moveTo(x, rulerHeight);
    ctx.lineTo(x, height);
    ctx.stroke();
    ctx.setLineDash([]);
  }
  for (const [time, color, label] of [[state.liveMarker,'#4fe0c3','LIVE'],[state.cursor, colors.cursor, "A"], [state.marker, colors.marker, "B"]]) {
    if (time === null || time < view.start || time > view.end) continue;
    const x = timeToX(time, width, view);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height);
    ctx.stroke();
    ctx.fillStyle = color;
    const labelWidth=label==='LIVE'?36:16;
    ctx.fillRect(x-labelWidth/2, label==='LIVE'?16:0,labelWidth,15);
    ctx.fillStyle = "#0b1117";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(label, x, label==='LIVE'?23:7);
    ctx.textAlign = "start";
  }
}
function renderOverview() {
  const canvas = el("overview"), ctx = canvas.getContext("2d"), width = canvas.clientWidth, height = canvas.clientHeight, dpr = devicePixelRatio || 1;
  if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#0d1217";
  ctx.fillRect(0, 0, width, height);
  const full = { start: state.min, end: Math.max(state.min + 1, state.max) };
  ctx.strokeStyle = colors.wave;
  ctx.globalAlpha = 0.45;
  ctx.beginPath();
  const displayed=new Set(visibleSignals().map(item=>item.signalId));
  for (const signal of state.overview?.signals || []) {
    if(!displayed.has(signal.signal))continue;
    if (signal.exact) {
      for (const point of signal.points || []) {
        const x = timeToX(point.timestamp, width, full);
        ctx.moveTo(x, 8);
        ctx.lineTo(x, height - 8);
      }
    } else for (const bucket of signal.buckets || []) {
      if (bucket.transitions || bucket.has_u || bucket.has_x || bucket.has_z) {
        const x = timeToX(bucket.start, width, full);
        ctx.moveTo(x, 8);
        ctx.lineTo(x, height - 8);
      }
    }
  }
  ctx.stroke();
  ctx.globalAlpha = 1;
  state.viewports.forEach((view, index) => {
    const x1 = timeToX(view.start, width, full), x2 = timeToX(view.end, width, full);
    ctx.fillStyle = index === state.active ? "rgba(102,184,255,.18)" : "rgba(149,160,170,.10)";
    ctx.fillRect(x1, 3, Math.max(2, x2 - x1), height - 6);
    ctx.strokeStyle = index === state.active ? colors.cursor : colors.muted;
    ctx.strokeRect(x1 + 0.5, 3.5, Math.max(1, x2 - x1 - 1), height - 7);
  });
}
function renderViewports() {
  const host = el("viewports"), geometry = waveformRows(state.layout);
  const height = Math.max(el('wavePane').clientHeight, rulerHeight + geometry.height);
  while(host.children.length>state.viewports.length)host.lastElementChild.remove();
  state.viewports.forEach((_, index) => {
    let container = host.children[index];
    if (!container) {
      container = document.createElement('div');
      const canvas = document.createElement('canvas');
      container.append(canvas);host.append(container);bindViewport(canvas,index);
    }
    container.className = "viewport " + (index === state.active ? "active" : "");
    container.style.height=height+'px';
    const canvas=container.firstElementChild;
    canvas.style.width = Math.max(280, el("wavePane").clientWidth / state.viewports.length) + "px";
    canvas.style.height = Math.max(100,el('wavePane').clientHeight)+'px';
    drawViewport(canvas, index);
  });
}
module.exports = { formatBits, formatSignalBits, signalFor, valuesFor, valueAt, timeToX, xToTime, niceStep, waveformRulerTicks, drawWaveformSignal, drawWaveformHolder, drawViewport, renderOverview, renderViewports };
