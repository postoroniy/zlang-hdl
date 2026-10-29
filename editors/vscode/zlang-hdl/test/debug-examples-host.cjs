// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Viacheslav Vinogradov
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vscode=require('vscode');
const {connectControl}=require('./waveform-host.cjs');
const {walk}=require('../waveform_workspace');

async function checkDebugExampleSwitches(){
  const repository=path.resolve(__dirname,'../../../..');
  const folder=vscode.workspace.workspaceFolders[0],workspace=folder.uri.fsPath;
  const configurations=JSON.parse(fs.readFileSync(path.join(repository,'.vscode/launch.json'),'utf8')).configurations;
  const local=value=>value.replaceAll('${workspaceFolder}',workspace);
  const copy=relative=>{
    const destination=path.join(workspace,relative);fs.mkdirSync(path.dirname(destination),{recursive:true});
    fs.copyFileSync(path.join(repository,relative),destination);
  };
  for(const configuration of configurations){
    for(const field of ['program','events'])copy(path.relative(workspace,local(configuration[field])));
    if(configuration.project){
      const relative=path.relative(workspace,local(configuration.project));
      copy(relative);copy(path.join(path.dirname(relative),'zlang.lock'));
      fs.cpSync(path.join(repository,path.dirname(relative),'src'),
        path.join(workspace,path.dirname(relative),'src'),{recursive:true});
    }
  }
  const ended=new Set();
  const termination=vscode.debug.onDidTerminateDebugSession(session=>ended.add(session.id));
  const control=await connectControl();
  let previous;
  try{
    // Include a return to the first example to exercise same-design recovery.
    for(const configuration of [...configurations,configurations[0]]){
      assert.equal(await vscode.commands.executeCommand('zlang-hdl.switchDebugExample',configuration.name),true);
      const session=vscode.debug.activeDebugSession;
      assert.equal(session?.configuration.top,configuration.top);
      if(previous){assert.notEqual(session.id,previous.id);assert.ok(ended.has(previous.id),'Old session must terminate before replacement');}
      previous=session;
      const status=await session.customRequest('zlang/waveformStatus');
      assert.equal(status.event,0);
      const expected=status.signals.map(signal=>signal.name).sort();
      const deadline=Date.now()+10000;
      for(;;){
        const state=(await control.command({command:'export_session'})).session;
        const names=[];walk(state.items,item=>{if(item.signal)names.push(item.signal.name);});
        if(JSON.stringify([...new Set(names)].sort())===JSON.stringify(expected))break;
        assert.ok(Date.now()<deadline,'Wrong live catalog after switching to '+configuration.name+': '+names.join(', '));
        await new Promise(resolve=>setTimeout(resolve,25));
      }
      assert.ok(vscode.window.tabGroups.all.some(group=>group.tabs.some(tab=>
        tab.label==='ZLang HDL Live Waveform — '+configuration.top)));
      await session.customRequest('next',{threadId:1});
      assert.equal((await session.customRequest('zlang/waveformStatus')).event,1);
    }
    console.log('ZLang debug examples: '+configurations.length+' named launches, automatic termination, exact live catalogs, stepping and return-to-first passed');
  }finally{
    control.close();termination.dispose();
    if(previous)await vscode.debug.stopDebugging(previous);
  }
}

module.exports={checkDebugExampleSwitches};
