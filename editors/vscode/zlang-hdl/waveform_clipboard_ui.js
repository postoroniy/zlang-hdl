// SPDX-License-Identifier: Apache-2.0
// Row editing and its menu. The extension owns access to the system clipboard.
function clipboardRowIdentities(items){
  const keys=new Set();walk(items,item=>{if(item.type==='signal')keys.add(signalKey(item.signal));});return keys;
}
async function editWaveformRows(action,targetId=state.selectedRowId){
  if(state.clipboardBusy)return;
  hideWaveformEditMenu();state.clipboardBusy=true;
  el('searchReadout').textContent={cut:'Cutting rows…',copy:'Copying rows…',paste:'Pasting rows…'}[action];
  try{
    const ids=new Set(state.selectedRowIds);
    let text;
    if(action==='paste'){
      const response=await queryRequest('clipboard',{action:'read',token:++state.sequence},'clipboard');
      if(!response)return;text=response.text;
      const pasted=pasteWaveformClipboard(serializeSessionLayout(state.layout,state.byId),text,
        state.catalog,targetId,()=>uid('row'));
      state.clipboardHolderKeys=clipboardRowIdentities(parseWaveformClipboard(text));
      state.layout=pasted.items;state.selectedRowIds=new Set(pasted.roots);state.selectedRowId=pasted.roots.at(-1);
    }else{
      const selected=selectedWaveformRows(state.layout,ids);
      if(!selected.length){el('searchReadout').textContent='Select rows to '+action;return;}
      const items=serializeSessionLayout(selected,state.byId);
      text=encodeWaveformClipboard(items);
      const response=await queryRequest('clipboard',{action:'write',text,token:++state.sequence},'clipboard');
      if(!response?.written)return;
      state.clipboardHolderKeys=clipboardRowIdentities(items);
      if(action==='cut'){
        const current=serializeSessionLayout(selectedWaveformRows(state.layout,ids),state.byId);
        if(JSON.stringify(current)!==JSON.stringify(items)){
          el('searchReadout').textContent='Rows copied; selection changed, so nothing was cut';return;
        }
        for(const item of selected)removeItem(state.layout,item.id);
        state.selectedRowIds.clear();state.selectedRowId=null;
      }
    }
    renderAll();scheduleQuery();save();
    el('searchReadout').textContent=action==='paste'?'Rows pasted':action==='cut'?'Rows cut':'Rows copied';
    el('displayRows').focus({preventScroll:true});
  }catch(error){el('searchReadout').textContent=error.message;}
  finally{state.clipboardBusy=false;state.querySlots.delete('clipboard');}
}
function hideWaveformEditMenu(){el('rowEditMenu').hidden=true;}
function showWaveformEditMenu(x,y){
  const menu=el('rowEditMenu');menu.replaceChildren();
  const modifier=/Mac|iPhone|iPad/.test(navigator.platform)?'⌘':'Ctrl+';
  for(const [action,key] of [['cut','X'],['copy','C'],['paste','V']]){
    const button=document.createElement('button');button.setAttribute('role','menuitem');
    button.disabled=state.clipboardBusy||(action!=='paste'&&!state.selectedRowIds.size);
    const name=document.createElement('span');name.textContent=action[0].toUpperCase()+action.slice(1);
    const shortcut=document.createElement('span');shortcut.className='muted';shortcut.textContent=modifier+key;
    button.append(name,shortcut);button.addEventListener('click',()=>void editWaveformRows(action));menu.append(button);
  }
  menu.hidden=false;menu.style.left=Math.max(0,Math.min(x,innerWidth-menu.offsetWidth-5))+'px';
  menu.style.top=Math.max(0,Math.min(y,innerHeight-menu.offsetHeight-5))+'px';
  menu.querySelector('button:not(:disabled)')?.focus({preventScroll:true});
}
function waveformTextEditing(target){
  const node=target instanceof Element?target:document.activeElement;
  return Boolean(node?.closest('input,textarea,select,[contenteditable]:not([contenteditable="false"])'));
}
function waveformClipboardEvent(event,action){
  if(waveformTextEditing(event.target)||document.activeElement?.closest('#browser'))return false;
  // Linux middle-click can emit a primary-selection paste during 2D panning.
  // That pointer gesture is navigation, never a request to insert waveform rows.
  if(event.type==='paste'&&state.clipboardPointerButton===1){event.preventDefault();return true;}
  if(action!=='paste'&&!state.selectedRowIds.size)return false;
  event.preventDefault();if(event.type==='keydown')event.stopImmediatePropagation();
  void editWaveformRows(action);return true;
}
function waveformEditKey(event){
  if(waveformTextEditing(event.target))return false;
  if(event.defaultPrevented)return true; // VS Code owns an already-routed key.
  const menu=el('rowEditMenu');
  if(!menu.hidden){
    const buttons=[...menu.querySelectorAll('button:not(:disabled)')],index=buttons.indexOf(document.activeElement);
    if(event.key==='Escape'||event.key==='Tab'){
      hideWaveformEditMenu();el('displayRows').focus({preventScroll:true});
      if(event.key==='Escape')event.preventDefault();return true;
    }
    if(['ArrowUp','ArrowDown','Home','End'].includes(event.key)){
      event.preventDefault();buttons[event.key==='Home'?0:event.key==='End'?buttons.length-1:
        (index+(event.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length]?.focus();return true;
    }
    if(event.key.startsWith('Arrow')){event.preventDefault();return true;}
  }
  const action={x:'cut',c:'copy',v:'paste'}[event.key.toLowerCase()];
  if((event.ctrlKey||event.metaKey)&&!event.altKey&&!event.shiftKey&&action)
    return waveformClipboardEvent(event,action);
  if(event.key==='ContextMenu'||(event.shiftKey&&event.key==='F10')){
    event.preventDefault();const row=document.querySelector('.displayRow.selected')||el('displayRows');
    const rect=row.getBoundingClientRect();showWaveformEditMenu(rect.left+35,rect.top+20);return true;
  }
  return false;
}
function bindWaveformClipboard(){
  for(const id of ['displayRows','wavePane']){
    const host=el(id);host.tabIndex=0;
    host.addEventListener('pointerdown',event=>{if(!waveformTextEditing(event.target))host.focus({preventScroll:true});});
    host.addEventListener('contextmenu',event=>{
      if(waveformTextEditing(event.target))return;event.preventDefault();
      const row=event.target.closest('.displayRow');
      const item=id==='displayRows'?row&&findItem(row.dataset.id):waveformItemAtY(event.clientY-event.target.getBoundingClientRect().top);
      if(!item)selectWaveformRow(null);
      else if(!state.selectedRowIds.has(item.id))selectWaveformRow(item.id);
      showWaveformEditMenu(event.clientX,event.clientY);
    });
  }
  for(const action of ['cut','copy','paste'])window.addEventListener(action,event=>waveformClipboardEvent(event,action));
  window.addEventListener('pointerdown',event=>{
    state.clipboardPointerButton=event.button;
    if(!event.target.closest('#rowEditMenu'))hideWaveformEditMenu();
  });
  window.addEventListener('keydown',()=>{state.clipboardPointerButton=null;},true);
  window.addEventListener('blur',()=>{state.clipboardPointerButton=null;hideWaveformEditMenu();});
}
module.exports={clipboardRowIdentities,editWaveformRows,hideWaveformEditMenu,showWaveformEditMenu,
  waveformTextEditing,waveformClipboardEvent,waveformEditKey,bindWaveformClipboard};
