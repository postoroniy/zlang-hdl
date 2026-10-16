// SPDX-License-Identifier: Apache-2.0
'use strict';
const assert=require('node:assert/strict');
const {test}=require('node:test');
const {EventEmitter}=require('node:events');
const {PassThrough,Writable}=require('node:stream');
const childProcess=require('node:child_process');
const {WaveformQueryClient}=require('../waveform_client');
const {WaveformDocument}=require('../waveform_document');

function fixture(t){
  const child=new EventEmitter(),frames=[];
  child.exitCode=null;child.stdout=new PassThrough();child.stderr=new PassThrough();
  child.stdin=new Writable({write(bytes,_encoding,done){frames.push(JSON.parse(bytes));done();}});
  t.mock.method(childProcess,'spawn',()=>child);
  const client=new WaveformQueryClient('fixture-indexer');
  t.after(()=>{child.stdout.end();child.stderr.end();child.stdin.destroy();});
  const reply=(frame,result)=>child.stdout.write(JSON.stringify({id:frame.id,ok:true,result})+'\n');
  return{client,child,frames,reply};
}

test('close stops admission immediately, sends one terminal frame and finishes stdin',async t=>{
  const {client,child,frames,reply}=fixture(t);
  const query=client.request('value_at',{signals:['q'],timestamp:2});
  const closing=client.close(),again=client.close();
  const late=assert.rejects(client.request('search'),/closed|closing/);
  try{
    assert.equal(closing,again,'all close callers share the same shutdown');
    assert.deepEqual(frames.map(frame=>frame.command),['value_at','close']);
    assert.equal(child.stdin.writableEnded,true,'EOF is sent before the indexer exits');
  }finally{
    for(const frame of frames)reply(frame,frame.command==='value_at'?{value:'1'}:{});
    await Promise.allSettled([query,closing,again,late]);
  }
  assert.deepEqual(await query,{value:'1'});
  assert.equal(client.pending.size,0);
});

test('exit stops writes but the final stdout response is drained before close',async t=>{
  const {client,child,frames,reply}=fixture(t);
  const query=client.request('value_at');
  child.exitCode=0;child.emit('exit',0);
  await assert.rejects(client.request('search'),/closed|exited/);
  assert.equal(frames.length,1);
  reply(frames[0],{value:'u'});child.emit('close',0);
  assert.deepEqual(await query,{value:'u'});
  await client.close();assert.equal(frames.length,1);
});

test('broken stdin rejects pending requests without an uncaught stream error',async t=>{
  const {client,child,frames}=fixture(t);
  const pending=assert.rejects(client.request('open'),/broken pipe/);
  assert.doesNotThrow(()=>child.stdin.emit('error',new Error('broken pipe')));
  await pending;
  await assert.rejects(client.request('search'),/broken pipe|closed/);
  await client.close();assert.equal(frames.length,1);
  assert.equal(client.pending.size,0);
});

test('indexer exit before a terminal reply settles shutdown without late writes',async t=>{
  const {client,child,frames}=fixture(t);
  const pending=assert.rejects(client.request('open'),/exited/);
  const closing=client.close();
  child.exitCode=1;child.emit('exit',1);child.emit('close',1);
  await Promise.all([pending,closing,client.close()]);
  assert.deepEqual(frames.map(frame=>frame.command),['open','close']);
  assert.equal(client.pending.size,0);
});

test('repeated document disposal closes its native trace exactly once',()=>{
  let closes=0,watches=0;
  const document=new WaveformDocument({fsPath:'trace.fst'},
    {close:async()=>{closes++;}},()=>({dispose(){watches++;}}));
  document.dispose();document.dispose();
  assert.equal(closes,1);assert.equal(watches,1);
});
