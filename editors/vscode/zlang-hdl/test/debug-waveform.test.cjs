// SPDX-License-Identifier: Apache-2.0
'use strict';
const {LiveWaveformFeed}=require('../waveform_live');
const {createWaveformChannel}=require('../waveform_channel');
const {sessionFile,sessionSource,saveSession,readSession,exportLiveTrace}=require('../waveform_session_io');
const assert=require('node:assert/strict');
const {test}=require('node:test');
const {addVariableToWaveform}=require('../debug_waveform');

test('Variables and Watch forward the exact DAP binding and owning session',async()=>{
  const requests=[];let opens=0;
  const session={id:'session',type:'zlang-hdl',customRequest:async(...args)=>requests.push(args)};
  const vscode={debug:{activeDebugSession:session}};
  const context={};
  const open=async actual=>{assert.equal(actual,context);opens++;};
  await addVariableToWaveform(vscode,context,open,{
    sessionId:'session',variable:{name:'value',evaluateName:'rx.payload.value'},
  });
  await addVariableToWaveform(vscode,context,open,{sessionId:'session',expression:'message.Data.value'});
  assert.deepEqual(requests,[
    ['zlang/addTraceSignals',{signals:['rx.payload.value']}],
    ['zlang/addTraceSignals',{signals:['message.Data.value']}],
  ]);
  assert.equal(opens,2);
  await assert.rejects(addVariableToWaveform(vscode,context,open,{sessionId:'other',variable:{evaluateName:'x'}}),/active paused/);
  await assert.rejects(addVariableToWaveform(vscode,context,open,{variable:{name:'no binding'}}),/no compiler-owned/);
  assert.equal(requests.length,2);
});

test('rejected selections do not open or alter the live waveform',async()=>{
  const vscode={debug:{activeDebugSession:{id:'s',type:'zlang-hdl',customRequest:async()=>{throw Error('selection exceeds limit');}}}};
  await assert.rejects(addVariableToWaveform(vscode,{},()=>assert.fail('must not open'),{variable:{evaluateName:'huge'}}),/exceeds limit/);
});

test('live queries share a bounded cache and restore design-scoped layouts',async()=>{
  const messages=[],stored=new Map();let queries=0,event=1;
  const session={id:'s',type:'zlang-hdl',configuration:{program:'/workspace/a.zhl',top:'Top'},
    customRequest:async command=>{
      if(command==='zlang/waveformStatus')return{schema:'zlang-waveform-trace-v1',epoch:0,event,signals:[]};
      queries++;return{values:{q:'1'}};
    }};
  const storage={get:key=>stored.get(key),update:async(key,value)=>stored.set(key,value)};
  const feed=new LiveWaveformFeed(message=>messages.push(message),storage);
  await feed.update(session);
  const fields={signals:['q'],timestamp:2};
  await feed.request('value_at',fields,'a');await feed.request('value_at',fields,'b');
  assert.equal(queries,1);
  const layout={schema:'zlang-waveform-session-v1',items:[]};
  await feed.persist(layout,'old');assert.equal(stored.size,0);
  await feed.persist(layout,'s');assert.equal(stored.size,1);
  event=2;await feed.update(session);await feed.request('value_at',fields,'a');
  assert.equal(queries,2);assert.ok(feed.cache.bytes<=128*1024*1024);
  feed.dispose();
  const reopened=new LiveWaveformFeed(message=>messages.push(message),storage);
  await reopened.update({...session,id:'reopened'});
  assert.deepEqual(messages.at(-1).layout,layout);
  await reopened.update({...session,id:'other-design',configuration:{program:'/workspace/b.zhl',top:'Top'}});
  assert.equal(messages.at(-1).layout,undefined);
  await assert.rejects(reopened.persist({...layout,payload:'x'.repeat(1024*1024)},'other-design'),/oversized/);
  reopened.dispose();await assert.rejects(reopened.request('value_at',fields,'a'),/Start/);
});

test('pre-ready live polls retain the recovery layout only for their own session',async()=>{
  let receive;const sent=[];
  const channel=createWaveformChannel({onDidReceiveMessage:fn=>{receive=fn;return{dispose(){}};},
    postMessage:async message=>sent.push(message)});
  const layout={schema:'zlang-waveform-session-v1',items:[]};
  await channel.post({type:'liveStatus',sessionId:'s',event:0,layout});
  await channel.post({type:'liveStatus',sessionId:'s',event:2});
  receive({type:'ready'});
  assert.deepEqual(sent[0].layout,layout);assert.equal(sent[0].event,2);
  channel.dispose();
  const fresh=createWaveformChannel({onDidReceiveMessage:fn=>{receive=fn;return{dispose(){}};},
    postMessage:async message=>sent.push(message)});
  await fresh.post({type:'liveStatus',sessionId:'old',layout});
  await fresh.post({type:'liveStatus',sessionId:'new',event:0});
  receive({type:'ready'});
  assert.equal(sent.at(-1).layout,undefined);fresh.dispose();
});

test('live session replacement suppresses late status and query results',async()=>{
  const messages=[];let releaseStatus,releaseQuery;
  const first={id:'first',type:'zlang-hdl',configuration:{program:'a',top:'A'},customRequest:async command=>
    command==='zlang/waveformStatus'?await new Promise(resolve=>{releaseStatus=resolve;})
      :await new Promise(resolve=>{releaseQuery=resolve;})};
  const trace={schema:'zlang-waveform-trace-v1',epoch:0,event:1,signals:[]};
  const second={id:'second',type:'zlang-hdl',configuration:{program:'b',top:'B'},customRequest:async()=>trace};
  const feed=new LiveWaveformFeed(message=>messages.push(message));
  const pending=feed.update(first);const replaced=feed.update(second);releaseStatus(trace);
  await Promise.all([pending,replaced]);
  assert.deepEqual(messages.map(message=>message.sessionId),['second']);
  await feed.update({...first,customRequest:async command=>command==='zlang/waveformStatus'?trace
    :await new Promise(resolve=>{releaseQuery=resolve;})});
  const query=feed.request('value_at',{signals:['q'],timestamp:2},'cursor');
  await feed.update(second);releaseQuery({values:{q:'stale'}});
  assert.equal(await query,null);assert.equal(feed.cache.bytes,0);
  feed.dispose();
});

test('live session templates are explicit and saved trace paths stay portable',()=>{
  const layout={schema:'zlang-waveform-session-v1',items:[],viewports:[{start:0,end:2}],cursorA:0};
  const live=JSON.parse(sessionFile(layout,'/workspace/live.zvw.json'));
  assert.equal(sessionSource(live),'live');assert.equal(live.trace,undefined);
  const saved=JSON.parse(sessionFile(layout,'/workspace/view.zvw.json','/workspace/build/counter.fst'));
  assert.equal(saved.trace,'build/counter.fst');assert.equal(sessionSource(saved),'file');
  for(const bad of [{...live,trace:'x.fst'},{...saved,trace:'x.ghw'},
    {...saved,source:{kind:'remote'}},{...saved,trace:''},{schema:'unknown'}])
    assert.throws(()=>sessionSource(bad));
  assert.throws(()=>sessionFile({...layout,payload:'x'.repeat(1024*1024)},'/workspace/a.zvw.json'),/1 MiB/);
});

test('live control tail extends the visible range without advancing the event marker',async()=>{
  const messages=[];
  const feed=new LiveWaveformFeed(message=>messages.push(message));
  const trace={schema:'zlang-waveform-trace-v1',epoch:0,event:5,end_tick:11,signals:[]};
  const session={id:'s',type:'zlang-hdl',customRequest:async()=>trace};
  await feed.update(session);
  assert.equal(messages[0].event,10);assert.equal(messages[0].endTick,11);
  for(const bad of [9,12,10.5,Number.MAX_SAFE_INTEGER+1]){
    trace.end_tick=bad;await feed.update(session);
    assert.equal(messages.at(-1).type,'reloadFailed');
    assert.match(messages.at(-1).error,/endpoint/);
  }
  feed.dispose();
});

test('DAP shutdown prevents late live RPCs and drops pending poll/query responses',async()=>{
  const messages=[];let requests=0,rejectQuery,resolvePoll;
  const trace={schema:'zlang-waveform-trace-v1',epoch:0,event:1,signals:[]};
  const session={id:'s',type:'zlang-hdl',customRequest:async command=>{
    requests++;
    if(command==='zlang/waveformStatus')return trace;
    return new Promise((_resolve,reject)=>{rejectQuery=reject;});
  }};
  const feed=new LiveWaveformFeed(message=>messages.push(message));
  await feed.update(session);
  const query=feed.request('value_at',{signals:['q'],timestamp:2},'cursor');
  const polling=feed.update({...session,customRequest:()=>new Promise(resolve=>{resolvePoll=resolve;})});
  feed.end('s');rejectQuery(new Error('disconnected'));resolvePoll(trace);
  assert.equal(await query,null);await polling;
  await feed.update(session);assert.equal(await feed.request('value_at',{},'cursor'),null);
  assert.equal(requests,2);assert.equal(messages.length,1);
  await feed.update({...session,id:'new',customRequest:async()=>trace});
  assert.equal(messages.at(-1).sessionId,'new');feed.dispose();
});

test('session dialogs support both sources and trace export never advances simulation',async()=>{
  const layout={schema:'zlang-waveform-session-v1',items:[],viewports:[{start:0,end:2}],cursorA:0};
  const calls=[],files=new Map();let target='/workspace/live.zvw.json';
  const uri=file=>({fsPath:file});
  const vscode={Uri:{file:uri},window:{showSaveDialog:async()=>uri(target),showOpenDialog:async()=>[uri(target)]},
    workspace:{workspaceFolders:[{uri:uri('/workspace')}],fs:{
      writeFile:async(file,bytes)=>files.set(file.fsPath,bytes),
      stat:async file=>({size:files.get(file.fsPath).byteLength}),readFile:async file=>files.get(file.fsPath),
      createDirectory:async file=>calls.push(['mkdir',file.fsPath]),
    }}};
  const channel={request:async command=>{assert.equal(command.command,'export_session');return{session:layout};}};
  await saveSession(vscode,channel);assert.equal((await readSession(vscode)).kind,'live');
  target='/workspace/view.zvw.json';await saveSession(vscode,channel,uri('/workspace/build/trace.vcd'));
  assert.equal((await readSession(vscode)).kind,'file');
  const session={type:'zlang-hdl',customRequest:async(...args)=>calls.push(args)};
  target='/workspace/build/live.fst';await exportLiveTrace(vscode,session);
  target='/workspace/build/live.vcd';await exportLiveTrace(vscode,session);
  assert.deepEqual(calls.filter(call=>call[0]!=='mkdir').map(call=>call[0]),['zlang/exportFst','zlang/exportVcd']);
  target='/workspace/build/live.ghw';await assert.rejects(exportLiveTrace(vscode,session),/fst or .vcd/);
});
