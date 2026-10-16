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
    if(item.type==='signal'&&state.selectedRowIds.has(item.id))Object.assign(item,patch);
  });
  renderDisplay();renderViewports();save();
}
function renderRowPresentation(host,item) {
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
  input.value=String(item.height||34);input.setAttribute('aria-label','Row height');
  const units=document.createElement('span');units.className='muted';units.textContent='px';
  const apply=()=>{if(input.value!==''&&input.checkValidity()){
    setRowPresentation({height:Number(input.value)});scheduleQuery();
  }};
  input.addEventListener('input',apply);
  input.addEventListener('change',()=>{
    if(!input.checkValidity()||input.value==='')input.value=String(item.height||34);
    apply();
  });
  input.addEventListener('blur',()=>setTimeout(renderInspector,0));
  height.append(caption,input,units);host.append(height);
}
module.exports={WAVEFORM_PALETTE,setRowPresentation,renderRowPresentation};
