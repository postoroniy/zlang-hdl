// SPDX-License-Identifier: Apache-2.0
'use strict';
const {waveformNumericValue,waveformNumericLabel,waveformAnalogRange,
  waveformAnalogRatio}=require('./waveform_numeric');

// Draw only fetched samples. A step is a held digital value, not an inferred
// interpolation or an assertion that a ready/valid payload was transferred.
function drawWaveformAnalog(ctx,item,signal,rendered,values,view,width,y,rowSize,theme,toX) {
  const dense=rendered && !rendered.exact;
  const points=[];let prior;
  for (const point of values) {
    if (point.event < view.start) prior=point;
    else if (point.event <= view.end) points.push(point);
  }
  if (prior) points.unshift(prior);
  const samples=dense?(rendered.buckets||[]).flatMap(b=>[b.first_bits,b.last_bits])
    :points.map(p=>p.bits);
  const numeric=samples.map(bits=>waveformNumericValue(bits,signal,item));
  const range=waveformAnalogRange(numeric);
  const top=y+7,bottom=y+rowSize-7;
  const level=value=>bottom-waveformAnalogRatio(value,range)*(bottom-top);
  const color=item.color||theme.wave;
  ctx.save();ctx.lineWidth=1.6;ctx.strokeStyle=color;ctx.fillStyle=color;
  if (range.min<=0 && range.max>=0) {
    ctx.strokeStyle=theme.grid;ctx.setLineDash([3,3]);ctx.beginPath();
    ctx.moveTo(toX(view.start,width,view),level(0));ctx.lineTo(toX(view.end,width,view),level(0));ctx.stroke();
    ctx.setLineDash([]);ctx.strokeStyle=color;
  }
  const gap=(bits,x1,x2)=>{
    const label=/u/i.test(bits||'')?'U':/x/i.test(bits||'')?'X':/z/i.test(bits||'')?'Z'
      :/^-+$/.test(bits||'')?'inactive':/^\?+$/.test(bits||'')?'unknown variant':'not numeric';
    ctx.save();ctx.fillStyle=label==='U'?theme.u:label==='X'?theme.x:label==='Z'?theme.z:theme.muted;
    ctx.globalAlpha*=0.15;ctx.fillRect(x1,top,Math.max(1,x2-x1),bottom-top);ctx.restore();
    if(x2-x1>38){ctx.fillStyle=theme.muted;ctx.font='10px sans-serif';ctx.fillText(label,x1+3,top+2);ctx.fillStyle=color;}
  };
  if (dense) {
    for(const [index,bucket] of (rendered.buckets||[]).entries()) {
      const x1=toX(bucket.start,width,view),x2=toX(bucket.end,width,view);
      const first=numeric[index*2],last=numeric[index*2+1];
      // v3 buckets contain activity and endpoints, not numeric extrema.
      // A full-height activity band retains subpixel events without inventing
      // min/max values or hiding a pulse whose endpoints happen to be equal.
      if(bucket.transitions){ctx.save();ctx.globalAlpha*=0.24;ctx.fillRect(x1,top,Math.max(1,x2-x1),bottom-top);ctx.restore();}
      if(bucket.has_u||bucket.has_x||bucket.has_z) gap(bucket.has_x?'x':bucket.has_u?'u':'z',x1,x2);
      if(!bucket.transitions && first!==null && last!==null && !bucket.has_u && !bucket.has_x && !bucket.has_z){
        ctx.beginPath();ctx.moveTo(x1,level(last));ctx.lineTo(x2,level(last));ctx.stroke();
      }
    }
    ctx.fillStyle=theme.muted;ctx.font='10px sans-serif';ctx.fillText('dense activity · zoom in',toX(view.start,width,view)+4,top+2);
  } else {
    let previous=null;
    for(let i=0;i<points.length;i++) {
      const point=points[i],value=numeric[i];
      const x1=toX(Math.max(view.start,point.event),width,view);
      const x2=toX(points[i+1]?.event??view.end,width,view);
      if(value===null){gap(point.bits,x1,x2);previous=null;continue;}
      ctx.beginPath();
      if(previous!==null && point.event>=view.start){ctx.moveTo(x1,level(previous));ctx.lineTo(x1,level(value));}
      ctx.moveTo(x1,level(value));ctx.lineTo(x2,level(value));ctx.stroke();previous=value;
    }
  }
  ctx.font='10px monospace';ctx.textAlign='right';ctx.textBaseline='top';
  for(const [value,at] of [[range.max,top],[range.min,bottom-10]]) {
    const label=waveformNumericLabel(value),x=toX(view.end,width,view)-3;
    ctx.fillStyle=theme.bg;ctx.fillRect(x-ctx.measureText(label).width-2,at,ctx.measureText(label).width+4,11);
    ctx.fillStyle=theme.muted;ctx.fillText(label,x,at);
  }
  ctx.restore();
}
module.exports={drawWaveformAnalog};
