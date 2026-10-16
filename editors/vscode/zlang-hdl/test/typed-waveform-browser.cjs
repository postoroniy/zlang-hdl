// SPDX-License-Identifier: Apache-2.0
'use strict';
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
const {pathToFileURL}=require('node:url');

// Presentation fixture only. Native execution/layout is tested by the real DAP
// host and Rust tests; this checks the generated webview's actual DOM/canvas.
async function checkTypedLiveBrowser(browser,root,html){
  const page=await browser.newPage({viewport:{width:1640,height:920},colorScheme:'dark'});
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  const messages=[],controls=new Map();let sequence=0,liveStatus;
  const fixtures={
    'packet.mode':{width:8,bits:'00001010',display:'Ready (0b00001010)',groups:['packet'],kind:'enum'},
    'message.Data.value':{width:8,bits:'????????',display:'<unknown variant>',groups:['message','message.Data'],kind:'sint'},
    'message.Error.code':{width:4,bits:'----',display:'<inactive variant>',groups:['message','message.Error'],kind:'bits'},
    'uninitialized':{width:1,bits:'u',display:'0bu',groups:[],kind:'bit'},
  };
  await page.exposeFunction('typedHost',async message=>{
    messages.push(message);
    if(message.type==='controlResult'){
      const pending=controls.get(message.requestId);controls.delete(message.requestId);
      if(message.error)pending?.reject(new Error(message.error));else pending?.resolve(message.result);
      return;
    }
    if(message.type==='reload'){
      await page.evaluate(payload=>window.postMessage(payload,'*'),liveStatus);return;
    }
    const ids=message.signals||[];let result;
    if(message.type==='renderRange')result={start:message.start,end:message.end,signals:ids.map(id=>({
      signal:id,width:fixtures[id].width,exact:true,buckets:[],
      points:[{signal:id,timestamp:2,bits:fixtures[id].bits,display:fixtures[id].display}],
    }))};
    if(message.type==='valueAt')result={timestamp:message.timestamp,
      values:Object.fromEntries(ids.map(id=>[id,fixtures[id].bits])),
      display_values:Object.fromEntries(ids.map(id=>[id,fixtures[id].display])),
    };
    if(message.type==='search')result={matches:message.query.start<=2&&message.query.end>=2
      ?[{signal:message.query.signals[0],timestamp:2,bits:fixtures[message.query.signals[0]].bits}]:[]};
    if(message.type==='statistics')result={signals:ids.map(id=>({signal:id,width:fixtures[id].width,
      start_bits:fixtures[id].bits,end_bits:fixtures[id].bits,transitions:0,rising:0,falling:0,
      high_ticks:0,low_ticks:0,unknown_ticks:0}))};
    if(result)await page.evaluate(payload=>window.postMessage(payload,'*'),{type:'queryResult',requestId:message.requestId,result});
  });
  await page.addInitScript(()=>{window.acquireVsCodeApi=()=>({postMessage:message=>window.typedHost(message)});});
  const control=command=>new Promise((resolve,reject)=>{
    const requestId='live-test-'+(++sequence);controls.set(requestId,{resolve,reject});
    page.evaluate(payload=>window.postMessage(payload,'*'),{type:'control',requestId,command}).catch(reject);
    setTimeout(()=>{if(controls.delete(requestId))reject(Error('Live control timed out'));},10000).unref();
  });
  try{
    const file=path.join(root,'typed-live.html');fs.writeFileSync(file,html);
    await page.goto(pathToFileURL(file).href);
    const signals=Object.entries(fixtures).map(([name,fixture])=>({
      name,width:fixture.width,kind:fixture.kind,groups:fixture.groups,
      layout:{kind:fixture.kind,label:fixture.kind},history_start:2,
    }));
    liveStatus={type:'liveStatus',sessionId:'typed',epoch:0,event:4,signals};
    await page.evaluate(payload=>window.postMessage(payload,'*'),liveStatus);
    await page.waitForFunction(()=>document.querySelector('#displayRows').textContent.includes('Ready (0b00001010)'));
    const rows=await page.locator('#displayRows').textContent();
    assert.ok(rows.includes('unknown variant'));assert.ok(rows.includes('inactive variant'));assert.ok(rows.includes('0bu'));
    assert.ok(await page.locator('#format option[value="typed"]').count());
    await page.screenshot({path:path.join(root,'typed-live.png')});
    await control({command:'set_selection',signals:['packet.mode']});
    await page.getByRole('button',{name:'Red',exact:true}).click();
    await page.getByRole('spinbutton',{name:'Row height',exact:true}).fill('70');
    await page.getByRole('spinbutton',{name:'Row height',exact:true}).press('Tab');
    await page.getByRole('button',{name:'Add packet.mode',exact:true}).click();
    await page.locator('#addViewport').click();
    await control({command:'set_cursors',cursorA:2,cursorB:4});
    await control({command:'set_bookmarks',bookmarks:[{id:'mark',label:'Saved mark',time:2,color:'#ff6b6b'}]});
    const before=(await control({command:'export_session'})).session;
    assert.equal(before.viewports.length,2);
    assert.equal(before.items[0].children[0].height,70);
    assert.equal(before.items[0].children[0].color,'#ff6b6b');
    for(const id of ['saveSession','loadSession','addTraceSignals','exportTrace'])await page.locator('#'+id).click();
    for(const type of ['saveSession','loadSession','addTraceSignals','exportTrace'])assert.ok(messages.some(message=>message.type===type));
    await page.locator('#reload').click();
    await page.waitForFunction(()=>document.querySelector('#reload').textContent==='Refresh');
    const refreshed=(await control({command:'export_session'})).session;
    assert.deepEqual(refreshed.items,before.items);assert.equal(refreshed.viewports.length,2);
    await control({command:'set_selection',signals:['packet.mode']});
    await control({command:'set_cursor',timestamp:0});
    await page.locator('#next').click();
    await page.waitForFunction(()=>document.querySelector('#cursorReadout').textContent.endsWith('2'));
    assert.equal((await control({command:'export_session'})).session.followLive,false);
    // A new epoch clears samples, not the workspace's groups/styles/splits.
    liveStatus={...liveStatus,epoch:1,event:0,reset:true};
    await page.evaluate(payload=>window.postMessage(payload,'*'),liveStatus);
    const restarted=(await control({command:'export_session'})).session;
    assert.deepEqual(restarted.items,before.items);assert.equal(restarted.viewports.length,2);
    assert.equal(restarted.bookmarks[0].label,'Saved mark');
    // Deliberately clearing the display must survive subsequent live updates.
    await control({command:'clear'});
    liveStatus={...liveStatus,event:2,reset:false};
    await page.evaluate(payload=>window.postMessage(payload,'*'),liveStatus);
    assert.equal((await control({command:'get_item_list'})).ids.length,0);
    // Workspace recovery goes through the same session validator as file views.
    liveStatus={...liveStatus,sessionId:'restored',layout:before};
    await page.evaluate(payload=>window.postMessage(payload,'*'),liveStatus);
    const restored=(await control({command:'export_session'})).session;
    assert.deepEqual(restored.items,before.items);assert.equal(restored.viewports.length,2);
    await page.screenshot({path:path.join(root,'live-workspace-parity.png')});
    assert.ok(messages.some(message=>message.type==='persist'&&message.sessionId==='typed'));
    // A new debugger session must not leave rows from the previous design.
    await page.evaluate(()=>window.postMessage({type:'liveStatus',sessionId:'new',epoch:0,event:0,signals:[]},'*'));
    await page.waitForFunction(()=>document.querySelectorAll('.displayRow').length===0);
    assert.deepEqual(errors,[]);
  }finally{await page.close();}
}
module.exports={checkTypedLiveBrowser};
