// SPDX-License-Identifier: Apache-2.0
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vscode=require('vscode');
const {connectControl,focusLiveWaveform}=require('./waveform-host.cjs');

async function checkTypedWaveformHost(scratch){
  const source=path.join(scratch,'typed-debug.zhl'),events=path.join(scratch,'typed-events.jsonl');
  fs.writeFileSync(source,`
enum Mode : u8 { Idle Ready = 10 Done = 121 }
struct Packet { mode : Mode value : s8 }
union Message { Idle Data { value : s8 } Error { code : bits<4> } }
module TypedDebug {
    clock clk reset rst
    in load : bit
    in code : u8
    in value : s8
    out packet : Packet
    out message : Message
    reg mode : Mode = Mode.Idle
    reg stored : Message = Message.Idle
    when load {
        mode <- enum_decode<Mode>(code, Mode.Idle)
        stored <- switch code { 10 => Message.Data { value } else => Message.Error { code = 13 } }
    }
    packet = Packet { mode value }
    message = stored
}
`);
  fs.writeFileSync(events,[
    {reset:{rst:true},edges:['clk']},
    {reset:{rst:false},set_logic:{load:'1',code:'00001010',value:'11111001'},edges:['clk']},
    {set_logic:{code:'01111001',value:'00000011'},edges:['clk']},
  ].map(event=>JSON.stringify(event)).join('\n')+'\n');
  assert.equal(await vscode.debug.startDebugging(undefined,{
    type:'zlang-hdl',request:'launch',name:'Typed waveform host',program:source,top:'TypedDebug',events,
    initialRegisters:{mode:'u',stored:'u'},traceSignals:[],stopOnEntry:true,
  }),true);
  const session=vscode.debug.activeDebugSession;
  assert.equal(session?.type,'zlang-hdl');
  let control;
  try{
    const ports=await session.customRequest('variables',{variablesReference:1});
    const packet=ports.variables.find(item=>item.name==='packet');
    assert.ok(packet.variablesReference>0);
    const fields=await session.customRequest('variables',{variablesReference:packet.variablesReference});
    assert.deepEqual(fields.variables.map(field=>field.evaluateName),['packet.mode','packet.value']);
    assert.match(fields.variables[0].value,/u/);
    await vscode.commands.executeCommand('zlang-hdl.addToWaveform',{sessionId:session.id,variable:packet});
    await vscode.commands.executeCommand('zlang-hdl.addToWaveform',{
      sessionId:session.id,variable:ports.variables.find(item=>item.name==='message'),
    });
    control=await connectControl();
    await focusLiveWaveform(control);
    let result=await session.customRequest('zlang/waveformQuery',{command:'value_at',signals:['packet.mode'],timestamp:0});
    assert.equal(result.values['packet.mode'],null,'late history is not U or fabricated history');
    await session.customRequest('next',{threadId:1});
    await session.customRequest('next',{threadId:1});
    await focusLiveWaveform(control);
    result=await session.customRequest('zlang/waveformQuery',{
      command:'value_at',signals:['packet.mode','packet.value','message.Data.value','message.Error.code'],timestamp:4,
    });
    assert.match(result.display_values['packet.mode'],/^Ready /);
    assert.match(result.display_values['packet.value'],/^-7 /);
    assert.equal(result.display_values['message.Error.code'],'<inactive variant>');
    await control.command({command:'set_selection',signals:['packet.mode']});
    await control.command({command:'set_cursors',cursorA:4,cursorB:4});
    const state=await control.command({command:'export_session'});
    assert.ok(state.session.items.some(item=>item.type==='group'&&item.name==='packet'));
    await session.customRequest('next',{threadId:1});
    result=await session.customRequest('zlang/waveformQuery',{
      command:'value_at',signals:['packet.mode','message.Data.value','message.Error.code'],timestamp:6,
    });
    assert.match(result.display_values['packet.mode'],/^Done /);
    assert.equal(result.display_values['message.Data.value'],'<inactive variant>');
    assert.equal(result.values['message.Error.code'],'1101');
    await focusLiveWaveform(control);
    const row=state.session.items.find(item=>item.type==='group'&&item.name==='packet')
      .children.find(item=>item.signal?.name==='packet.mode');
    await control.command({command:'set_item_color',id:row.id,color:'#ff6b6b'});
    const workspace=(await control.command({command:'export_session'})).session;
    workspace.viewports=[{start:0,end:6},{start:2,end:6}];
    workspace.bookmarks=[{id:'m',label:'Retained marker',time:4,color:'#ff6b6b'}];
    workspace.followLive=false;
    await control.command({command:'import_session',session:workspace});
    const beforeRefresh=await session.customRequest('zlang/waveformStatus');
    await control.command({command:'reload'});
    assert.equal((await session.customRequest('zlang/waveformStatus')).event,beforeRefresh.event);
    await session.customRequest('restart',{});
    await focusLiveWaveform(control);
    const restarted=(await control.command({command:'export_session'})).session;
    assert.deepEqual(restarted.items,workspace.items);
    assert.equal(restarted.viewports.length,2);
    assert.equal(restarted.bookmarks[0].label,'Retained marker');
    // Closing/reopening the live editor must use its workspace autosave.
    const deadline=Date.now()+10000;
    while((await control.command({command:'export_session'})).layoutPending){
      assert.ok(Date.now()<deadline,'Live workspace did not acknowledge autosave');
      await new Promise(resolve=>setTimeout(resolve,50));
    }
    await vscode.commands.executeCommand('workbench.action.closeActiveEditor');
    await vscode.commands.executeCommand('zlang-hdl.openLiveWaveform');
    await focusLiveWaveform(control);
    const recovered=(await control.command({command:'export_session'})).session;
    assert.deepEqual(recovered.items,workspace.items);
    assert.equal(recovered.viewports.length,2);
    console.log('VS Code typed waveform host: Variables → Add to Waveform, enum Ready/Done, s8=-7, union variant switch and late history passed');
    console.log('VS Code live workspace: Refresh is read-only, restart/reopen retain groups, colors, bookmarks and two viewports');
  }finally{control?.close();await vscode.debug.stopDebugging(session);}
}
module.exports={checkTypedWaveformHost};
