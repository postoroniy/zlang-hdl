// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Viacheslav Vinogradov
'use strict';
const path=require('node:path');

function expandLaunchPath(value,file,folder){
  if(value.includes('${workspaceFolder}')&&!folder)
    throw new Error('Debug paths using ${workspaceFolder} require a workspace folder.');
  const expanded=value.replaceAll('${workspaceFolder}',folder||'').replaceAll('${file}',file||'');
  return !expanded||path.isAbsolute(expanded)?expanded:path.resolve(folder||'.',expanded);
}

function debugLaunches(vscode){
  return (vscode.workspace.workspaceFolders||[]).flatMap(folder=>
    vscode.workspace.getConfiguration('launch',folder.uri).get('configurations',[])
      .filter(config=>config.type==='zlang-hdl'&&config.request==='launch'
        &&typeof config.name==='string'&&typeof config.program==='string'&&typeof config.top==='string')
      .map(configuration=>({label:configuration.name,description:folder.name,
        detail:configuration.top+' · '+configuration.program,folder,configuration})));
}

async function selectDebugLaunch(vscode,{name,activeFile}={}){
  let choices=debugLaunches(vscode);
  if(name!==undefined){
    if(typeof name!=='string')throw new Error('Choose a named ZLang launch configuration.');
    choices=choices.filter(choice=>choice.label===name);
    if(!choices.length)throw new Error('Unknown ZLang launch configuration: '+name);
  }else if(activeFile){
    choices=choices.filter(choice=>path.resolve(expandLaunchPath(choice.configuration.program,
      activeFile,choice.folder.uri.fsPath))===path.resolve(activeFile));
    if(!choices.length)throw new Error('Add a ZLang launch configuration with the correct top and stimulus for this file, then use Switch Debug Example.');
  }
  if(!choices.length)throw new Error('Add a ZLang launch configuration in .vscode/launch.json.');
  if((name!==undefined||activeFile)&&choices.length===1)return choices[0];
  return vscode.window.showQuickPick(choices,{title:'Switch ZLang debug example',
    placeHolder:'Select a top and stimulus; the active ZLang session will be replaced',matchOnDetail:true});
}

async function stopSession(vscode,session){
  let timer,subscription;
  try{
    const terminated=new Promise((resolve,reject)=>{
      subscription=vscode.debug.onDidTerminateDebugSession(ended=>{if(ended.id===session.id)resolve();});
      timer=setTimeout(()=>reject(new Error('The previous ZLang session did not terminate; the new example was not started.')),10000);
    });
    await Promise.all([terminated,vscode.debug.stopDebugging(session)]);
  }finally{clearTimeout(timer);subscription?.dispose();}
}

class DebugLaunchController{
  // This controller owns only serialization of explicit session replacements.
  // Different languages and unrelated inactive debug sessions remain VS Code's.
  constructor(vscode,openWaveform){this.vscode=vscode;this.openWaveform=openWaveform;this.pending=Promise.resolve();}
  start(choice){
    if(!choice)return Promise.resolve(false);
    const launch=async()=>{
      const active=this.vscode.debug.activeDebugSession;
      if(active?.type==='zlang-hdl')await stopSession(this.vscode,active);
      if(!await this.vscode.debug.startDebugging(choice.folder,choice.configuration))
        throw new Error('Could not start '+choice.label+'. See the Debug Console and ZLang output.');
      await this.openWaveform();
      return true;
    };
    const result=this.pending.then(launch);
    this.pending=result.catch(()=>{});
    return result;
  }
}

module.exports={expandLaunchPath,debugLaunches,selectDebugLaunch,DebugLaunchController};
