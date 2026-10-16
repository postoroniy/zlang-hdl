// SPDX-License-Identifier: Apache-2.0
'use strict';
const {WaveformTileCache,waveformQueryKey}=require('./waveform_cache');
const {WaveformQueryQueue}=require('./waveform_client');

// One debugger session owns this status poll. Samples remain in the Rust store
// and use the same bounded query interface as saved waveforms.
class LiveWaveformFeed {
  constructor(post,storage) {
    this.post=post;this.storage=storage;this.session=null;this.sessionId=null;
    this.epoch=null;this.revision=null;this.pending=null;this.updating=null;this.closed=false;
    this.closingSessionIds=new Set();
    this.cache=new WaveformTileCache();
    this.queries=new WaveformQueryQueue((_command,fields)=>this.requireSession().customRequest('zlang/waveformQuery',fields));
  }
  requireSession(){
    if(this.closed||!this.session||this.closingSessionIds.has(this.session.id))
      throw new Error('Start a ZLang HDL debug session first');
    return this.session;
  }
  layoutKey(session=this.requireSession()){
    const config=session.configuration||{};
    return 'waveform.live.layout:'+JSON.stringify([config.program,config.top,config.profile]);
  }
  async persist(layout,sessionId){
    if(sessionId!==this.sessionId||!this.storage)return;
    if(layout?.schema!=='zlang-waveform-session-v1'||Buffer.byteLength(JSON.stringify(layout),'utf8')>1024*1024)
      throw new Error('Invalid or oversized live waveform layout');
    await this.storage.update(this.layoutKey(),layout);
  }
  async request(command,fields,purpose){
    if(this.closingSessionIds.has(this.sessionId))return null;
    this.requireSession();
    const revision=this.revision,sessionId=this.sessionId,key=waveformQueryKey(command,fields);
    const cached=this.cache.get(key);if(cached!==undefined)return cached;
    let result;
    try{result=await this.queries.request(purpose,command,{command,...fields});}
    catch(error){if(this.closed||this.closingSessionIds.has(sessionId)||revision!==this.revision)return null;throw error;}
    if(this.closed||this.closingSessionIds.has(sessionId)||revision!==this.revision)return null;
    if(result!==null)this.cache.set(key,result);
    return result;
  }
  update(session) {
    if(this.closed||session?.type!=='zlang-hdl'||this.closingSessionIds.has(session.id))return Promise.resolve();
    this.pending=session;
    if(!this.updating)this.updating=this.drain().finally(()=>{this.updating=null;});
    return this.updating;
  }
  async drain(){
    try{
      while(this.pending&&!this.closed){
        const session=this.pending;this.pending=null;
        let trace;
        try{trace=await session.customRequest('zlang/waveformStatus');}
        catch(error){if(this.closingSessionIds.has(session.id))continue;throw error;}
        if(this.closed)return;
        if(this.closingSessionIds.has(session.id))continue;
        if(this.pending&&this.pending.id!==session.id)continue;
        if(trace.schema!=='zlang-waveform-trace-v1')throw new Error('Unsupported live waveform schema');
        if(!Number.isSafeInteger(trace.event)||trace.event<0||trace.event>Number.MAX_SAFE_INTEGER/2)
          throw new Error('Live timestamp exceeds the exact integer range supported by this viewer');
        const endTick=trace.end_tick??trace.event*2;
        if(!Number.isSafeInteger(endTick)||endTick<trace.event*2||endTick>trace.event*2+1)
          throw new Error('Invalid live control-phase endpoint');
        const changedSession=this.sessionId!==session.id;
        const reset=changedSession||this.epoch!==trace.epoch;
        const revision=JSON.stringify([session.id,trace.epoch,trace.event,trace.signals]);
        if(this.revision!==revision){this.revision=revision;this.cache.clear();}
        this.session=session;this.sessionId=session.id;this.epoch=trace.epoch;
        await this.post({type:'liveStatus',sessionId:session.id,reset,
          layout:changedSession?this.storage?.get(this.layoutKey(session)):undefined,
          epoch:trace.epoch,event:trace.event*2,endTick,signals:trace.signals,
          timeDomain:{kind:'zlang_event_ticks',ticksPerEvent:2}});
      }
    }catch(error){if(!this.closed)await this.post({type:'reloadFailed',error:'Cannot read live trace: '+error.message});}
  }
  end(sessionId){
    // Gate late events from recently ended sessions without a growing cache.
    this.closingSessionIds.add(sessionId);
    if(this.closingSessionIds.size>32)this.closingSessionIds.delete(this.closingSessionIds.values().next().value);
    if(this.pending?.id===sessionId)this.pending=null;
  }
  dispose(){
    this.closed=true;this.pending=null;this.session=null;this.cache.clear();this.queries.dispose();
    this.closingSessionIds.clear();
  }
}
module.exports={LiveWaveformFeed};
