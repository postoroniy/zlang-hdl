// SPDX-License-Identifier: Apache-2.0
'use strict';
const assert=require('node:assert/strict');
const {test}=require('node:test');
const {selectedWaveformRows,encodeWaveformClipboard,parseWaveformClipboard,
  pasteWaveformClipboard,MAX_WAVEFORM_CLIPBOARD_BYTES}=require('../waveform_clipboard');

const catalog=[{id:'q',name:'Top.q',width:8}];
const row=(id='a')=>({type:'signal',id,signal:{name:'Top.q',width:8},color:'#ff6b6b',height:70,format:'signed',mnemonic:{'00000000':'Idle'}});
function ids(){let next=0;return()=> 'pasted-'+(++next);}
test('clipboard copies presentation, allocates fresh row IDs and preserves input layouts',()=>{
  const items=[row()],before=JSON.stringify(items),text=encodeWaveformClipboard(items);
  const pasted=pasteWaveformClipboard(items,text,catalog,'a',ids());
  assert.equal(JSON.stringify(items),before);assert.equal(pasted.items.length,2);
  assert.equal(pasted.items[1].signalId,'q');assert.equal(pasted.items[1].id,'pasted-1');
  for(const key of ['color','height','format'])assert.deepEqual(pasted.items[1][key],items[0][key]);
  assert.deepEqual(Object.entries(pasted.items[1].mnemonic),Object.entries(items[0].mnemonic));
  assert.equal(Object.getPrototypeOf(pasted.items[1].mnemonic),null);
  pasted.items[1].mnemonic['00000000']='Changed';assert.equal(items[0].mnemonic['00000000'],'Idle');
  assert.deepEqual(pasted.roots,['pasted-1']);assert.equal(text.includes('signalId'),false);
  assert.equal(encodeWaveformClipboard([{...row(),holder:{samples:['secret']}}]).includes('secret'),false);
});
test('groups and dividers copy once with parent-selected children and preserve order',()=>{
  const group={type:'group',id:'g',name:'Datapath',children:[row('b'),{type:'divider',id:'d',name:'Stage'}]};
  assert.deepEqual(selectedWaveformRows([row(),group],new Set(['a','g','b'])),[row(),group]);
  const text=encodeWaveformClipboard([group]);
  const pasted=pasteWaveformClipboard([row()],text,catalog,'a',ids());
  assert.equal(pasted.items[1].type,'group');
  assert.deepEqual(pasted.items[1].children.map(item=>item.type),['signal','divider']);
  assert.equal(new Set(pasted.ids).size,3);
});
test('paste targets after rows, inside groups or the end without a selection',()=>{
  const text=encodeWaveformClipboard([row()]);
  const group={type:'group',id:'g',name:'Target',collapsed:true,children:[]};
  const inside=pasteWaveformClipboard([group],text,catalog,'g',ids());
  assert.equal(inside.items[0].collapsed,false);assert.equal(inside.items[0].children[0].signalId,'q');
  const end=pasteWaveformClipboard([row()],text,catalog,null,ids());
  assert.equal(end.items.at(-1).id,'pasted-1');
});
test('cross-trace paste rebinds exact names and widths, preserving missing holder identities',()=>{
  const text=encodeWaveformClipboard([row()]);
  const matched=pasteWaveformClipboard([],text,[{...catalog[0],id:'other'}],null,ids());
  assert.equal(matched.items[0].signalId,'other');
  const missing=pasteWaveformClipboard([],text,[{...catalog[0],width:9}],null,ids());
  assert.equal(missing.items[0].signalId,null);assert.deepEqual(missing.items[0].unresolved,row().signal);
});
test('clipboard rejects hostile, malformed, empty and oversized content',()=>{
  const valid=encodeWaveformClipboard([row()]);
  for(const text of ['plain text','{}',valid.replace('rows-v1','rows-v99'),
    JSON.stringify({schema:'zlang-waveform-rows-v1',items:[]}),
    JSON.stringify({schema:'zlang-waveform-rows-v1',items:[row(),row()]}),
    valid.replace('#ff6b6b','url(evil)'), 'x'.repeat(MAX_WAVEFORM_CLIPBOARD_BYTES+1)])
    assert.throws(()=>parseWaveformClipboard(text));
});
test('combined signal and depth caps fail atomically before paste changes a layout',()=>{
  const text=encodeWaveformClipboard([row()]);
  const full=Array.from({length:4096},(_,index)=>row('r'+index));
  assert.throws(()=>pasteWaveformClipboard(full,text,catalog,null,ids()),/4096 signal rows/);
  assert.equal(full.length,4096);
  let group={type:'group',id:'deep',name:'Deep',children:[]};
  for(let i=0;i<7;i++)group={type:'group',id:'level'+i,name:'Level',children:[group]};
  const nested=encodeWaveformClipboard([{type:'group',id:'new',children:[row()]}]);
  const before=JSON.stringify(group);
  assert.throws(()=>pasteWaveformClipboard([group],nested,catalog,'deep',ids()),/group nesting/);
  assert.equal(JSON.stringify(group),before);
});
test('clipboard size cap also includes fields added by canonical normalization',()=>{
  const items=Array.from({length:4096},(_,i)=>({type:'signal',id:'r'+i,signal:{name:'x'.repeat(181),width:8}}));
  assert.ok(JSON.stringify({schema:'zlang-waveform-rows-v1',items}).length<MAX_WAVEFORM_CLIPBOARD_BYTES);
  assert.throws(()=>encodeWaveformClipboard(items),/exceeds 1 MiB/);
});
