// SPDX-License-Identifier: Apache-2.0
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {pathToFileURL}=require('node:url');
const {WaveformQueryClient}=require('../waveform_client');

async function checkAnalogBrowser(browser,root,html,executable){
  const page=await browser.newPage({viewport:{width:1640,height:920},colorScheme:'dark'});
  const errors=[],controls=new Map();let sequence=0,native=false,pending=0,dense=false;
  const client=new WaveformQueryClient(executable);
  const bits=value=>(value<0?65536+value:value).toString(2).padStart(16,'0');
  const samples=[{event:0,bits:'u'.repeat(16)},...[0,11585,16384,11585,0,-11585,-16384,-11585,0]
    .map((value,index)=>({event:4+index*4,bits:bits(value)})),{event:40,bits:'x'.repeat(16)},{event:44,bits:bits(0)}];
  const fixed={name:'output.sine',width:16,kind:'fixed',groups:['output'],
    layout:{kind:'fixed',width:16,canonical:{kind:'fixed',width:16,fraction:14}}};
  const valueAt=time=>samples.filter(p=>p.event<=time).at(-1)?.bits??null;
  page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(()=>{
    window.numericLabels=[];
    const fill=CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText=function(text,...args){
      if(this.canvas.closest('#viewports')){window.numericLabels.push(String(text));if(window.numericLabels.length>256)window.numericLabels.shift();}
      return fill.call(this,text,...args);
    };
    window.acquireVsCodeApi=()=>({postMessage:message=>window.analogHost(message)});
  });
  await page.exposeFunction('analogHost',async message=>{
    if(message.type==='controlResult'){
      const slot=controls.get(message.requestId);controls.delete(message.requestId);
      if(message.error)slot?.reject(Error(message.error));else slot?.resolve(message.result);return;
    }
    const commands={renderRange:'render_range',valueAt:'value_at',statistics:'statistics'};
    if(!commands[message.type])return;
    pending++;
    try{
      let result;
      const {type,requestId,...fields}=message;
      if(native)result=await client.request(commands[type],fields);
      else if(type==='renderRange')result={start:message.start,end:message.end,signals:[
        {signal:fixed.name,width:16,exact:!dense,points:dense?[]:samples.map(p=>({timestamp:p.event,bits:p.bits})),
          buckets:dense?[{start:message.start,end:message.end,first_bits:bits(0),last_bits:bits(0),transitions:2}]:[]},
      ]};
      else if(type==='valueAt')result={timestamp:message.timestamp,values:{[fixed.name]:valueAt(message.timestamp)}};
      else result={signals:[]};
      await page.evaluate(payload=>window.postMessage(payload,'*'),{type:'queryResult',requestId,result});
    }finally{pending--;}
  });
  const control=command=>new Promise((resolve,reject)=>{
    const requestId='analog-'+(++sequence);controls.set(requestId,{resolve,reject});
    page.evaluate(payload=>window.postMessage(payload,'*'),{type:'control',requestId,command}).catch(reject);
    setTimeout(()=>{if(controls.delete(requestId))reject(Error('Analog control timed out'));},10000).unref();
  });
  const settled=async()=>{for(let i=0;i<100&&pending;i++)await page.waitForTimeout(20);assert.equal(pending,0);};
  try{
    const file=path.join(root,'analog.html');fs.writeFileSync(file,html);
    await page.goto(pathToFileURL(file).href);
    await page.evaluate(signal=>window.postMessage({type:'liveStatus',sessionId:'analog-cordic',epoch:0,event:44,signals:[signal]},'*'),fixed);
    await control({command:'set_selection',signals:[fixed.name]});
    await control({command:'set_cursor',timestamp:28});
    await page.waitForFunction(()=>document.querySelector('#inspectorBody').textContent.includes('A: -1'));
    assert.match(await page.locator('#inspectorBody').textContent(),/signed · 14 fractional bits/);
    assert.equal(await page.getByRole('spinbutton',{name:'Row height',exact:true}).inputValue(),'72');
    await page.waitForFunction(()=>window.numericLabels.includes('-1')&&window.numericLabels.includes('1'));
    await settled();await page.screenshot({path:path.join(root,'analog-cordic-auto.png')});
    const original=(await control({command:'export_session'})).session;
    const canvas=page.locator('.viewport canvas'),bounds=await canvas.boundingBox();
    await page.mouse.move(bounds.x+bounds.width/2,bounds.y+80);await page.mouse.wheel(0,-360);
    await page.waitForTimeout(150);
    const zoomed=(await control({command:'export_session'})).session;
    assert.ok(zoomed.viewports[0].end-zoomed.viewports[0].start<44);
    await page.mouse.down({button:'middle'});await page.mouse.move(bounds.x+bounds.width/2+40,bounds.y+80,{steps:5});await page.mouse.up({button:'middle'});
    await page.waitForTimeout(150);await settled();
    const panned=(await control({command:'export_session'})).session;
    assert.notEqual(panned.viewports[0].start,zoomed.viewports[0].start);
    await page.screenshot({path:path.join(root,'analog-pan-zoom.png')});
    await control({command:'import_session',session:original});
    await page.getByLabel('Waveform display',{exact:true}).selectOption('digital');
    assert.equal(await page.getByLabel('Analog interpretation',{exact:true}).count(),0);
    await page.getByLabel('Waveform display',{exact:true}).selectOption('analog');
    await page.getByLabel('Analog fraction bits',{exact:true}).fill('0');
    await page.getByLabel('Analog fraction bits',{exact:true}).press('Tab');
    await page.waitForFunction(()=>document.querySelector('#inspectorBody').textContent.includes('A: -16384'));
    const saved=(await control({command:'export_session'})).session;
    assert.equal(saved.items[0].children[0].display,'analog');
    assert.equal(saved.items[0].children[0].analog.fraction,0);
    await control({command:'import_session',session:saved});
    assert.equal(await page.getByLabel('Analog fraction bits',{exact:true}).inputValue(),'0');
    await page.getByLabel('Analog fraction bits',{exact:true}).fill('');
    await page.getByLabel('Analog fraction bits',{exact:true}).press('Tab');
    await page.waitForFunction(()=>document.querySelector('#inspectorBody').textContent.includes('A: -1'));
    await settled();
    dense=true;
    await page.evaluate(signal=>{window.numericLabels=[];window.postMessage({type:'liveStatus',sessionId:'analog-cordic',epoch:1,event:44,reset:true,signals:[signal]},'*');},fixed);
    await page.waitForFunction(()=>window.numericLabels.includes('dense activity · zoom in'));
    await settled();await page.screenshot({path:path.join(root,'analog-dense.png')});
    // Actual saved VCD -> reviewed Rust parser -> generated browser, not a JS
    // reimplementation of waveform parsing or an IEEE guess from a bit bus.
    const vcd=path.join(root,'analog-real.vcd');
    fs.writeFileSync(vcd,'$timescale 1ns $end\n$scope module Top $end\n$var real 64 ! voltage $end\n$upscope $end\n$enddefinitions $end\n#0\nr0 !\n#4\nr1.25 !\n#8\nr-2.5 !\n#12\nr0.5 !\n');
    const catalog=await client.request('open',{path:vcd});
    assert.equal(catalog.signals[0].kind,'real');native=true;
    await page.goto(pathToFileURL(file).href);
    await page.evaluate(catalog=>window.postMessage({type:'catalog',catalog},'*'),catalog);
    await control({command:'set_selection',signals:[catalog.signals[0].id]});
    await control({command:'set_cursor',timestamp:8});
    await page.waitForFunction(()=>document.querySelector('#inspectorBody').textContent.includes('A: -2.5'));
    await page.waitForFunction(()=>window.numericLabels.includes('-2.5')&&window.numericLabels.includes('1.25'));
    await settled();await page.screenshot({path:path.join(root,'analog-saved-real.png')});
    assert.deepEqual(errors,[]);
  }catch(error){await page.screenshot({path:path.join(root,'analog-failed.png')}).catch(()=>{});throw error;}
  finally{await settled();await page.close();await client.close();}
}
module.exports={checkAnalogBrowser};
