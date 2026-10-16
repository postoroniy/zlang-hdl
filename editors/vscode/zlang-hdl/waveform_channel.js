// SPDX-License-Identifier: Apache-2.0
'use strict';

// Owns readiness, request deadlines and disposal for one webview transport.
function createWaveformChannel(webview, receive) {
  let ready=false,closed=false,sequence=0;
  const pending=new Map(),waiting=[],latest=new Map();
  const subscription=webview.onDidReceiveMessage(message=>{
    if(message?.type==='ready') {
      ready=true;
      for(const payload of latest.values())void webview.postMessage(payload);
      latest.clear();
      for(const send of waiting.splice(0))send();
    } else if(message?.type==='controlResult') {
      const request=pending.get(message.requestId);
      if(!request)return;
      pending.delete(message.requestId);clearTimeout(request.timer);
      if(message.error)request.reject(new Error(message.error));
      else request.resolve(message.result);
    } else if(!closed)void receive?.(message);
  });
  return {
    post(payload) {
      if(closed)return Promise.resolve(false);
      if(!ready){
        const previous=latest.get(payload.type);
        // A later status poll may arrive before the new webview says ready.
        // Keep its one-time recovery layout, but never across debug sessions.
        if(payload.type==='liveStatus'&&previous?.sessionId===payload.sessionId
          &&payload.layout===undefined&&previous?.layout!==undefined)
          payload={...payload,layout:previous.layout};
        latest.delete(payload.type);latest.set(payload.type,payload);return Promise.resolve(false);
      }
      return webview.postMessage(payload);
    },
    request(command) {
      if(closed)return Promise.reject(new Error('Waveform editor was closed'));
      if(pending.size>=64)return Promise.reject(new Error('Too many waveform control requests'));
      const requestId='control-'+(++sequence);
      return new Promise((resolve,reject)=>{
        const timer=setTimeout(()=>{
          pending.delete(requestId);reject(new Error('Waveform editor did not respond'));
        },30000);
        pending.set(requestId,{resolve,reject,timer});
        const send=()=>{if(pending.has(requestId))void webview.postMessage({type:'control',requestId,command});};
        if(ready)send();else waiting.push(send);
      });
    },
    dispose() {
      closed=true;subscription.dispose();waiting.length=0;latest.clear();
      for(const request of pending.values()){
        clearTimeout(request.timer);request.reject(new Error('Waveform editor was closed'));
      }
      pending.clear();
    },
  };
}
module.exports={createWaveformChannel};
