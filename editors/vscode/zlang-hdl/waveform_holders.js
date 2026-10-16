// SPDX-License-Identifier: Apache-2.0
'use strict';
const {signalKey}=require('./waveform_session');
const MAX_HOLDER_BYTES=16*1024*1024;

// Owns only previously displayed tiles for missing signals, never full traces.
class WaveformHolderCache {
  constructor(limit=MAX_HOLDER_BYTES){this.limit=limit;this.bytes=0;this.entries=new Map();}
  remember(signal,frames){
    const key=signalKey(signal),record={signal,frames};
    const size=JSON.stringify(record).length*2;
    if(size>this.limit)return false;
    const previous=this.entries.get(key);if(previous)this.bytes-=previous.size;
    this.entries.delete(key);this.entries.set(key,{record,size});this.bytes+=size;
    while(this.bytes>this.limit){const key=this.entries.keys().next().value;this.bytes-=this.entries.get(key).size;this.entries.delete(key);}
    return true;
  }
  get(signal){return this.entries.get(signalKey(signal))?.record;}
  retain(keys){for(const [key,entry] of this.entries)if(!keys.has(key)){this.bytes-=entry.size;this.entries.delete(key);}}
  clear(){this.entries.clear();this.bytes=0;}
}
function waveformHolderFrame(holder,view){
  let best=null,overlap=0;
  for(const frame of holder.frames){
    const span=Math.min(frame.end,view.end)-Math.max(frame.start,view.start);
    if(span>overlap){best=frame;overlap=span;}
  }
  return best;
}
function rememberMissingWaveforms(catalog){
  const keys=new Set(catalog.map(signalKey)),missing=new Map();
  walk(state.layout,item=>{
    const signal=state.byId.get(item.signalId);
    if(signal&&!keys.has(signalKey(signal)))missing.set(signal.id,signal);
  });
  for(const [id,signal] of missing){
    const frames=[];
    for(const [index,records] of state.rendered){
      const rendered=records.get(id),range=state.renderedRanges.get(index);
      if(rendered&&range)frames.push({...range,rendered,values:state.data.get(index)?.get(id)||[]});
    }
    state.holders.remember(signal,frames);
  }
}
function syncWaveformHolders(){
  const retained=new Set(state.clipboardHolderKeys);
  walk(state.layout,item=>{
    if(item.type!=='signal')return;
    if(item.unresolved){
      retained.add(signalKey(item.unresolved));
      item.holder=state.holders.get(item.unresolved)||{signal:item.unresolved,frames:[]};
    }else delete item.holder;
  });
  state.holders.retain(retained);
}
module.exports={MAX_HOLDER_BYTES,WaveformHolderCache,waveformHolderFrame,rememberMissingWaveforms,syncWaveformHolders};
