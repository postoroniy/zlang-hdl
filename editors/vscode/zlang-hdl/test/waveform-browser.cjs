// SPDX-License-Identifier: Apache-2.0
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {pathToFileURL}=require('node:url');
const {chromium}=require('playwright');
const {buildWaveformBundle}=require('./waveform-assets.cjs');
const {waveformHtml}=require('../waveform_view');
const {WaveformQueryClient}=require('../waveform_client');
const {WaveformTileCache,waveformQueryKey}=require('../waveform_cache');
const {writeScaleVcd}=require('./waveform-fixtures.cjs');
const {checkContinuousPan,checkDensePan}=require('./waveform-pan.cjs');
const {checkTypedLiveBrowser}=require('./typed-waveform-browser.cjs');
const {checkNativeTraceShutdown}=require('./waveform-client-native.cjs');

async function main(){
  const repository=path.resolve(__dirname,'../../../..');
  const root=fs.mkdtempSync(path.join(repository,'build/tmp/waveform-browser.'));
  const executable=path.resolve(process.env.ZLANG_WAVEFORM_INDEXER||path.join(repository,'native-runtime/target/debug/zlang-waveform-indexer'));
  const client=new WaveformQueryClient(executable);
  let browser,page;const errors=[];
  try{
    const oversized=path.join(root,'oversized.vcd');
    const descriptor=fs.openSync(oversized,'wx');fs.ftruncateSync(descriptor,1024**3+1);fs.closeSync(descriptor);
    await assert.rejects(client.request('open',{path:oversized}),/byte limit/);
    const malformed=path.join(root,'malformed.vcd');fs.writeFileSync(malformed,'not a waveform');
    await assert.rejects(client.request('open',{path:malformed}));
    const vcd=path.join(root,'counter.vcd');
    const lines=['$version ZLang HDL native waveform $end','$timescale 1ns $end','$scope module Counter $end','$var wire 1 ! clk $end','$var wire 8 " q $end','$var wire 1 # rst $end','$var wire 1 $ unknown $end','$scope module __zlang_meta $end','$var wire 1 % unknown_u_mask $end','$upscope $end','$upscope $end','$enddefinitions $end'];
    for(let time=0;time<=128;time++){
      lines.push('#'+time,(time%2)+'!');
      if(time%4===0)lines.push('b'+(time/4).toString(2).padStart(8,'0')+' "');
      if(time===0)lines.push('1#','x$','1%');if(time===2)lines.push('0#');
      if(time===8)lines.push('0%');if(time===16)lines.push('z$');
    }
    fs.writeFileSync(vcd,lines.join('\n')+'\n');
    await checkNativeTraceShutdown(executable,vcd);
    const cliVcd=path.join(root,'cli-native.vcd');
    const cliLines=lines.join('\n').replace('ZLang HDL native waveform','ZLang HDL simulation')
      .replace('$scope module __zlang_meta $end','$upscope $end\n$scope module __zlang_meta $end')
      .replace('$upscope $end\n$upscope $end\n$enddefinitions $end','$upscope $end\n$enddefinitions $end');
    fs.writeFileSync(cliVcd,cliLines+'\n');
    const cliCatalog=await client.request('open',{path:cliVcd});
    assert.equal(cliCatalog.metadata.time_domain,'zlang_event_ticks');
    assert.equal(cliCatalog.metadata.ticks_per_event,2);
    assert.equal(cliCatalog.signals.length,4);
    const cliUnknown=cliCatalog.signals.find(signal=>signal.name==='Counter.unknown');
    assert.equal((await client.request('value_at',{signals:[cliUnknown.id],timestamp:0})).values[cliUnknown.id],'u');
    assert.equal((await client.request('value_at',{signals:[cliUnknown.id],timestamp:8})).values[cliUnknown.id],'x');
    const catalog=await client.request('open',{path:vcd});
    assert.equal(catalog.metadata.schema,'zlang-waveform-query-v3');
    const html=path.join(root,'viewer.html');fs.writeFileSync(html,waveformHtml(buildWaveformBundle().toString()));
    browser=await chromium.launch({headless:true});
    page=await browser.newPage({viewport:{width:1640,height:920},colorScheme:'dark'});
    page.on('pageerror',error=>errors.push(error.message));
    const controls=new Map();let sequence=0;const requests=[];let pendingQueries=0,cacheHits=0;
    let clipboardText='',failClipboardWrite=false,renderDelay=0;
    const cache=new WaveformTileCache();
    await page.exposeFunction('waveformHost',async message=>{
      if(message.type==='controlResult'){
        const pending=controls.get(message.requestId);controls.delete(message.requestId);
        if(message.error)pending?.reject(new Error(message.error));else pending?.resolve(message.result);return;
      }
      if(message.type==='ready')return;
      if(message.type==='clipboard'){
        if(failClipboardWrite&&message.action==='write'){
          await page.evaluate(requestId=>window.postMessage({type:'queryError',requestId,error:'Clipboard write denied'},'*'),message.requestId);return;
        }
        if(message.action==='write')clipboardText=message.text;
        await page.evaluate(payload=>window.postMessage(payload,'*'),{type:'queryResult',requestId:message.requestId,
          result:message.action==='write'?{written:true}:{text:clipboardText}});return;
      }
      if(message.type==='reload'){
        await page.evaluate(()=>window.postMessage({type:'reloadStarted'},'*'));
        cache.clear();
        try{
          const catalog=await client.request('open',{path:vcd});
          await page.evaluate(catalog=>window.postMessage({type:'catalog',catalog,preserveLayout:true},'*'),catalog);
        }catch(error){await page.evaluate(error=>window.postMessage({type:'reloadFailed',error},'*'),error.message);}
        return;
      }
      const commands={renderRange:'render_range',valueAt:'value_at',statistics:'statistics',search:'search'};
      if(!commands[message.type])return;
      pendingQueries++;
      const ids=message.signals||message.query?.signals||[];
      const request={type:message.type,count:ids.length,unique:new Set(ids).size===ids.length,
        overview:Boolean(message.overview),viewport:message.viewport,
        start:message.start,end:message.end,completed:false};
      requests.push(request);
      const {type,requestId,...fields}=message;
      try{
        if(type==='renderRange'&&!message.overview&&renderDelay)
          await new Promise(resolve=>setTimeout(resolve,renderDelay));
        const key=waveformQueryKey(commands[type],fields);let result=cache.get(key);
        if(result===undefined){result=await client.request(commands[type],fields);cache.set(key,result);}else cacheHits++;
        await page.evaluate(payload=>window.postMessage(payload,'*'),{type:'queryResult',requestId,result});
      }
      catch(error){await page.evaluate(payload=>window.postMessage(payload,'*'),{type:'queryError',requestId,error:error.message});}
      finally{pendingQueries--;request.completed=true;}
    });
    await page.addInitScript(()=>{window.acquireVsCodeApi=()=>({postMessage:message=>window.waveformHost(message)});});
    await page.goto(pathToFileURL(html).href);
    const control=command=>new Promise((resolve,reject)=>{
      const requestId='test-'+(++sequence);controls.set(requestId,{resolve,reject});
      page.evaluate(payload=>window.postMessage(payload,'*'),{type:'control',requestId,command}).catch(reject);
      setTimeout(()=>{if(controls.delete(requestId))reject(new Error('Control timed out: '+command.command));},10000).unref();
    });
    await page.evaluate(catalog=>window.postMessage({type:'catalog',catalog},'*'),catalog);
    await page.waitForFunction(()=>document.querySelectorAll('.displayRow').length===4);
    const q=catalog.signals.find(signal=>signal.name==='Counter.q');
    await control({command:'set_selection',signals:[q.id]});
    await control({command:'set_viewport_range',start:0,end:5});
    await control({command:'set_cursor',timestamp:4.2});
    await page.locator('#next').click();
    await page.waitForFunction(()=>document.querySelector('#cursorReadout').textContent.endsWith('8'));
    assert.equal((await control({command:'export_session'})).session.cursorA,8);
    await page.locator('#fit').click();
    await page.keyboard.press('ArrowLeft');
    await page.waitForFunction(()=>document.querySelector('#cursorReadout').textContent.endsWith('4'));
    const clockSignal=catalog.signals.find(signal=>signal.name==='Counter.clk');
    await control({command:'set_selection',signals:[clockSignal.id]});
    await control({command:'set_cursor',timestamp:4});
    await page.keyboard.press('Shift+ArrowRight');
    await page.waitForFunction(()=>document.querySelector('#cursorReadout').textContent.endsWith('5'));
    await page.keyboard.press('Control+ArrowRight');
    await page.waitForFunction(()=>document.querySelector('#cursorReadout').textContent.endsWith('6'));
    const unknownSignal=catalog.signals.find(signal=>signal.name==='Counter.unknown');
    await control({command:'set_selection',signals:[unknownSignal.id]});
    await control({command:'set_cursor',timestamp:0});
    await page.locator('#searchKind').selectOption('unknown_entry');
    await page.locator('#searchNext').click();
    await page.waitForFunction(()=>document.querySelector('#cursorReadout').textContent.endsWith('8'));
    await page.locator('#searchKind').selectOption('uninitialized_entry');
    await page.locator('#searchPrev').click();
    await page.waitForFunction(()=>document.querySelector('#cursorReadout').textContent.endsWith('0'));
    await page.locator('#searchKind').selectOption('any_change');
    await control({command:'set_selection',signals:[q.id]});
    await control({command:'set_cursors',cursorA:0,cursorB:20});
    const measurement=await control({command:'get_measurements'});
    assert.equal(measurement.measurement.transitions,5);
    assert.equal(measurement.measurement.eventDelta,10);
    assert.equal(measurement.measurement.reciprocalHz,null);
    await page.screenshot({path:path.join(root,'normal.png')});
    const original=(await control({command:'export_session'})).session;
    const originalRow=original.items.find(item=>item.signal?.name==='Counter.q');
    await page.getByRole('button',{name:'Add Counter.q',exact:true}).click();
    await page.getByRole('button',{name:'Add Counter.q',exact:true}).click();
    const copies=(await control({command:'export_session'})).session;
    const qrows=copies.items.filter(item=>item.signal?.name==='Counter.q');
    assert.equal(qrows.length,3);assert.equal(new Set(qrows.map(row=>row.id)).size,3);
    const copy=qrows.at(-1);
    assert.equal(copies.selectedRow,copy.id);
    assert.equal(await page.locator('.displayRow.selected').count(),1);
    assert.equal(await page.getByRole('group',{name:'Signal color'}).getByRole('button').count(),16);
    await page.getByRole('button',{name:'Red',exact:true}).click();
    assert.equal(await page.getByRole('button',{name:'Red',exact:true}).getAttribute('aria-pressed'),'true');
    const height=page.getByRole('spinbutton',{name:'Row height',exact:true});
    await height.fill('70');await height.press('ArrowUp');
    await page.waitForTimeout(250);
    assert.equal(await height.inputValue(),'71');assert.equal(await height.evaluate(node=>node===document.activeElement),true);
    await height.press('ArrowDown');assert.equal(await height.inputValue(),'70');
    await height.fill('999');await height.press('Tab');
    assert.equal(await height.inputValue(),'70');
    await page.locator('#format').selectOption('binary');
    const customized=(await control({command:'export_session'})).session;
    assert.equal(customized.items.find(row=>row.id===copy.id).height,70);
    assert.equal(customized.items.find(row=>row.id===copy.id).color,'#ff6b6b');
    assert.equal(customized.items.find(row=>row.id===copy.id).format,'binary');
    assert.deepEqual(customized.items.find(row=>row.id===originalRow.id),originalRow);
    // Render-query replies can rebuild the virtualized row between locator
    // resolution and evaluation. Check the current row, not a detached node.
    await page.waitForFunction(id=>document.querySelector(`.displayRow[data-id="${id}"]`)
      ?.getBoundingClientRect().height===70,copy.id);
    assert.equal(await page.locator('#textDialog').evaluate(node=>node.open),false);
    await page.screenshot({path:path.join(root,'palette-height-copies.png')});
    assert.equal(await page.getByRole('button',{name:'Duplicate row',exact:true}).count(),0);
    await page.locator(`.displayRow[data-id="${copy.id}"]`).click({button:'right'});
    await page.screenshot({path:path.join(root,'clipboard-menu.png')});
    await page.getByRole('menuitem',{name:/^Copy/}).click();
    await page.waitForFunction(()=>document.querySelector('#searchReadout').textContent==='Rows copied');
    assert.equal(JSON.parse(clipboardText).schema,'zlang-waveform-rows-v1');
    await page.keyboard.press('Control+v');
    await page.waitForFunction(()=>document.querySelector('#searchReadout').textContent==='Rows pasted');
    const duplicated=(await control({command:'export_session'})).session;
    const duplicate=duplicated.items.find(row=>row.id===duplicated.selectedRow);
    assert.deepEqual({...duplicate,id:copy.id},customized.items.find(row=>row.id===copy.id));
    await control({command:'import_session',session:duplicated});
    assert.equal((await control({command:'export_session'})).session.selectedRow,duplicate.id);
    assert.equal(await page.locator('#format').inputValue(),'binary');
    await control({command:'focus_item',id:originalRow.id});
    assert.equal(await height.inputValue(),'34');
    assert.equal(await page.getByRole('button',{name:'Green',exact:true}).getAttribute('aria-pressed'),'true');
    await page.locator(`.displayRow[data-id="${copy.id}"]`).click({modifiers:['Control']});
    await page.getByRole('button',{name:'Cyan',exact:true}).click();
    const multiselected=(await control({command:'export_session'})).session;
    assert.deepEqual(new Set(multiselected.selectedRows),new Set([originalRow.id,copy.id]));
    for(const id of multiselected.selectedRows)assert.equal(multiselected.items.find(row=>row.id===id).color,'#56dce4');
    assert.equal(multiselected.items.find(row=>row.id===duplicate.id).color,'#ff6b6b');
    await page.locator(`.displayRow[data-id="${copy.id}"]`).click({button:'right'});
    await page.keyboard.press('Escape');await page.keyboard.press('Meta+c');
    await page.waitForFunction(()=>document.querySelector('#searchReadout').textContent==='Rows copied');
    assert.equal(JSON.parse(clipboardText).items.length,2);
    await page.keyboard.press('Control+v');
    await page.waitForFunction(()=>document.querySelector('#searchReadout').textContent==='Rows pasted');
    assert.equal((await control({command:'export_session'})).session.items.length,multiselected.items.length+2);
    await control({command:'import_session',session:multiselected});
    await control({command:'remove_items',ids:[copy.id]});
    assert.equal((await control({command:'export_session'})).session.items.filter(row=>row.signal?.name==='Counter.q').length,3);
    await page.locator('.variable').filter({has:page.getByRole('button',{name:'Add Counter.q',exact:true})}).locator('span').first().dblclick();
    assert.equal((await control({command:'export_session'})).session.items.filter(row=>row.signal?.name==='Counter.q').length,4);
    await control({command:'import_session',session:original});
    await control({command:'focus_item',id:originalRow.id});
    const uncut=(await control({command:'export_session'})).session.items;
    failClipboardWrite=true;await page.keyboard.press('Control+x');
    await page.waitForFunction(()=>document.querySelector('#searchReadout').textContent==='Clipboard write denied');
    assert.deepEqual((await control({command:'export_session'})).session.items,uncut);
    failClipboardWrite=false;await page.keyboard.press('Control+x');
    await page.waitForFunction(()=>document.querySelector('#searchReadout').textContent==='Rows cut');
    assert.equal((await control({command:'export_session'})).session.items.length,original.items.length-1);
    await page.keyboard.press('Control+v');
    await page.waitForFunction(()=>document.querySelector('#searchReadout').textContent==='Rows pasted');
    assert.equal((await control({command:'export_session'})).session.items.length,original.items.length);
    const validClipboard=clipboardText;clipboardText='not waveform rows';
    const beforeBadPaste=(await control({command:'export_session'})).session.items;
    await page.keyboard.press('Control+v');
    await page.waitForFunction(()=>document.querySelector('#searchReadout').textContent.includes('does not contain'));
    assert.deepEqual((await control({command:'export_session'})).session.items,beforeBadPaste);
    clipboardText=validClipboard;
    await page.getByRole('button',{name:'Alias',exact:true}).click();
    await page.locator('#textDialogValue').fill('ordinary text');
    await page.locator('#textDialogValue').press('Control+a');await page.locator('#textDialogValue').press('Control+x');
    assert.equal(await page.locator('#textDialogValue').inputValue(),'');assert.equal(clipboardText,validClipboard);
    await page.locator('#textDialog button[value="cancel"]').click();
    await control({command:'import_session',session:original});
    const before=(await control({command:'export_session'})).session.viewports[0];
    const canvas=page.locator('.viewport canvas').first();const box=await canvas.boundingBox();
    await page.mouse.move(box.x+box.width/2,box.y+80);await page.mouse.wheel(0,-150);
    await page.waitForTimeout(150);
    const zoomed=(await control({command:'export_session'})).session.viewports[0];
    assert.ok(zoomed.end-zoomed.start<before.end-before.start);
    await page.keyboard.down('Shift');await page.mouse.wheel(0,100);await page.keyboard.up('Shift');await page.waitForTimeout(150);
    assert.notEqual((await control({command:'export_session'})).session.viewports[0].start,zoomed.start);
    await page.mouse.down({button:'middle'});await page.mouse.move(box.x+box.width/2-50,box.y+110,{steps:5});await page.mouse.up({button:'middle'});
    assert.equal((await control({command:'export_session'})).session.items.length,original.items.length,
      'Middle-button panning must not paste primary-selection clipboard data');
    await checkContinuousPan({page,control,requests,setRenderDelay:value=>{renderDelay=value;},root});
    await control({command:'import_session',session:original});
    await page.locator('#newGroup').click();await page.locator('#textDialogValue').fill('Datapath');await page.locator('#textDialog button[value="ok"]').click();
    const qrow=page.locator('.displayRow').filter({hasText:'q'}).first();
    await qrow.dragTo(page.locator('.groupRow').filter({hasText:'Datapath'}));
    const grouped=(await control({command:'export_session'})).session;
    assert.ok(grouped.items.find(item=>item.type==='group').children.some(item=>item.signal?.name==='Counter.q'));
    assert.equal(grouped.items.flatMap(item=>item.type==='group'?item.children:[item]).filter(item=>item.signal?.name==='Counter.q').length,1,
      'Drag reordering must not copy a row: '+JSON.stringify(grouped.items));
    await page.locator('.groupRow').click({button:'right'});
    await page.getByRole('menuitem',{name:/^Copy/}).click();
    await page.waitForFunction(()=>document.querySelector('#searchReadout').textContent==='Rows copied');
    assert.equal(JSON.parse(clipboardText).items[0].type,'group');
    await page.locator('#displayRows').click({position:{x:15,y:450},button:'right'});
    await page.getByRole('menuitem',{name:/^Paste/}).click();
    await page.waitForFunction(()=>document.querySelectorAll('.groupRow').length===2);
    await control({command:'import_session',session:grouped});
    await control({command:'set_bookmarks',bookmarks:[{id:'m',time:16,label:'Capture',color:'#ff9d45'}]});
    await page.locator('#addViewport').click();await page.locator('#syncViewports').check();
    await page.waitForTimeout(300);
    assert.equal((await control({command:'export_session'})).session.viewports.length,2);
    await page.screenshot({path:path.join(root,'multi-viewport.png')});
    const portable=(await control({command:'export_session'})).session;
    const hostile=structuredClone(portable);
    const firstSignal=hostile.items.find(item=>item.type==='signal');
    firstSignal.alias='<svg onload="alert(1)">';
    await control({command:'import_session',session:hostile});
    assert.equal(await page.locator('svg').count(),0);
    assert.ok(await page.getByText('<svg onload="alert(1)">',{exact:true}).count()>0);
    portable.items.push({type:'signal',id:'missing',signal:{name:'Counter.absent',width:8}});
    const rebound=await control({command:'import_session',session:portable});
    assert.equal(rebound.unresolved,1);
    assert.ok((await control({command:'export_session'})).session.items.some(item=>item.signal?.name==='Counter.absent'));
    await page.screenshot({path:path.join(root,'unresolved.png')});
    await control({command:'clear'});
    assert.equal(await page.locator('#overview').evaluate(canvas=>{
      const data=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
      let activity=0;for(let i=0;i<data.length;i+=4)
        if(data[i+1]>80&&data[i+1]>data[i]*1.3&&data[i+1]>data[i+2]*1.3)activity++;
      return activity;
    }),0,'removing all signals must not retain stale overview activity');
    await page.screenshot({path:path.join(root,'empty.png')});
    await control({command:'import_session',session:portable});
    const beforeReload=(await control({command:'export_session'})).session;
    const flatten=items=>items.flatMap(item=>item.type==='group'?flatten(item.children):[item]);
    const retained=flatten(beforeReload.items).find(item=>item.signal?.name==='Counter.q');
    await control({command:'focus_item',id:retained.id});
    await page.waitForTimeout(300);
    const previousHolders=await page.locator('.holderRow').count();
    fs.writeFileSync(vcd,lines.filter(line=>!line.startsWith('$var wire 8 ')&&!line.startsWith('b')).join('\n')+'\n');
    await page.evaluate(()=>window.postMessage({type:'fileChanged'},'*'));
    assert.equal((await control({command:'export_session'})).traceChanged,true);
    assert.match(await page.locator('#traceStatus').textContent(),/File changed/);
    const frozenRequests=requests.length;
    await page.locator('#next').click();await page.waitForTimeout(100);
    assert.equal(requests.length,frozenRequests,'changed traces must not issue native reads before Reload');
    await page.locator('#reload').click();
    await page.waitForFunction(count=>document.querySelectorAll('.holderRow').length===count&&!document.querySelector('#reload').disabled,previousHolders+1);
    assert.deepEqual((await control({command:'export_session'})).session.items,beforeReload.items);
    assert.equal((await control({command:'export_session'})).traceChanged,false);
    assert.ok(await page.locator('.holderBadge').getByText('holder',{exact:true}).count()>0);
    await page.locator('#next').click();
    assert.match(await page.locator('#searchReadout').textContent(),/Holder has no current trace data/);
    await page.waitForFunction(()=>[...document.querySelectorAll('.displayRow')].some(row=>
      row.querySelector('.signalName')?.textContent==='clk'&&row.querySelector('.signalValue')?.textContent==='0x0'));
    await page.screenshot({path:path.join(root,'reloaded-holder.png')});
    await page.locator('#reload').click();
    await page.waitForFunction(()=>!document.querySelector('#reload').disabled);
    assert.equal(await page.locator('.holderRow').count(),previousHolders+1);
    await page.locator(`.holderRow[data-id="${retained.id}"] .remove`).click();
    assert.equal(await page.locator('.holderRow').count(),previousHolders);
    await control({command:'import_session',session:beforeReload});
    fs.writeFileSync(vcd,lines.join('\n')+'\n');
    await page.locator('#reload').click();
    await page.waitForFunction(count=>!document.querySelector('#reload').disabled&&document.querySelectorAll('.holderRow').length===count,previousHolders);
    assert.deepEqual((await control({command:'export_session'})).session.items,beforeReload.items);
    fs.writeFileSync(vcd,'incomplete waveform');
    await page.locator('#reload').click();
    await page.waitForFunction(()=>document.querySelector('#traceStatus').textContent.includes('retry after'));
    assert.equal(await page.locator('#reload').isEnabled(),true);
    fs.writeFileSync(vcd,lines.join('\n')+'\n');
    await page.locator('#reload').click();
    await page.waitForFunction(()=>!document.querySelector('#reload').disabled&&document.querySelector('#traceStatus').textContent==='');
    await control({command:'import_session',session:portable});
    const splitter=page.locator('[data-splitter="browser"]');const split=await splitter.boundingBox();
    await page.mouse.move(split.x+2,split.y+40);await page.mouse.down();await page.mouse.move(split.x+60,split.y+40,{steps:4});await page.mouse.up();
    assert.ok((await control({command:'export_session'})).session.panelLayout.browser>240);
    const large=path.join(root,'large.vcd');writeScaleVcd(large);
    const started=performance.now();const largeCatalog=await client.request('open',{path:large});
    const openMilliseconds=performance.now()-started;
    assert.equal(largeCatalog.signals.length,10000);
    const clock=largeCatalog.signals.find(signal=>signal.name==='Large.Bank0.signal0');
    const renderStarted=performance.now();
    const dense=await client.request('render_range',{signals:[clock.id],start:0,end:999999,pixels:1024});
    const renderMilliseconds=performance.now()-renderStarted;
    assert.equal(dense.signals[0].exact,false);assert.equal(dense.signals[0].points.length,0);
    assert.equal(dense.signals[0].buckets.reduce((sum,bucket)=>sum+bucket.transitions,0),999999);
    assert.ok(dense.signals[0].buckets.every(bucket=>bucket.has_zero&&bucket.has_one));
    const responseBytes=Buffer.byteLength(JSON.stringify(dense));assert.ok(responseBytes<16*1024*1024);
    await page.evaluate(catalog=>window.postMessage({type:'catalog',catalog},'*'),largeCatalog);
    await page.waitForFunction(()=>document.querySelectorAll('.displayRow').length===8);
    await assert.rejects(control({command:'add_items',items:['Large'],recursive:true}));
    await control({command:'clear'});
    await control({command:'add_items',items:Array.from({length:40},(_,i)=>'Large.Bank'+i),recursive:true});
    assert.equal((await control({command:'get_item_list'})).ids.length,4000);
    assert.ok(await page.locator('.displayRow').count()<40);
    assert.ok(await page.locator('.variable').count()<300);
    await page.locator('#wavePane').evaluate(node=>{node.scrollTop=50000;});
    await page.waitForTimeout(500);
    assert.ok(await page.locator('.displayRow').count()<40);
    assert.ok((await page.locator('.viewport canvas').first().boundingBox()).height<1200);
    await page.locator('#wavePane').evaluate(node=>{node.scrollTop=0;});
    await checkDensePan({page,control,requests,root});
    const deadline=Date.now()+30000;
    do{await page.waitForTimeout(100);}while(pendingQueries>0&&Date.now()<deadline);
    assert.equal(pendingQueries,0,'render requests must settle before screenshot review');
    await page.waitForFunction(()=>{
      const canvas=document.querySelector('.viewport canvas'),context=canvas.getContext('2d');
      const pixels=context.getImageData(15,35,Math.min(canvas.width-15,200),25).data;
      for(let i=0;i<pixels.length;i+=4)if(pixels[i+1]>140&&pixels[i+1]>pixels[i]*1.3)return true;
      return false;
    });
    await page.screenshot({path:path.join(root,'dense.png')});
    console.log(JSON.stringify({scale:{signals:10000,events:1000000,displayed:4000,
      openMilliseconds,renderMilliseconds,responseBytes,pendingQueries}},null,2));
    assert.deepEqual(errors,[]);assert.ok(requests.length>0);assert.equal(requests.some(request=>request.type==='range'),false);
    assert.ok(requests.every(request=>request.unique),'copies must share channel queries');
    assert.ok(cacheHits>0);assert.ok(cache.bytes<=128*1024*1024);
    await checkTypedLiveBrowser(browser,root,waveformHtml(buildWaveformBundle().toString()));
    console.log(JSON.stringify({result:'passed',requests:requests.length,cacheHits,cacheBytes:cache.bytes,screenshots:root},null,2));
  }catch(error){
    console.error(JSON.stringify({browserErrors:errors,evidence:root}));
    await page?.screenshot({path:path.join(root,'failed.png')}).catch(()=>{});throw error;
  }finally{await client.close();await browser?.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
