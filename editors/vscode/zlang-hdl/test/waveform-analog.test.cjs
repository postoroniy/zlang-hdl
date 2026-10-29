// SPDX-License-Identifier: Apache-2.0
'use strict';
const assert=require('node:assert/strict');
const {test}=require('node:test');
const vm=require('node:vm');
const numeric=require('../waveform_numeric');
const {formatSignalBits}=require('../waveform_renderer');
const {waveformNumericType,waveformNumericValue,waveformAnalogEnabled,
  waveformAnalogRange,waveformAnalogRatio,waveformDefaultRowHeight}=require('../waveform_numeric');
const {drawWaveformAnalog}=require('../waveform_analog');
const {normalizeWaveformSession,serializeSessionLayout}=require('../waveform_session');
const {encodeWaveformClipboard,pasteWaveformClipboard}=require('../waveform_clipboard');
const {waveformRows}=require('../waveform_rows');
const fixed={id:'sin',name:'output.sine',width:16,layout:{kind:'fixed',canonical:{fraction:14}}};

test('Auto uses typed numerical metadata, never names or guessed bit patterns',()=>{
  assert.equal(waveformAnalogEnabled(fixed),true);
  for(const kind of ['sint','uint','fixed','ufixed'])assert.equal(waveformAnalogEnabled({width:8,layout:{kind,canonical:{fraction:3}}}),true);
  for(const kind of ['bit','bits','enum','struct','tuple','vec','tagged_union'])
    assert.equal(waveformAnalogEnabled({width:8,layout:{kind}}),false);
  assert.equal(waveformAnalogEnabled({width:1,layout:{kind:'uint'}}),false);
  assert.equal(waveformAnalogEnabled({name:'sine_fixed_16_14',width:16,kind:'wire'}),false);
  assert.equal(waveformAnalogEnabled(fixed,{display:'digital'}),false);
  assert.equal(waveformAnalogEnabled(fixed,{},'digital'),false);
  assert.equal(waveformAnalogEnabled({width:16,kind:'wire'},{display:'analog'}),true);
  assert.equal(waveformAnalogEnabled({width:16,layout:{kind:'struct'}},{display:'analog'}),false);
  assert.equal(waveformAnalogEnabled({...fixed,layout:{kind:'fixed'}}),false);
});
test('fixed-point signedness and scales decode CORDIC outputs automatically',()=>{
  for(const [bits,number] of [['0100000000000000',1],['1100000000000000',-1],['1111111111111101',-3/16384]])
    assert.equal(waveformNumericValue(bits,fixed),number);
  assert.equal(waveformNumericValue('1100000000000000',fixed,{analog:{signed:false}}),3);
  assert.equal(waveformNumericValue('1100000000000000',fixed,{analog:{fraction:0}}),-16384);
  assert.equal(waveformNumericType(fixed).fraction,14);
  assert.equal(waveformNumericValue('11111111',{width:8,kind:'wire'},{format:'signed'}),-1);
});
test('typed analog value cells show scaled decimals while exact digital text remains available',()=>{
  const state={display:'auto',format:'typed'};
  const format=vm.runInNewContext('('+formatSignalBits.toString()+')',{
    ...numeric,state,formatBits:()=> 'raw',
  });
  assert.equal(format('1100000000000000',fixed,{},'-16384/2^14 (raw bits)'),'-1');
  state.display='digital';
  assert.equal(format('1100000000000000',fixed,{},'-16384/2^14 (raw bits)'),'-16384/2^14 (raw bits)');
  assert.equal(format('real:-2.5',{kind:'real',width:1},{},undefined),'-2.5');
});
test('explicit saved-file type hints work only with matching widths',()=>{
  const signal={width:16,kind:'wire',type_hint:'fixed<16,14>'};
  assert.equal(waveformAnalogEnabled(signal),true);
  assert.equal(waveformNumericValue('1100000000000000',signal),-1);
  assert.equal(waveformAnalogEnabled({...signal,width:8}),false);
  assert.equal(waveformNumericValue('11111111',{width:8,type_hint:'s8'}),-1);
});
test('small signed/unsigned fixed encodings agree with an independent integer oracle',()=>{
  for(let width=1;width<=8;width++)for(let fraction=0;fraction<width;fraction++)
    for(const signed of [false,true])for(let raw=0;raw<2**width;raw++){
      const n=signed && raw>=2**(width-1)?raw-2**width:raw;
      const signal={width,layout:{kind:signed?'fixed':'ufixed',canonical:{fraction}}};
      assert.equal(waveformNumericValue(raw.toString(2).padStart(width,'0'),signal),n/2**fraction);
    }
});
test('unknown, inactive and unrecorded samples never become zero',()=>{
  for(const bits of [null,'','000u','00x0','z001','----','????'])
    assert.equal(waveformNumericValue(bits,{width:4,layout:{kind:'uint'}}),null);
});
test('VCD/FST real values and finite extremes remain numeric without overflow',()=>{
  const signal={kind:'real',width:64};
  assert.equal(waveformAnalogEnabled(signal),true);
  assert.equal(waveformNumericValue('real:-1.25e-3',signal),-0.00125);
  for(const bits of ['real:NaN','real:inf','real:Infinity','real:','real:0x10','real:1e500','real:'+'1'.repeat(10000)+'!'])
    assert.equal(waveformNumericValue(bits,signal),null);
  const range=waveformAnalogRange([-Number.MAX_VALUE,Number.MAX_VALUE]);
  assert.equal(waveformAnalogRatio(0,range),0.5);
  assert.ok(Number.isFinite(waveformAnalogRange([Number.MAX_VALUE]).max));
  const huge={width:4096,layout:{kind:'ufixed',canonical:{fraction:4000}}};
  assert.equal(waveformNumericValue('1'+'0'.repeat(4095),huge),2**95);
  const tiny={width:1100,layout:{kind:'ufixed',canonical:{fraction:1080}}};
  assert.equal(waveformNumericValue('0'.repeat(1047)+'1'+'0'.repeat(52),tiny),2**-1028);
});
test('display modes and per-row analog overrides round-trip through sessions and clipboard',()=>{
  const item={id:'a',type:'signal',signalId:'sin',display:'analog',analog:{signed:true,fraction:14}};
  const encoded=serializeSessionLayout([item],new Map([['sin',fixed]]));
  const session={schema:'zlang-waveform-session-v1',display:'digital',items:encoded,viewports:[{start:0,end:10}]};
  const restored=normalizeWaveformSession(session,[fixed],0,10);
  assert.equal(restored.display,'digital');assert.deepEqual(restored.items[0].analog,item.analog);
  const copy=pasteWaveformClipboard(encoded,encodeWaveformClipboard(encoded),[fixed],'a',()=> 'copy');
  assert.equal(copy.items[1].display,'analog');assert.deepEqual(copy.items[1].analog,item.analog);
  assert.equal(normalizeWaveformSession({...session,display:undefined},[fixed],0,10).display,'auto');
  for(const analog of [null,[],{signed:'yes'},{fraction:-1},{fraction:16},{fraction:1.1},{fraction:NaN},{bad:1}])
    assert.throws(()=>normalizeWaveformSession({...session,items:[{...encoded[0],analog}]},[fixed],0,10));
  assert.throws(()=>normalizeWaveformSession({...session,display:'invalid'},[fixed],0,10));
});
test('analog row geometry uses one shared default and respects explicit user heights',()=>{
  const height=item=>waveformDefaultRowHeight(item,fixed);
  const rows=waveformRows([{type:'signal'},{type:'signal',height:100},{type:'signal',display:'digital'}],height).rows;
  assert.deepEqual(rows.map(r=>[r.top,r.height]),[[0,72],[72,100],[172,34]]);
});

function canvas(){
  const strokes=[],fills=[],labels=[];let path=[];
  return {strokes,fills,labels,globalAlpha:1,strokeStyle:'',save(){},restore(){},setLineDash(){},beginPath(){path=[];},
    moveTo(x,y){path.push([x,y]);},lineTo(x,y){path.push([x,y]);},stroke(){strokes.push({color:this.strokeStyle,path});},
    fillRect(...args){fills.push(args);},fillText(text){labels.push(text);},measureText(text){return {width:String(text).length*5};}};
}
const theme={wave:'green',grid:'gray',bg:'black',muted:'white',u:'blue',x:'red',z:'orange'};
test('analog canvas preserves time holds and breaks paths across U/X/inactive gaps',()=>{
  const ctx=canvas(),signal={width:2,layout:{kind:'uint'}};
  drawWaveformAnalog(ctx,{},signal,{exact:true},[{event:0,bits:'00'},{event:2,bits:'xx'},{event:4,bits:'11'}],
    {start:0,end:6},600,0,72,theme,time=>time*100);
  const wave=ctx.strokes.filter(s=>s.color==='green');
  assert.deepEqual(wave.map(s=>s.path.map(p=>p[0])),[[0,200],[400,600]]);
  assert.ok(ctx.labels.includes('X'));
});
test('dense activity keeps a narrow pulse visible even with equal bucket endpoints',()=>{
  const ctx=canvas();
  drawWaveformAnalog(ctx,{},fixed,{exact:false,buckets:[{start:0,end:10,first_bits:'0'.repeat(16),last_bits:'0'.repeat(16),transitions:2}]},[],
    {start:0,end:10},100,0,72,theme,time=>time*10);
  assert.ok(ctx.fills.some(rect=>rect[0]===0&&rect[1]===7&&rect[2]===100&&rect[3]===58));
  assert.ok(ctx.labels.includes('dense activity · zoom in'));
  assert.equal(ctx.strokes.filter(s=>s.color==='green').length,0);
});
