// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Viacheslav Vinogradov
'use strict';
const assert=require('node:assert/strict');
const {test}=require('node:test');
const {expandLaunchPath,debugLaunches,selectDebugLaunch,DebugLaunchController}=require('../debug_launch');

const config=(name,top='Top')=>({type:'zlang-hdl',request:'launch',name,top,program:'${workspaceFolder}/example.zhl'});
function fixture(){
  const events=[],listeners=new Set(),folders=[{name:'one',uri:{fsPath:'/one'}},{name:'two',uri:{fsPath:'/two'}}];
  const configs=[[config('A'),config('B','Second')],[config('A','Other')]];
  let picked;
  const vscode={workspace:{workspaceFolders:folders,getConfiguration:(_key,uri)=>({get:()=>configs[uri===folders[0].uri?0:1]})},
    window:{showQuickPick:async choices=>{picked=choices;return choices[0];}},
    debug:{activeDebugSession:{id:'old',type:'zlang-hdl'},
      onDidTerminateDebugSession:listener=>{listeners.add(listener);return{dispose:()=>listeners.delete(listener)};},
      stopDebugging:async session=>{events.push('stop:'+session.id);setImmediate(()=>{
        events.push('terminated:'+session.id);for(const listener of listeners)listener(session);
      });},
      startDebugging:async(folder,configuration)=>{
        events.push('start:'+folder.name+':'+configuration.name);
        vscode.debug.activeDebugSession={id:configuration.name,type:'zlang-hdl'};return true;
      }}};
  const controller=new DebugLaunchController(vscode,async()=>events.push('waveform'));
  return{vscode,events,listeners,controller,configs,get picked(){return picked;}};
}

test('launch paths and picker preserve multi-root folder and same-name top identities',async()=>{
  const f=fixture();
  assert.equal(expandLaunchPath('${workspaceFolder}/${file}','x','/two'),'/two/x');
  assert.equal(expandLaunchPath('examples/counter.zhl',undefined,'/two'),'/two/examples/counter.zhl');
  assert.equal(expandLaunchPath('${file}',undefined,'/two'),'');
  assert.throws(()=>expandLaunchPath('${workspaceFolder}/x'),/require a workspace/);
  assert.equal(debugLaunches(f.vscode).length,3);
  await selectDebugLaunch(f.vscode,{name:'A'});
  assert.deepEqual(f.picked.map(item=>item.folder.name),['one','two']);
  assert.equal((await selectDebugLaunch(f.vscode,{activeFile:'/two/example.zhl'})).configuration.top,'Other');
  await selectDebugLaunch(f.vscode,{activeFile:'/one/example.zhl'});
  assert.deepEqual(f.picked.map(item=>item.configuration.top),['Top','Second']);
  await assert.rejects(selectDebugLaunch(f.vscode,{name:'missing'}),/Unknown ZLang/);
  await assert.rejects(selectDebugLaunch(f.vscode,{activeFile:'/one/unconfigured.zhl'}),/correct top and stimulus/);
});

test('picker cancellation does not stop or launch any session',async()=>{
  const f=fixture();f.vscode.window.showQuickPick=async()=>undefined;
  assert.equal(await f.controller.start(await selectDebugLaunch(f.vscode)),false);
  assert.deepEqual(f.events,[]);
});

test('replacement waits for termination and serializes repeated switch requests',async()=>{
  const f=fixture(),[first,second]=debugLaunches(f.vscode);
  await Promise.all([f.controller.start(first),f.controller.start(second)]);
  assert.deepEqual(f.events,['stop:old','terminated:old','start:one:A','waveform',
    'stop:A','terminated:A','start:one:B','waveform']);
  assert.equal(f.listeners.size,0);
});

test('other-language sessions stay running and failed launches allow another selection',async()=>{
  const f=fixture();f.vscode.debug.activeDebugSession={id:'python',type:'python'};
  const selected=debugLaunches(f.vscode)[0],start=f.vscode.debug.startDebugging;
  f.vscode.debug.startDebugging=async()=>false;
  await assert.rejects(f.controller.start(selected),/Could not start A/);
  assert.deepEqual(f.events,[]);
  f.vscode.debug.startDebugging=start;
  await f.controller.start(selected);
  assert.deepEqual(f.events,['start:one:A','waveform']);
});

test('a rejected stop cleans listeners and never starts the replacement',async()=>{
  const f=fixture();f.vscode.debug.stopDebugging=async()=>{throw new Error('cannot stop');};
  await assert.rejects(f.controller.start(debugLaunches(f.vscode)[0]),/cannot stop/);
  assert.equal(f.listeners.size,0);assert.deepEqual(f.events,[]);
});
