// SPDX-License-Identifier: Apache-2.0
'use strict';
const path=require('node:path');

// Sidecar envelope and file-dialog I/O only. Row validation/rebinding stays in
// waveform_session; live files are explicitly layout templates, never samples.
function sessionSource(session){
  if(session?.schema!=='zlang-waveform-session-v1')throw new Error('Unsupported waveform session schema.');
  if(session.source?.kind==='live'){
    if(session.trace!==undefined)throw new Error('A live session template must not name a saved trace.');
    return 'live';
  }
  if(session.source!==undefined)throw new Error('Unsupported waveform session source.');
  if(typeof session.trace!=='string'||!session.trace)throw new Error('Waveform session does not name a trace.');
  if(!['.fst','.vcd'].includes(path.extname(session.trace).toLowerCase()))
    throw new Error('Waveform session trace must be a VCD or FST file.');
  return 'file';
}
function sessionFile(layout,target,trace){
  const session={...layout};
  delete session.trace;delete session.source;
  if(trace){
    const relative=path.relative(path.dirname(target),trace);
    session.trace=relative&&!path.isAbsolute(relative)?relative:trace;
  }else session.source={kind:'live'};
  sessionSource(session);
  const bytes=Buffer.from(JSON.stringify(session,null,2)+'\n','utf8');
  if(bytes.byteLength>1024*1024)throw new Error('Waveform session exceeds 1 MiB; reduce the saved signal set.');
  return bytes;
}
async function saveSession(vscode,channel,traceUri){
  if(!channel)throw new Error('Open and focus a waveform before saving a session.');
  const base=traceUri?path.basename(traceUri.fsPath,path.extname(traceUri.fsPath)):'live-waveform';
  const directory=traceUri?path.dirname(traceUri.fsPath):vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  const target=await vscode.window.showSaveDialog({
    defaultUri:directory?vscode.Uri.file(path.join(directory,base+'.zvw.json')):undefined,
    filters:{'ZLang waveform sessions':['zvw.json']},
  });
  if(!target)return;
  const response=await channel.request({command:'export_session'});
  await vscode.workspace.fs.writeFile(target,sessionFile(response.session,target.fsPath,traceUri?.fsPath));
}
async function readSession(vscode){
  const selected=await vscode.window.showOpenDialog({canSelectMany:false,filters:{'ZLang waveform sessions':['zvw.json']}});
  if(!selected?.[0])return null;
  if((await vscode.workspace.fs.stat(selected[0])).size>1024*1024)throw new Error('Waveform session exceeds its 1 MiB limit.');
  const bytes=await vscode.workspace.fs.readFile(selected[0]);
  if(bytes.byteLength>1024*1024)throw new Error('Waveform session exceeds its 1 MiB limit.');
  let session;
  try{session=JSON.parse(Buffer.from(bytes).toString('utf8'));}
  catch(error){throw new Error('Invalid waveform session JSON: '+error.message);}
  return {uri:selected[0],session,kind:sessionSource(session)};
}
async function exportLiveTrace(vscode,session){
  if(session?.type!=='zlang-hdl')throw new Error('Start a ZLang HDL debug session before exporting its trace.');
  const directory=vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  const target=await vscode.window.showSaveDialog({
    defaultUri:directory?vscode.Uri.file(path.join(directory,'build','live-trace.fst')):undefined,
    filters:{'FST waveform':['fst'],'VCD waveform':['vcd']},
  });
  if(!target)return;
  const extension=path.extname(target.fsPath).toLowerCase();
  if(!['.fst','.vcd'].includes(extension))throw new Error('Trace export requires an .fst or .vcd filename.');
  await vscode.workspace.fs.createDirectory(vscode.Uri.file(path.dirname(target.fsPath)));
  await session.customRequest(extension==='.fst'?'zlang/exportFst':'zlang/exportVcd',{path:target.fsPath});
}
module.exports={sessionSource,sessionFile,saveSession,readSession,exportLiveTrace};
