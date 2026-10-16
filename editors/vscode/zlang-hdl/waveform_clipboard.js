// SPDX-License-Identifier: Apache-2.0
'use strict';
const {serializeSessionLayout,rebindSessionLayout}=require('./waveform_session');
const WAVEFORM_CLIPBOARD_SCHEMA='zlang-waveform-rows-v1';
const MAX_WAVEFORM_CLIPBOARD_BYTES=1024*1024;

// Select complete subtrees once, even when a parent and its child are selected.
function selectedWaveformRows(items,ids){
  const selected=new Set(ids),result=[];
  function collect(children){for(const item of children){
    if(selected.has(item.id))result.push(item);
    else if(item.type==='group')collect(item.children);
  }}
  collect(items);return result;
}
function parseWaveformClipboard(text){
  if(typeof text!=='string'||text.length>MAX_WAVEFORM_CLIPBOARD_BYTES
    ||new TextEncoder().encode(text).length>MAX_WAVEFORM_CLIPBOARD_BYTES)
    throw new Error('Waveform clipboard exceeds 1 MiB');
  let payload;try{payload=JSON.parse(text);}catch{throw new Error('Clipboard does not contain ZLang waveform rows');}
  if(payload?.schema!==WAVEFORM_CLIPBOARD_SCHEMA||!Array.isArray(payload.items)||!payload.items.length)
    throw new Error('Clipboard does not contain ZLang waveform rows');
  return serializeSessionLayout(rebindSessionLayout(payload.items,[]),new Map());
}
function encodeWaveformClipboard(items){
  const text=JSON.stringify({schema:WAVEFORM_CLIPBOARD_SCHEMA,items});
  const canonical=JSON.stringify({schema:WAVEFORM_CLIPBOARD_SCHEMA,items:parseWaveformClipboard(text)});
  parseWaveformClipboard(canonical);return canonical;
}
// Build and validate a fresh layout before publishing any paste mutation.
function pasteWaveformClipboard(items,text,catalog,targetId,newId){
  const layout=rebindSessionLayout(items,catalog);
  const incoming=rebindSessionLayout(parseWaveformClipboard(text),catalog),ids=[];
  function identify(children){for(const item of children){
    item.id=newId();ids.push(item.id);if(item.type==='group')identify(item.children);
  }}
  identify(incoming);
  function insert(children){for(let index=0;index<children.length;index++){
    const item=children[index];
    if(item.id===targetId){
      if(item.type==='group'){item.children.push(...incoming);item.collapsed=false;}
      else children.splice(index+1,0,...incoming);
      return true;
    }
    if(item.type==='group'&&insert(item.children))return true;
  }return false;}
  if(!insert(layout))layout.push(...incoming);
  const encoded=serializeSessionLayout(layout,new Map(catalog.map(signal=>[signal.id,signal])));
  return {items:rebindSessionLayout(encoded,catalog),ids,roots:incoming.map(item=>item.id)};
}
module.exports={WAVEFORM_CLIPBOARD_SCHEMA,MAX_WAVEFORM_CLIPBOARD_BYTES,
  selectedWaveformRows,parseWaveformClipboard,encodeWaveformClipboard,pasteWaveformClipboard};
