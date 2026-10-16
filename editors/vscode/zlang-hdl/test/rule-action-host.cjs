// SPDX-License-Identifier: Apache-2.0
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vscode=require('vscode');
const {connectControl,focusLiveWaveform}=require('./waveform-host.cjs');

async function waitFor(description,predicate){
  const deadline=Date.now()+10000;
  while(!predicate()){
    assert.ok(Date.now()<deadline,`Timed out waiting for ${description}`);
    await new Promise(resolve=>setTimeout(resolve,25));
  }
}

async function checkRuleActionHost(){
  const repository=path.resolve(__dirname,'../../../..');
  const folder=vscode.workspace.workspaceFolders[0];
  const workspace=folder.uri.fsPath;
  // Install the clone's exact F5 example into the isolated test workspace.
  // Never write launch settings into the repository or the user's profile.
  for(const relative of ['.vscode/launch.json','examples/rule_action.zhl','examples/rule_action.events.jsonl']){
    const destination=path.join(workspace,relative);
    fs.mkdirSync(path.dirname(destination),{recursive:true});
    fs.copyFileSync(path.join(repository,relative),destination);
  }
  await waitFor('checked-in launch configuration',()=>vscode.workspace.getConfiguration('launch',folder.uri)
    .get('configurations',[]).some(item=>item.name==='Debug RuleAction'));
  const source=await vscode.workspace.openTextDocument(path.join(workspace,'examples/rule_action.zhl'));
  const editor=await vscode.window.showTextDocument(source,{preview:false});
  assert.equal(vscode.workspace.getConfiguration('debug',source.uri)
    .get('allowBreakpointsEverywhere'),false,'F9 must work without the global workaround');
  const ruleLine=source.getText().split('\n').findIndex(line=>line.trim()==='when enable {');
  assert.ok(ruleLine>=0);
  // Match normal use: F9 on the state-update line inside the hardware action.
  const breakpointLine=ruleLine+1;
  editor.selection=new vscode.Selection(breakpointLine,2,breakpointLine,2);
  const sourceBreakpoints=()=>vscode.debug.breakpoints.filter(item=>item.location?.uri.toString()===source.uri.toString());
  assert.equal(sourceBreakpoints().length,0);
  // Execute F9's workbench command, not the API that bypasses language registration.
  await vscode.commands.executeCommand('editor.debug.action.toggleBreakpoint');
  await waitFor('F9 source breakpoint',()=>sourceBreakpoints().length===1);
  assert.equal(sourceBreakpoints()[0].location.range.start.line,breakpointLine);
  const messages=[];
  const tracker=vscode.debug.registerDebugAdapterTrackerFactory('zlang-hdl',{
    createDebugAdapterTracker(){return {onDidSendMessage(message){messages.push(message);}};},
  });
  let session,control;
  try{
    assert.equal(await vscode.debug.startDebugging(folder,'Debug RuleAction'),true);
    session=vscode.debug.activeDebugSession;
    assert.equal(session?.type,'zlang-hdl');
    await waitFor('verified F9 breakpoint',()=>messages.some(item=>item.type==='response'
      &&item.command==='setBreakpoints'&&item.success
      &&item.body?.breakpoints.some(bp=>bp.line===breakpointLine+1&&bp.verified)));
    await waitFor('entry stop',()=>messages.some(item=>item.event==='stopped'&&item.body.reason==='entry'));
    await vscode.commands.executeCommand('zlang-hdl.openLiveWaveform');
    const catalog=await session.customRequest('zlang/signalCatalog');
    for(const [name,kind]of [['clk','clock'],['rst','reset']]){
      const signal=catalog.signals.find(item=>item.name===name);
      assert.equal(signal?.kind,kind);assert.equal(signal.traced,true);
    }
    const controls=await session.customRequest('variables',{variablesReference:4});
    assert.deepEqual(controls.variables.map(item=>item.name).sort(),['clk','rst']);
    assert.ok(controls.variables.every(item=>item.presentationHint.attributes.includes('readOnly')));
    for(const [event,count] of [[3,1],[4,2]]){
      // A stale caret on the breakpoint line must not make this test pass.
      await vscode.window.showTextDocument(source,{preview:false});
      editor.selection=new vscode.Selection(0,0,0,0);
      await vscode.commands.executeCommand('zlang-hdl.openLiveWaveform');
      const after=messages.length;
      await vscode.commands.executeCommand('workbench.action.debug.continue');
      await waitFor('Continue breakpoint stop',()=>messages.slice(after)
        .some(item=>item.event==='stopped'&&item.body.reason==='breakpoint'));
      assert.equal((await session.customRequest('zlang/waveformStatus')).event,event);
      // Do not issue a custom stack request or reveal/select the editor here:
      // the workbench itself must focus the stopped source frame for its native
      // execution-line highlight, even while live waveforms had keyboard focus.
      await waitFor('active stopped stack frame',()=>vscode.debug.activeStackItem?.session.id===session.id
        &&vscode.debug.activeStackItem?.frameId===1);
      await waitFor('highlighted breakpoint source line',()=>vscode.window.activeTextEditor?.document.uri.toString()===source.uri.toString()
        &&vscode.window.activeTextEditor.selection.active.line===breakpointLine);
      assert.ok(messages.slice(after).some(item=>item.type==='response'&&item.command==='stackTrace'
        &&item.success&&item.body?.stackFrames?.[0]?.line===breakpointLine+1),
      'VS Code must fetch the stopped source frame itself');
      const stack=await session.customRequest('stackTrace',{threadId:1});
      assert.equal(stack.stackFrames[0].source.path,source.uri.fsPath);
      assert.equal(stack.stackFrames[0].line,breakpointLine+1);
      const values=await session.customRequest('zlang/waveformQuery',{
        command:'value_at',signals:['count'],timestamp:event*2,
      });
      assert.equal(values.values.count,count.toString(2).padStart(8,'0'));
    }
    console.log('VS Code F9 → Continue → source-line highlight: body breakpoint, active stack frame, automatic editor focus, stops at events 3/4 with count 1/2 passed');
    await session.customRequest('restart');
    assert.equal((await session.customRequest('zlang/waveformStatus')).event,0);
    const signals=['clk','rst','count','enable','fired'];
    for(const [step,count] of [0,0,0,1,2,2].entries()){
      if(step)await session.customRequest('next',{threadId:1});
      const result=await session.customRequest('zlang/waveformQuery',{
        command:'value_at',signals,timestamp:step*2,
      });
      assert.equal(result.values.count,count.toString(2).padStart(8,'0'));
      assert.equal(result.values.clk,step?'1':'0');
      assert.equal(result.values.rst,step===1?'1':'0');
      if(step)assert.equal(result.values.fired,step===3||step===4?'1':'0');
    }
    const transitions=await session.customRequest('zlang/waveformQuery',{
      command:'search',query:{signals:['clk'],start:0,end:11,kind:'rising',reverse:false,limit:100},
    });
    assert.deepEqual(transitions.matches.map(item=>item.timestamp),[2,4,6,8,10]);
    const released=await session.customRequest('zlang/waveformQuery',{
      command:'value_at',signals:['clk','rst'],timestamp:3,
    });
    assert.equal(released.values.clk,'0');assert.equal(released.values.rst,'0');
    control=await connectControl();await focusLiveWaveform(control);
    await control.command({command:'set_selection',signals:['clk']});
    await control.command({command:'set_cursors',cursorA:10,cursorB:11});
    const measurement=(await control.command({command:'get_measurements'})).measurement;
    assert.equal(measurement.valueA,'1');assert.equal(measurement.valueB,'0');
    assert.equal(measurement.transitions,1);
    console.log('VS Code RuleAction quick start: checked-in launch by name, entry stop, five steps and live trace count=0,0,1,2,2 passed');
  }finally{
    control?.close();
    if(session)await vscode.debug.stopDebugging(session);
    tracker.dispose();
    vscode.debug.removeBreakpoints(sourceBreakpoints());
  }
}
module.exports={checkRuleActionHost};
