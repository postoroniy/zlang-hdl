// SPDX-License-Identifier: Apache-2.0
// Per-row appearance controls; channel samples and queries are not owned here.
'use strict';
const WAVEFORM_PALETTE=[
  ['Green','#77d84b'],['Lime','#b9e34b'],['Teal','#35c5a3'],['Cyan','#56dce4'],
  ['Sky','#66b8ff'],['Blue','#7194ff'],['Violet','#a68cff'],['Purple','#cb8cff'],
  ['Magenta','#ed83d3'],['Pink','#ff9eb5'],['Red','#ff6b6b'],['Orange','#ff9d45'],
  ['Amber','#e9b949'],['Yellow','#f3e66b'],['White','#e6edf3'],['Gray','#9ba8b4'],
];

function setRowPresentation(patch) {
  walk(state.layout,item=>{
    if(item.type==='signal'&&state.selectedRowIds.has(item.id))Object.assign(item,typeof patch==='function'?patch(item):patch);
  });
  renderDisplay();renderViewports();save();
}
function renderRowPresentation(host,item) {
  renderAnalogPresentation(host,item);
  const label=document.createElement('div');label.className='presentationLabel';
  const title=document.createElement('span');title.textContent='Color';
  const current=document.createElement('span');current.className='muted';
  current.textContent=item.color||'Default';
  const reset=document.createElement('button');reset.textContent='Default';reset.title='Reset selected row colors';
  reset.addEventListener('click',()=>{setRowPresentation({color:undefined});renderInspector();});
  label.append(title,current,reset);host.append(label);
  const palette=document.createElement('div');palette.className='colorPalette';
  palette.setAttribute('role','group');palette.setAttribute('aria-label','Signal color');
  for(const [name,color] of WAVEFORM_PALETTE){
    const button=document.createElement('button');button.className='colorChip';
    button.style.backgroundColor=color;button.dataset.color=color;
    button.title=name+' '+color;button.setAttribute('aria-label',name);
    button.setAttribute('aria-pressed',String((item.color||colors.wave).toLowerCase()===color));
    button.addEventListener('click',()=>{
      setRowPresentation({color});renderInspector();
      el('inspectorBody').querySelector('[data-color="'+color+'"]').focus({preventScroll:true});
    });
    palette.append(button);
  }
  host.append(palette);
  const height=document.createElement('label');height.className='heightControl';
  const caption=document.createElement('span');caption.textContent='Row height';
  const input=document.createElement('input');input.type='number';input.min='24';input.max='128';input.step='1';
  input.value=String(item.height||waveformDefaultRowHeight(item,signalFor(item),state.display));input.setAttribute('aria-label','Row height');
  const units=document.createElement('span');units.className='muted';units.textContent='px';
  const apply=()=>{if(input.value!==''&&input.checkValidity()){
    setRowPresentation({height:Number(input.value)});scheduleQuery();
  }};
  input.addEventListener('input',apply);
  input.addEventListener('change',()=>{
    if(!input.checkValidity()||input.value==='')input.value=String(item.height||waveformDefaultRowHeight(item,signalFor(item),state.display));
    apply();
  });
  input.addEventListener('blur',()=>setTimeout(renderInspector,0));
  height.append(caption,input,units);host.append(height);
}
function renderAnalogPresentation(host,item) {
  const selected=signalFor(item),type=waveformNumericType(selected,item);
  const label=document.createElement('div');label.className='muted';
  const analog=waveformAnalogEnabled(selected,item,state.display);
  label.textContent=analog?'Analog · '+(type.real?'real':(type.signed?'signed':'unsigned')+' · '+type.fraction+' fractional bits'):'Digital';
  host.append(label);
  if(!analog)return;
  const readout=document.createElement('div');readout.id='analogCursorValue';readout.className='metric';
  readout.textContent=waveformAnalogCursorText(item);host.append(readout);
  if(type.real)return;
  const signed=document.createElement('label');signed.className='heightControl';
  const caption=document.createElement('span');caption.textContent='Interpretation';
  const choice=document.createElement('select');choice.setAttribute('aria-label','Analog interpretation');
  choice.title='Auto uses the declared type; untyped buses are unsigned';
  for(const [value,name] of [['auto','Auto'],['signed','Signed'],['unsigned','Unsigned']]){
    const option=document.createElement('option');option.value=value;option.textContent=name;choice.append(option);
  }
  choice.value=item.analog?.signed===undefined?'auto':item.analog.signed?'signed':'unsigned';
  const update=(key,value)=>{
    setRowPresentation(row=>{const analog={...row.analog};if(value===undefined)delete analog[key];else analog[key]=value;return {analog};});
    renderInspector();scheduleQuery();
  };
  choice.addEventListener('change',()=>update('signed',choice.value==='auto'?undefined:choice.value==='signed'));
  signed.append(caption,choice);host.append(signed);
  const fraction=document.createElement('label');fraction.className='heightControl';
  const title=document.createElement('span');title.textContent='Fraction bits';
  const input=document.createElement('input');input.type='number';input.min='0';input.max=String(Math.min(4095,(selected?.width||1)-1));input.step='1';
  input.placeholder='Auto';input.value=item.analog?.fraction===undefined?'':String(item.analog.fraction);
  input.setAttribute('aria-label','Analog fraction bits');input.title='Blank uses typed scale; raw VCD/FST defaults to zero';
  input.addEventListener('change',()=>{if(input.value===''||input.checkValidity())update('fraction',input.value===''?undefined:Number(input.value));});
  fraction.append(title,input);host.append(fraction);
}
function waveformAnalogCursorText(item) {
  const value=waveformNumericValue(cursorValue(item.signalId),signalFor(item),item);
  return 'A: '+(value===null?'unavailable / unknown':waveformNumericLabel(value));
}
function renderAnalogCursorValue() {
  const readout=el('analogCursorValue'),item=findItem(state.selectedRowId);
  if(readout&&item?.type==='signal')readout.textContent=waveformAnalogCursorText(item);
}
module.exports={WAVEFORM_PALETTE,setRowPresentation,renderRowPresentation,renderAnalogPresentation,
  waveformAnalogCursorText,renderAnalogCursorValue};
