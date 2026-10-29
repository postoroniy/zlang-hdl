// SPDX-License-Identifier: Apache-2.0
'use strict';

const WAVEFORM_SESSION_SCHEMA = 'zlang-waveform-session-v1';
const MAX_SESSION_ITEMS = 4096;
const MAX_LAYOUT_ITEMS = 8192;
const MAX_BOOKMARKS = 256;

// Capacity counts all rows, including hidden groups and repeated channels.
function waveformLayoutUsage(items) {
  const usage = { rows: 0, signals: 0 };
  function count(children) {
    for (const item of children) {
      usage.rows++;
      if (item.type === 'signal') usage.signals++;
      else if (item.type === 'group') count(item.children);
    }
  }
  count(items);
  return usage;
}

function signalKey(signal) { return signal.name + '\u0000' + signal.width; }

function sessionColor(value, fallback) {
  if (value === undefined) return fallback;
  const named={red:'#ff0000',green:'#008000',blue:'#0000ff',yellow:'#ffff00',orange:'#ffa500',
    purple:'#800080',cyan:'#00ffff',magenta:'#ff00ff',white:'#ffffff',black:'#000000',
    gray:'#808080',grey:'#808080',lime:'#00ff00',pink:'#ffc0cb',brown:'#a52a2a',teal:'#008080'};
  if(typeof value==='string'&&Object.hasOwn(named,value.toLowerCase()))return named[value.toLowerCase()];
  if (typeof value !== 'string' || !/^#[0-9a-f]{6}$/i.test(value))
    throw new Error('Waveform color must be #RRGGBB');
  return value;
}
function sessionPresentation(item, width=4096) {
  const mnemonic = Object.create(null);
  if (item.mnemonic !== undefined) {
    if (!item.mnemonic || Array.isArray(item.mnemonic) || typeof item.mnemonic !== 'object'
      || Object.keys(item.mnemonic).length > 256) throw new Error('Invalid mnemonic map');
    for (const [bits, label] of Object.entries(item.mnemonic)) {
      if (!/^[01uxz]{1,4096}$/i.test(bits) || typeof label !== 'string' || label.length > 128)
        throw new Error('Mnemonic entries require bit strings and short labels');
      mnemonic[bits.toLowerCase()] = label;
    }
  }
  return {
    alias: typeof item.alias === 'string' ? item.alias.slice(0,128) : undefined,
    format: ['hex','binary','unsigned','signed','enum','typed'].includes(item.format) ? item.format : undefined,
    color: sessionColor(item.color, undefined),
    height: Number.isInteger(item.height) ? Math.max(24,Math.min(128,item.height)) : undefined,
    mnemonic,
    ...(item.display===undefined?{}:{display:sessionDisplay(item.display)}),
    ...(item.analog===undefined?{}:{analog:sessionAnalog(item.analog,width)}),
  };
}
function sessionDisplay(value) {
  if(!['auto','digital','analog'].includes(value))throw new Error('Invalid waveform display mode');
  return value;
}
function sessionAnalog(value,width=4096) {
  if(!value||typeof value!=='object'||Array.isArray(value)
    ||Object.keys(value).some(key=>!['signed','fraction'].includes(key)))
    throw new Error('Invalid analog interpretation');
  const result={};
  if(value.signed!==undefined){
    if(typeof value.signed!=='boolean')throw new Error('Analog signedness must be boolean');
    result.signed=value.signed;
  }
  if(value.fraction!==undefined){
    if(!Number.isInteger(value.fraction)||value.fraction<0||value.fraction>4095||value.fraction>=width)
      throw new Error('Analog fraction must be 0..4095 bits and less than the signal width');
    result.fraction=value.fraction;
  }
  return result;
}
function serializeSessionLayout(items,catalogById) {
  const count={value:0};
  function encode(children,depth) {
    if(!Array.isArray(children)||depth>8)throw new Error('Invalid waveform group nesting');
    return children.map(item=>{
      if(++count.value>MAX_LAYOUT_ITEMS)throw new Error('Waveform session exceeds 8192 rows');
      if(item.type==='group')return {type:'group',id:String(item.id),name:String(item.name||'Group').slice(0,128),collapsed:Boolean(item.collapsed),children:encode(item.children,depth+1)};
      if(item.type==='divider')return {type:'divider',id:String(item.id),name:String(item.name||'').slice(0,128)};
      if(item.type!=='signal')throw new Error('Unknown waveform row type');
      const signal=catalogById.get(item.signalId)||item.unresolved;
      if(!signal)throw new Error('Waveform row has no signal identity');
      return {type:'signal',id:String(item.id),signal:{name:signal.name,width:signal.width},...sessionPresentation(item,signal.width)};
    });
  }
  return encode(items,0);
}
function rebindSessionLayout(items,catalog) {
  const byKey=new Map();
  for(const signal of catalog){const key=signalKey(signal);byKey.set(key,byKey.has(key)?null:signal);}
  const ids=new Set();let count=0,signalRows=0;
  function decode(children,depth) {
    if(!Array.isArray(children)||depth>8)throw new Error('Invalid waveform group nesting');
    return children.map(item=>{
      if(!item||typeof item!=='object'||++count>MAX_LAYOUT_ITEMS)
        throw new Error('Waveform session exceeds 8192 rows or has an invalid row');
      const id=typeof item.id==='string'&&item.id?item.id:'session-row-'+count;
      if(ids.has(id))throw new Error('Duplicate waveform row identity: '+id);
      ids.add(id);
      if(item.type==='group')return {type:'group',id,name:String(item.name||'Group').slice(0,128),collapsed:Boolean(item.collapsed),children:decode(item.children,depth+1)};
      if(item.type==='divider')return {type:'divider',id,name:String(item.name||'').slice(0,128)};
      const identity=item.signal;
      if(item.type!=='signal'||!identity||typeof identity.name!=='string'||!identity.name
        ||!Number.isInteger(identity.width)||identity.width<1)
        throw new Error('Invalid signal identity in waveform session');
      const signal=byKey.get(signalKey(identity));
      if(++signalRows>MAX_SESSION_ITEMS)throw new Error('Waveform session exceeds 4096 signal rows');
      return {type:'signal',id,signalId:signal?.id||null,
        unresolved:signal?null:{name:identity.name,width:identity.width},...sessionPresentation(item,identity.width)};
    });
  }
  return decode(items,0);
}
function sessionTimestamp(value,minimum,maximum,label) {
  if(typeof value!=='number'||!Number.isFinite(value)||Math.abs(value)>Number.MAX_SAFE_INTEGER)
    throw new Error(label+' must be a safe finite timestamp');
  return Math.max(minimum,Math.min(maximum,value));
}
function normalizeBookmarks(bookmarks,minimum,maximum) {
  if(bookmarks===undefined)return [];
  if(!Array.isArray(bookmarks)||bookmarks.length>MAX_BOOKMARKS)
    throw new Error('Waveform session supports at most 256 bookmarks');
  const ids=new Set();
  return bookmarks.map((item,index)=>{
    if(!item||typeof item!=='object')throw new Error('Invalid waveform bookmark');
    const id=String(item.id||'bookmark-'+(index+1));
    if(ids.has(id))throw new Error('Duplicate bookmark identity');ids.add(id);
    return {id,time:sessionTimestamp(item.time,minimum,maximum,'Bookmark'),
      label:String(item.label||'M'+(index+1)).slice(0,64),color:sessionColor(item.color,'#d7a84d')};
  });
}
function normalizeWaveformSession(session,catalog,minimum,maximum) {
  if(!session||session.schema!==WAVEFORM_SESSION_SCHEMA)
    throw new Error('Unsupported waveform session schema');
  if(!Array.isArray(session.viewports)||session.viewports.length<1||session.viewports.length>4)
    throw new Error('Waveform session requires 1..4 viewports');
  const items=rebindSessionLayout(session.items,catalog);
  const viewports=session.viewports.map(view=>{
    if(!view||typeof view!=='object')throw new Error('Invalid waveform viewport');
    if(typeof view.start!=='number'||typeof view.end!=='number'||view.start>=view.end)
      throw new Error('Waveform viewport start must precede end');
    const start=sessionTimestamp(view.start,minimum,maximum,'Viewport start');
    const end=sessionTimestamp(view.end,minimum,maximum,'Viewport end');
    if(start>=end)return{start:minimum,end:maximum};
    return {start,end};
  });
  const sizes=session.panelLayout||{};
  const size=(key,fallback,min,max)=>{
    if(sizes[key]===undefined)return fallback;
    const value=sizes[key];if(!Number.isFinite(value))throw new Error('Invalid splitter size');
    return Math.max(min,Math.min(max,value));
  };
  const selected=session.selectedSignal&&catalog.find(signal=>
    signal.name===session.selectedSignal.name&&signal.width===session.selectedSignal.width);
  const rows=[];
  function collect(children){for(const item of children){rows.push(item);if(item.type==='group')collect(item.children);}}
  collect(items);
  const active=rows.find(row=>row.id===session.selectedRow)
    ||rows.find(row=>selected&&row.signalId===selected.id);
  if(session.selectedRows!==undefined&&(!Array.isArray(session.selectedRows)
    ||session.selectedRows.length>MAX_LAYOUT_ITEMS||session.selectedRows.some(id=>typeof id!=='string')))
    throw new Error('Invalid waveform row selection');
  const requested=new Set(session.selectedRows||[]);
  const selectedRows=rows.filter(row=>requested.has(row.id)).map(row=>row.id);
  if(active&&!selectedRows.includes(active.id))selectedRows.push(active.id);
  return {items,viewports,activeViewport:Math.max(0,Math.min(viewports.length-1,
    Number.isInteger(session.activeViewport)?session.activeViewport:0)),
    panelLayout:{browser:size('browser',240,160,520),signals:size('signals',400,260,700),inspector:size('inspector',270,190,520)},
    syncViewports:Boolean(session.syncViewports),cursorA:sessionTimestamp(session.cursorA??minimum,minimum,maximum,'Cursor A'),
    cursorB:session.cursorB===null||session.cursorB===undefined?null:sessionTimestamp(session.cursorB,minimum,maximum,'Cursor B'),
    bookmarks:normalizeBookmarks(session.bookmarks,minimum,maximum),
    selectedRowId:active?.id||null,selectedRowIds:selectedRows,selectedSignalId:active?.signalId||null,
    format:['hex','binary','unsigned','signed','enum','typed'].includes(session.format)?session.format:'hex',
    display:session.display===undefined?'auto':sessionDisplay(session.display),
    search:{kind:['exact','pattern','any_change','rising','falling','unknown_entry','uninitialized_entry'].includes(session.search?.kind)?session.search.kind:'any_change',value:typeof session.search?.value==='string'?session.search.value.slice(0,4096):''},
    inspectorCollapsed:Boolean(session.inspectorCollapsed),followLive:session.followLive!==false,
  };
}
module.exports={WAVEFORM_SESSION_SCHEMA,MAX_SESSION_ITEMS,MAX_LAYOUT_ITEMS,MAX_BOOKMARKS,waveformLayoutUsage,signalKey,
  sessionColor,sessionPresentation,sessionDisplay,sessionAnalog,serializeSessionLayout,rebindSessionLayout,
  sessionTimestamp,normalizeBookmarks,normalizeWaveformSession};
