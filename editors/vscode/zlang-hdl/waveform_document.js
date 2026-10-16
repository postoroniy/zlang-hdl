// SPDX-License-Identifier: Apache-2.0
'use strict';
const path=require('node:path');
const fs=require('node:fs');
const {WaveformQueryQueue}=require('./waveform_client');
const {WaveformTileCache,waveformQueryKey}=require('./waveform_cache');

// Watch the parent so an atomic replacement is detected as well as writes.
function watchWaveformFile(vscode,uri,changed){
  const watcher=vscode.workspace.createFileSystemWatcher(
    new vscode.RelativePattern(path.dirname(uri.fsPath),'*'));
  const checkpoint=()=>{
    try{
      const stat=fs.statSync(uri.fsPath,{bigint:true});
      return [stat.dev,stat.ino,stat.size,stat.mtimeNs,stat.ctimeNs].join(':');
    }catch(error){return 'unavailable:'+error.code;}
  };
  let observed=checkpoint();
  const notify=file=>{
    if(file.toString()!==uri.toString())return;
    const current=checkpoint();
    if(current!==observed){observed=current;changed();}
  };
  const subscriptions=[watcher.onDidChange(notify),watcher.onDidCreate(notify),watcher.onDidDelete(notify)];
  return {checkpoint(){observed=checkpoint();return observed;},
    dispose(){for(const subscription of subscriptions)subscription.dispose();watcher.dispose();}};
}

// One saved trace owns its native index, query generation and file revisions.
// All editors of that document observe the same reload boundary.
class WaveformDocument {
  constructor(uri,client,watchChanges){
    this.uri=uri;this.client=client;this.cache=new WaveformTileCache();
    this.queries=new WaveformQueryQueue((command,fields)=>this.client.request(command,fields));
    this.panelSequence=0;this.generation=0;this.fileRevision=0;this.loadedRevision=null;
    this.catalog=null;this.loading=null;this.listeners=new Set();this.closed=false;
    this.watcher=watchChanges?.(()=>this.markChanged());
  }
  get changed(){return this.loadedRevision!==null&&this.loadedRevision!==this.fileRevision;}
  subscribe(listener){this.listeners.add(listener);return{dispose:()=>this.listeners.delete(listener)};}
  notify(message){for(const listener of this.listeners)listener(message);}
  markChanged(){
    this.fileRevision++;this.generation++;this.queries.dispose();
    this.notify({type:'fileChanged'});
  }
  async cachedRequest(command,fields,purpose){
    if(this.loading||this.closed||this.changed)return null;
    const generation=this.generation,key=waveformQueryKey(command,fields);
    const cached=this.cache.get(key);if(cached!==undefined)return cached;
    const result=purpose===undefined?await this.client.request(command,fields)
      :await this.queries.request(purpose,command,fields);
    if(generation!==this.generation||this.closed)return null;
    if(result!==null)this.cache.set(key,result);
    return result;
  }
  open(){return this.catalog?Promise.resolve(this.catalog):this.load(false);}
  reload(){return this.load(true);}
  load(publish){
    if(this.closed)return Promise.reject(new Error('Waveform document is closed'));
    if(this.loading)return this.loading;
    // A checkpoint suppresses delayed duplicate/create notifications for bytes
    // that were already indexed. Inode catches atomic replacement; timestamps
    // and size catch in-place writes, without hashing a potentially 1 GiB file.
    const stamp=this.watcher?.checkpoint?.();
    const revision=this.fileRevision,preserveLayout=this.catalog!==null;
    this.generation++;this.cache.clear();this.queries.dispose();
    this.queries=new WaveformQueryQueue((command,fields)=>this.client.request(command,fields));
    if(publish)this.notify({type:'reloadStarted'});
    this.loading=(async()=>{
      try{
        // Opening by the original path also permits retry after an initial
        // incomplete file, and follows an atomically replaced file correctly.
        const catalog=await this.client.request('open',{path:this.uri.fsPath});
        if(stamp!==this.watcher?.checkpoint?.())this.fileRevision++;
        this.catalog=catalog;this.loadedRevision=revision;
        if(publish)this.notify({type:'catalog',catalog,preserveLayout,fileChanged:this.changed});
        return catalog;
      }catch(error){
        this.fileRevision++;
        if(publish)this.notify({type:'reloadFailed',error:'Cannot reload waveform: '+error.message});
        throw error;
      }finally{this.loading=null;}
    })();
    return this.loading;
  }
  dispose(){
    if(this.closed)return;
    this.closed=true;this.watcher?.dispose();this.listeners.clear();
    this.queries.dispose();this.cache.clear();void this.client.close();
  }
}
module.exports={WaveformDocument,watchWaveformFile};
