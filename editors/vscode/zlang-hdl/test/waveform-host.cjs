// SPDX-License-Identifier: Apache-2.0
'use strict';
const assert=require('node:assert/strict');
const net=require('node:net');
const path=require('node:path');
const fs=require('node:fs');
const vscode=require('vscode');

async function connectControl(){
  const port=Number(process.env.ZLANG_EDITOR_SMOKE_WCP_PORT);
  assert.ok(port>0);
  const socket=net.createConnection(port,'127.0.0.1');
  await new Promise((resolve,reject)=>{socket.once('connect',resolve);socket.once('error',reject);});
  let buffered='';const queue=[];
  socket.setEncoding('utf8');socket.on('data',data=>{
    buffered+=data;const parts=buffered.split('\0');buffered=parts.pop();
    for(const raw of parts){if(raw)queue.shift()?.(JSON.parse(raw));}
  });
  const request=message=>new Promise((resolve,reject)=>{
    const timeout=setTimeout(()=>reject(new Error('WCP host request timed out')),10000);
    queue.push(response=>{clearTimeout(timeout);if(response.type==='error')reject(new Error(response.message));else resolve(response);});
    socket.write(JSON.stringify(message)+'\0');
  });
  const greeting=await request({type:'greeting',version:'1',commands:[]});assert.equal(greeting.version,'1');
  return {command:fields=>request({type:'command',...fields}),close:()=>socket.destroy()};
}
async function eventually(check){
  const deadline=Date.now()+10000;
  while(Date.now()<deadline){const result=await check();if(result)return result;await new Promise(resolve=>setTimeout(resolve,50));}
  assert.fail('Waveform state did not reach the expected debugger event');
}
async function checkRowClipboard(control){
  const original=await control.command({command:'export_session'});
  const before=await control.command({command:'get_item_list'}),clipboard=await vscode.env.clipboard.readText();
  try{
    await vscode.commands.executeCommand('workbench.action.focusActiveEditorGroup');
    await control.command({command:'focus_item',id:before.ids[0]});
    console.log('Clipboard host focus: '+vscode.window.tabGroups.activeTabGroup.activeTab?.label);
    await vscode.commands.executeCommand('zlang-hdl.copyWaveformRows');
    await eventually(async()=>{
      try{return JSON.parse(await vscode.env.clipboard.readText()).schema==='zlang-waveform-rows-v1';}catch{return false;}
    });
    await vscode.commands.executeCommand('zlang-hdl.pasteWaveformRows');
    await eventually(async()=>(await control.command({command:'get_item_list'})).ids.length===before.ids.length+1);
    await vscode.commands.executeCommand('zlang-hdl.cutWaveformRows');
    await eventually(async()=>(await control.command({command:'get_item_list'})).ids.length===before.ids.length);
    await vscode.commands.executeCommand('zlang-hdl.pasteWaveformRows');
    await eventually(async()=>(await control.command({command:'get_item_list'})).ids.length===before.ids.length+1);
    await control.command({command:'import_session',session:original.session});
  }finally{await vscode.env.clipboard.writeText(clipboard);}
}
async function focusLiveWaveform(control){
  // DAP stops asynchronously reveal the source editor. WCP addresses the last
  // active waveform, so establish the actual panel, not merely a shared cursor.
  const groups=vscode.window.tabGroups.all.length;
  await eventually(async()=>{
    await vscode.commands.executeCommand('zlang-hdl.openLiveWaveform');
    assert.equal(vscode.window.tabGroups.all.length,groups,'Reopening live waveform must not create another editor group');
    if(!vscode.window.tabGroups.activeTabGroup.activeTab?.label.startsWith('ZLang HDL Live Waveform'))return false;
    return(await control.command({command:'export_session'})).session.catalogSignature==='live';
  });
}
async function checkWaveformHost(session,scratch){
  await vscode.commands.executeCommand('zlang-hdl.openLiveWaveform');
  const control=await connectControl();
  try{
    await eventually(async()=>{
      try{return(await control.command({command:'get_item_list'})).ids.length>0;}catch{return false;}
    });
    await control.command({command:'set_selection',signals:['observed']});
    await control.command({command:'set_cursors',cursorA:2,cursorB:2});
    const live=await control.command({command:'get_measurements'});
    assert.equal(live.measurement.valueA,'00000101');assert.equal(live.measurement.reciprocalHz,null);
    const initial=new Map();
    for(const [extension,command]of [['fst','zlang/exportFst'],['vcd','zlang/exportVcd']]){
      const file=path.join(scratch,'initial.'+extension);
      await session.customRequest(command,{path:file});initial.set(extension,file);
    }
    await session.customRequest('next',{threadId:1});
    await eventually(async()=>(await control.command({command:'export_session'})).session.cursorA===4);
    const statusBefore=await session.customRequest('zlang/waveformStatus');
    await control.command({command:'set_cursors',cursorA:2,cursorB:4});
    assert.equal((await session.customRequest('zlang/waveformStatus')).event,statusBefore.event,
      'Moving waveform cursors must never advance the simulator');
    const changed=await control.command({command:'get_measurements'});
    assert.equal(changed.measurement.valueB,'00001001');assert.equal(changed.measurement.transitions,1);
    // The stopped debugger may have focused the source editor after next.
    await focusLiveWaveform(control);
    for(let step=0;step<=10;step++)await control.command({
      command:'set_viewport_range',start:step/5,end:2+step/5,
    });
    await checkRowClipboard(control);
    assert.equal((await session.customRequest('zlang/waveformStatus')).event,statusBefore.event,
      'Live pan and row Cut/Copy/Paste must never change debugger state');
    for(const [extension,command]of [['fst','zlang/exportFst'],['vcd','zlang/exportVcd']]){
      const file=path.join(scratch,'waveform-host.'+extension);await session.customRequest(command,{path:file});
      assert.ok(fs.statSync(file).size>0);
      await vscode.commands.executeCommand('vscode.openWith',vscode.Uri.file(file),'zlang-hdl.waveform');
      await eventually(async()=>{
        const items=await control.command({command:'get_item_list'});
        const info=await control.command({command:'get_item_info',ids:items.ids});
        return info.results.some(item=>item.name==='DebugSmoke.observed');
      });
      let saved;
      await eventually(async()=>{
        const state=await control.command({command:'export_session'});
        if(state.reloading||state.traceChanged)return false;
        await control.command({command:'set_selection',signals:['DebugSmoke.observed']});
        await control.command({command:'set_cursors',cursorA:2,cursorB:4});
        saved=await control.command({command:'get_measurements'});
        // FST and VCD have identical names. Wait for the new editor's actual
        // two-event data, not an old tab's catalog or an in-flight response.
        return saved.measurement?.valueB==='00001001';
      });
      assert.ok(saved.measurement,JSON.stringify({format:extension,state:await control.command({command:'export_session'}),saved}));
      assert.equal(saved.measurement.valueA,'00000101');assert.equal(saved.measurement.valueB,'00001001');
      await checkRowClipboard(control);
      const before=(await control.command({command:'export_session'})).session;
      fs.copyFileSync(initial.get(extension),file+'.new');fs.renameSync(file+'.new',file);
      await eventually(async()=>(await control.command({command:'export_session'})).traceChanged);
      await control.command({command:'reload'});
      await eventually(async()=>{const state=await control.command({command:'export_session'});return!state.reloading&&!state.traceChanged;});
      assert.deepEqual((await control.command({command:'export_session'})).session.items,before.items);
      const reloaded=await control.command({command:'get_measurements'});
      assert.equal(reloaded.measurement.valueB,'00000101','reload must return new native data rather than a cached old value');
      // Close the actual custom document while a viewport refresh may still
      // have native queries in flight; hidden tabs alone do not test shutdown.
      await control.command({command:'set_viewport_range',start:0,end:2});
      const tab=vscode.window.tabGroups.activeTabGroup.activeTab;
      assert.equal(tab.input.uri.fsPath,file);
      assert.equal(await vscode.window.tabGroups.close(tab),true);
    }
    // Return from saved editors to the same stopped debug session. Navigation
    // and reload must not consume its next stimulus or disable live updates.
    await focusLiveWaveform(control);
    assert.equal((await session.customRequest('zlang/waveformStatus')).event,statusBefore.event);
    await session.customRequest('next',{threadId:1});
    assert.equal((await session.customRequest('zlang/waveformStatus')).event,3);
    await focusLiveWaveform(control);
    try{
      await eventually(async()=>(await control.command({command:'export_session'})).session.cursorA===6);
    }catch(error){
      console.error('Live debug state: '+JSON.stringify({
        native:await session.customRequest('zlang/waveformStatus'),
        viewer:await control.command({command:'export_session'}),
        tab:vscode.window.tabGroups.activeTabGroup.activeTab?.label,
      }));throw error;
    }
    await control.command({command:'set_selection',signals:['observed']});
    await control.command({command:'set_cursors',cursorA:4,cursorB:6});
    const resumed=await control.command({command:'get_measurements'});
    assert.equal(resumed.measurement.valueA,'00001001');
    assert.equal(resumed.measurement.valueB,'00010001');
    assert.equal(resumed.measurement.transitions,1);
    console.log('VS Code waveform host: DAP stepping after live pan/clipboard and saved FST/VCD reload passed (5 → 9 → 17)');
  }finally{control.close();}
}
module.exports={checkWaveformHost,connectControl,focusLiveWaveform};
