// SPDX-License-Identifier: Apache-2.0
'use strict';
const assert=require('node:assert/strict');
const {test}=require('node:test');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const {waveformRows,waveformRowWindow,waveformSignalIds}=require('../waveform_rows');
const {WAVEFORM_PALETTE}=require('../waveform_presentation');
const {moveWaveformRows}=require('../waveform_workspace');
const {normalizeWaveformSession,rebindSessionLayout,serializeSessionLayout}=require('../waveform_session');
const {createWaveformChannel}=require('../waveform_channel');
const {waveformTimeScale,waveformMeasurement}=require('../waveform_measurement');
const {WaveformTileCache}=require('../waveform_cache');
const {WaveformQueryQueue,WaveformQueryClient}=require('../waveform_client');
const {niceStep,waveformRulerTicks,valuesFor}=require('../waveform_renderer');
const {WaveformDocument,watchWaveformFile}=require('../waveform_document');
const {WaveformHolderCache,waveformHolderFrame}=require('../waveform_holders');
const {setRange}=require('../waveform_viewport');

test('pan and zoom redraw time only without rebuilding unchanged layout',()=>{
  const calls=[];
  const state={min:0,max:100,viewports:[{start:20,end:40}],syncViewports:true};
  const change=vm.runInNewContext('('+setRange.toString()+')',{
    state,clamp:(v,lo,hi)=>Math.max(lo,Math.min(hi,v)),
    renderAll:()=>calls.push('layout'),renderTimeView:()=>calls.push('time'),
    save:()=>calls.push('save'),scheduleQuery:()=>calls.push('query'),
  });
  change(0,30,50);
  assert.deepEqual(calls,['time','save','query']);
  assert.equal(state.viewports[0].start,30);
  assert.equal(state.viewports[0].end,50);
});

test('opt-in query timings preserve native results and stay outside the wire schema',()=>{
  const samples=[];let value;
  const client=Object.create(WaveformQueryClient.prototype);
  client.onTiming=sample=>samples.push(sample);
  client.pending=new Map([[7,{resolve:result=>{value=result;},timer:null,
    timing:{command:'render_range',started:performance.now(),encodeMs:0.1}}]]);
  const frame=JSON.stringify({id:7,ok:true,result:{bits:'01ux'}});
  client.receive(frame);
  assert.deepEqual(value,{bits:'01ux'});assert.equal(client.pending.size,0);
  assert.equal(samples[0].command,'render_range');
  assert.equal(samples[0].responseBytes,Buffer.byteLength(frame));
  assert.ok(samples[0].decodeMs>=0);assert.ok(samples[0].roundtripMs>=0);
  client.onTiming=null;
  client.pending.set(8,{resolve:result=>{value=result;},timer:null});
  client.receive(JSON.stringify({id:8,ok:true,result:{bits:'uu'}}));
  assert.deepEqual(value,{bits:'uu'});assert.equal(samples.length,1);
});

const catalog=[{id:'s1',name:'Top.q',width:8}];
const session=()=>({schema:'zlang-waveform-session-v1',items:[{type:'signal',id:'row',signal:{name:'Top.q',width:8}}],viewports:[{start:0,end:100}],cursorA:4.2,cursorB:20});
test('portable session rebinding keeps missing signals and exact widths',()=>{
  const missing=normalizeWaveformSession(session(),[],0,100);
  assert.deepEqual(missing.items[0].unresolved,{name:'Top.q',width:8});
  const encoded=serializeSessionLayout(missing.items,new Map());
  assert.deepEqual(encoded[0].signal,{name:'Top.q',width:8});
  assert.equal(rebindSessionLayout(encoded,[{id:'wrong',name:'Top.q',width:9}])[0].signalId,null);
  assert.equal(rebindSessionLayout(encoded,catalog)[0].signalId,'s1');
});
test('copies of one signal retain independent row identity and presentation',()=>{
  const data=session();
  data.items[0].color='#77d84b';data.items[0].height=34;data.items[0].format='unsigned';
  data.items.push({...data.items[0],id:'copy',color:'#ff6b6b',height:70,format:'hex'});
  data.selectedRow='copy';data.selectedRows=['copy'];
  const rebound=normalizeWaveformSession(data,catalog,0,100);
  assert.deepEqual(rebound.items.map(row=>row.signalId),['s1','s1']);
  assert.equal(rebound.selectedRowId,'copy');assert.deepEqual(rebound.selectedRowIds,['copy']);
  const encoded=serializeSessionLayout(rebound.items,new Map(catalog.map(signal=>[signal.id,signal])));
  assert.deepEqual(encoded,data.items.map(row=>({...row,alias:undefined,mnemonic:Object.create(null)})));
  assert.deepEqual(waveformSignalIds(rebound.items),['s1']);
  const legacy=normalizeWaveformSession({...data,selectedRow:undefined,selectedRows:undefined,
    selectedSignal:{name:'Top.q',width:8}},catalog,0,100);
  assert.equal(legacy.selectedRowId,'row');
});
test('signal row cap counts copies and malformed selections fail closed',()=>{
  const data=session();
  data.items=Array.from({length:4096},(_,index)=>({...data.items[0],id:'row'+index}));
  assert.equal(normalizeWaveformSession(data,catalog,0,100).items.length,4096);
  data.items.push({...data.items[0],id:'extra'});
  assert.throws(()=>normalizeWaveformSession(data,catalog,0,100),/4096 signal rows/);
  for(const selectedRows of [false,[1],Array(8193).fill('row')])
    assert.throws(()=>normalizeWaveformSession({...session(),selectedRows},catalog,0,100),/row selection/);
});
test('appearance palette contains sixteen distinct named safe colors',()=>{
  assert.equal(WAVEFORM_PALETTE.length,16);
  assert.equal(new Set(WAVEFORM_PALETTE.map(([name])=>name)).size,16);
  assert.equal(new Set(WAVEFORM_PALETTE.map(([,color])=>color)).size,16);
  assert.ok(WAVEFORM_PALETTE.every(([,color])=>/^#[a-f0-9]{6}$/.test(color)));
});
test('hostile and malformed sessions are rejected before application',()=>{
  for(const mutate of [s=>s.viewports[0].end=NaN,s=>s.cursorA=Infinity,
    s=>s.items.push(s.items[0]),s=>s.items[0].color='url(http://hostile)',
    s=>s.items[0].mnemonic={'__proto__':{},'1':5},s=>s.schema='unknown',
    s=>s.bookmarks=Array(257).fill({time:0})]){
    const data=session();mutate(data);assert.throws(()=>normalizeWaveformSession(data,catalog,0,100));
  }
});
test('one row geometry covers nested groups, dividers and variable heights',()=>{
  const geometry=waveformRows([{type:'group',id:'g',children:[{type:'signal',id:'s',height:60}]},{type:'divider',id:'d'}]);
  assert.deepEqual(geometry.rows.map(row=>[row.item.id,row.top,row.height]),[['g',0,28],['s',28,60],['d',88,24]]);
  assert.equal(waveformRowWindow(geometry.rows,30,10,0)[0].item.id,'s');
  assert.equal(waveformRows([{type:'group',collapsed:true,children:[{type:'signal'}]}]).rows.length,1);
  const large=waveformRows(Array.from({length:4096},(_,id)=>({type:'signal',id})));
  assert.ok(waveformRowWindow(large.rows,50000,800).length<32);
});
test('event tick measurements never produce a physical frequency',()=>{
  const stats={width:1,start_bits:'0',end_bits:'1',transitions:3,rising:2,falling:1,high_ticks:4,low_ticks:4,unknown_ticks:0};
  const native=waveformMeasurement(stats,0,8,{time_domain:'zlang_event_ticks',ticks_per_event:2,timescale:'1 ns'});
  assert.equal(native.eventDelta,4);assert.equal(native.reciprocalHz,null);assert.equal(native.dutyCycle,0.5);
  assert.equal(waveformTimeScale({time_domain:'physical',timescale:'10 PicoSeconds'}),1e-11);
  assert.equal(waveformMeasurement({...stats,unknown_ticks:1},0,8,{time_domain:'physical'}).dutyCycle,null);
});
test('large integer timestamps and split live viewport data stay independent',()=>{
  const ticks=vm.runInNewContext('('+waveformRulerTicks.toString()+')({start:Number.MAX_SAFE_INTEGER-2,end:Number.MAX_SAFE_INTEGER})',{niceStep});
  assert.deepEqual(Array.from(ticks),[Number.MAX_SAFE_INTEGER-2,Number.MAX_SAFE_INTEGER-1,Number.MAX_SAFE_INTEGER]);
  const state={live:true,data:new Map([[0,new Map([['q',[0]]])],[1,new Map([['q',[1]]])]])};
  assert.deepEqual(vm.runInNewContext('('+valuesFor.toString()+')(1,"q")',{state}),[1]);
});
test('moving groups rejects cycles and multi-row moves retain order',()=>{
  const layout=[{id:'a',type:'signal'},{id:'g',type:'group',children:[{id:'b',type:'signal'}]},{id:'c',type:'signal'}];
  const original=JSON.stringify(layout);assert.equal(moveWaveformRows(layout,['g'],'b'),false);
  assert.equal(JSON.stringify(layout),original);
  assert.equal(moveWaveformRows(layout,['a','c'],'g'),true);
  assert.deepEqual(layout[0].children.map(item=>item.id),['b','a','c']);
  const nested={id:'deep7',type:'group',children:[]};let root=nested;
  for(let depth=6;depth>=0;depth--)root={id:'deep'+depth,type:'group',children:[root]};
  const bounded=[root,{id:'extra',type:'group',children:[]}];
  const before=JSON.stringify(bounded);
  assert.equal(moveWaveformRows(bounded,['extra'],'deep7'),false);
  assert.equal(JSON.stringify(bounded),before);
});
test('webview transport waits for ready and rejects pending controls on close',async()=>{
  let receive;const sent=[];
  const channel=createWaveformChannel({onDidReceiveMessage:fn=>{receive=fn;return{dispose(){}};},postMessage:message=>{sent.push(message);return Promise.resolve(true);}});
  await channel.post({type:'catalog'});
  const response=channel.request({command:'export_session'});
  assert.equal(sent.length,0);receive({type:'ready'});
  assert.equal(sent[0].type,'catalog');assert.equal(sent[1].type,'control');
  receive({type:'controlResult',requestId:sent[1].requestId,result:{ok:true}});
  assert.deepEqual(await response,{ok:true});
  const closed=channel.request({command:'get_item_list'});channel.dispose();
  await assert.rejects(closed,/closed/);
});
test('tile cache enforces byte bound and access-based eviction',()=>{
  const cache=new WaveformTileCache(80);cache.set('a',{v:'aaaa'});cache.set('b',{v:'bbbb'});
  cache.get('a');cache.set('c',{v:'cccccccccccccccccccccccccccccccccccccccc'});
  assert.ok(cache.bytes<=80);
  cache.set('x'.repeat(1000),null);assert.equal(cache.entries.has('x'.repeat(1000)),false);
});
test('rapid viewport requests retain only the latest waiting tile',async()=>{
  const sent=[];let finish;
  const queue=new WaveformQueryQueue(async(_command,fields)=>{
    sent.push(fields.tick);
    if(fields.tick===0)await new Promise(resolve=>{finish=resolve;});
    return fields.tick;
  });
  const first=queue.request('viewport','render_range',{tick:0});
  const superseded=queue.request('viewport','render_range',{tick:1});
  const latest=queue.request('viewport','render_range',{tick:2});
  assert.equal(await superseded,null);assert.deepEqual(sent,[0]);
  finish();assert.equal(await first,0);assert.equal(await latest,2);
  assert.deepEqual(sent,[0,2]);queue.dispose();
  await assert.rejects(queue.request('viewport','render_range',{}),/closed/);
});
test('file watcher detects writes and replacements only for its own trace and disposes',()=>{
  const handlers=new Map();let changes=0,disposed=0;
  const root=fs.mkdtempSync(path.join(__dirname,'../../../../build/tmp/waveform-watch.'));
  const file=path.join(root,'trace.fst');fs.writeFileSync(file,'original');
  const uri={fsPath:file,toString:()=>file};
  const watcher={dispose(){disposed++;}};
  for(const event of ['Change','Create','Delete'])watcher['onDid'+event]=callback=>{
    handlers.set(event,callback);return{dispose(){disposed++;}};
  };
  const vscode={RelativePattern:class{constructor(base,pattern){assert.equal(base,root);assert.equal(pattern,'*');}},
    workspace:{createFileSystemWatcher:()=>watcher}};
  const subscription=watchWaveformFile(vscode,uri,()=>changes++);
  try{
    handlers.get('Create')(uri);assert.equal(changes,0,'late creation of an already indexed file is not a change');
    fs.writeFileSync(file,'updated');handlers.get('Change')({toString:()=>'/workspace/other.fst'});
    assert.equal(changes,0);handlers.get('Change')(uri);assert.equal(changes,1);
    handlers.get('Change')(uri);assert.equal(changes,1,'duplicate notification is ignored');
    fs.unlinkSync(file);handlers.get('Delete')(uri);assert.equal(changes,2);
    fs.writeFileSync(file,'recreated');handlers.get('Create')(uri);assert.equal(changes,3);
    fs.writeFileSync(file,'indexed before notification');subscription.checkpoint();
    handlers.get('Change')(uri);assert.equal(changes,3,'an indexed version is acknowledged');
  }finally{subscription.dispose();fs.rmSync(root,{recursive:true});}
  assert.equal(disposed,4);
});
test('reload replaces one document generation for all views and rejects stale cache results',async()=>{
  let index=0,complete,watch,closed=false;const messages=[],other=[];
  const client={request:async command=>{
    if(command==='open')return{index:++index};
    return new Promise(resolve=>{complete=resolve;});
  },close:async()=>{closed=true;}};
  const doc=new WaveformDocument({fsPath:'/workspace/trace.fst'},client,callback=>{
    watch=callback;return{dispose(){watch=null;}};
  });
  doc.subscribe(message=>messages.push(message));doc.subscribe(message=>other.push(message));
  assert.deepEqual(await doc.open(),{index:1});
  const old=doc.cachedRequest('value_at',{},'cursor');watch();assert.equal(doc.changed,true);
  assert.equal(await doc.cachedRequest('value_at',{timestamp:99},'blocked'),null,
    'a changed file must not mix partially written data into the previous view');
  const reload=doc.reload();assert.equal(doc.reload(),reload);await reload;
  complete({stale:true});assert.equal(await old,null);assert.equal(doc.cache.bytes,0);
  assert.equal(doc.changed,false);assert.deepEqual(messages,other);
  assert.deepEqual(messages.map(message=>message.type),['fileChanged','reloadStarted','catalog']);
  assert.equal(messages.at(-1).preserveLayout,true);assert.equal(messages.at(-1).fileChanged,false);
  doc.dispose();assert.equal(watch,null);assert.equal(closed,true);
});
test('failed or concurrent-write reload remains retryable without claiming a fresh trace',async()=>{
  let finish;const notices=[];
  const client={request:async()=>new Promise((resolve,reject)=>{finish={resolve,reject};}),close:async()=>{}};
  const doc=new WaveformDocument({fsPath:'/workspace/trace.vcd'},client);
  doc.subscribe(message=>notices.push(message));
  const first=doc.open();finish.resolve({old:true});await first;
  doc.markChanged();const failed=doc.reload();finish.reject(new Error('incomplete file'));
  await assert.rejects(failed,/incomplete/);assert.deepEqual(doc.catalog,{old:true});assert.equal(doc.changed,true);
  assert.equal(notices.at(-1).type,'reloadFailed');assert.equal(doc.loading,null);
  const retry=doc.reload();doc.markChanged();finish.resolve({new:true});await retry;
  assert.equal(doc.changed,true);assert.equal(notices.at(-1).fileChanged,true);
  const final=doc.reload();finish.resolve({new:true});await final;assert.equal(doc.changed,false);doc.dispose();
});
test('metadata changes during indexing remain stale even before a watcher notification arrives',async()=>{
  let stamp='old',finish;
  const client={request:async()=>new Promise(resolve=>{finish=resolve;}),close:async()=>{}};
  const doc=new WaveformDocument({fsPath:'/workspace/trace.fst'},client,()=>({checkpoint:()=>stamp,dispose(){}}));
  const first=doc.open();stamp='new';finish({index:1});await first;
  assert.equal(doc.changed,true);
  const reload=doc.reload();finish({index:2});await reload;assert.equal(doc.changed,false);doc.dispose();
});
test('ready transport retains the latest catalog and subsequent change notice in order',async()=>{
  let receive;const sent=[];
  const channel=createWaveformChannel({onDidReceiveMessage:fn=>{receive=fn;return{dispose(){}};},postMessage:message=>{sent.push(message);return Promise.resolve(true);}});
  await channel.post({type:'catalog',revision:1});await channel.post({type:'fileChanged'});
  await channel.post({type:'catalog',revision:2});receive({type:'ready'});
  assert.deepEqual(sent,[{type:'fileChanged'},{type:'catalog',revision:2}]);channel.dispose();
});
test('holder snapshots are bounded, exact-width keyed, removable and range-limited',()=>{
  const cache=new WaveformHolderCache(1000);
  const frame={start:0,end:20,rendered:{exact:true,points:[]},values:[]};
  assert.equal(cache.remember(catalog[0],[frame]),true);
  assert.equal(cache.get({...catalog[0],width:9}),undefined);
  assert.equal(waveformHolderFrame(cache.get(catalog[0]),{start:21,end:30}),null);
  assert.deepEqual(waveformHolderFrame(cache.get(catalog[0]),{start:5,end:10}),frame);
  const encoded=serializeSessionLayout([{id:'holder',type:'signal',unresolved:catalog[0],holder:cache.get(catalog[0])}],new Map());
  assert.equal(Object.hasOwn(encoded[0],'holder'),false,'sessions must not embed waveform samples');
  assert.equal(cache.remember(catalog[0],[{...frame,values:['x'.repeat(1000)]}]),false);
  assert.ok(cache.bytes<=1000);cache.retain(new Set());assert.equal(cache.bytes,0);assert.equal(cache.entries.size,0);
});
