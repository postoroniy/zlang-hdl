// SPDX-License-Identifier: Apache-2.0
'use strict';
const assert=require('node:assert/strict');
const {once}=require('node:events');
const {WaveformQueryClient}=require('../waveform_client');

// Exercise the reviewed native executable, not just an in-memory pipe double.
async function checkNativeTraceShutdown(executable,trace){
  let signals=0;
  const observed=()=>{signals++;};
  process.on('SIGPIPE',observed);
  try{
    for(let iteration=0;iteration<32;iteration++){
      const client=new WaveformQueryClient(executable);
      const exited=once(client.process,'close');
      try{
        const open=client.request('open',{path:trace});
        const closing=client.close();
        assert.equal(client.close(),closing);
        await assert.rejects(client.request('capabilities'),/closed/);
        assert.ok((await open).signals.length>0,'in-flight response must not be lost');
        await closing;await exited;
        assert.equal(client.pending.size,0);
        await assert.rejects(client.request('capabilities'),/closed|exited/);
      }finally{await client.close();}
    }
    assert.equal(signals,0,'native trace shutdown must not emit SIGPIPE');
  }finally{process.removeListener('SIGPIPE',observed);}
}
module.exports={checkNativeTraceShutdown};
