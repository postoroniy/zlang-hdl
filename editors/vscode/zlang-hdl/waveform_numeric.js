// SPDX-License-Identifier: Apache-2.0
'use strict';

// Numeric interpretation is presentation metadata, never simulator semantics.
function waveformNumericType(signal, item = {}) {
  const layout = signal?.layout, canonical = layout?.canonical;
  let kind = layout?.kind || signal?.kind || '';
  let hintedFraction;
  if (!layout && signal?.type_hint) {
    // Explicit type hints only; signal names and bit patterns are not types.
    const hint=String(signal.type_hint).replace(/\s+/g,'');
    const fixedHint=/^(fixed|ufixed)<(\d+),(\d+)>$/.exec(hint);
    const integerHint=/^(uint|sint)<(\d+)>$/.exec(hint)||/^([us])(\d+)$/.exec(hint);
    if(fixedHint && Number(fixedHint[2])===signal.width){kind=fixedHint[1];hintedFraction=Number(fixedHint[3]);}
    else if(integerHint && Number(integerHint[2])===signal.width)
      kind=['s','sint'].includes(integerHint[1])?'sint':'uint';
  }
  const real = ['real','realtime','shortreal'].includes(kind);
  const fixed = kind === 'fixed' || kind === 'ufixed';
  const numeric = real || fixed || ['uint','sint'].includes(kind);
  const scalar = !layout || ['bit','bits','uint','sint','fixed','ufixed','enum'].includes(kind);
  let fraction = fixed ? canonical?.fraction ?? hintedFraction : 0;
  if (item.analog?.fraction !== undefined) fraction = item.analog.fraction;
  const signed = item.analog?.signed ?? (kind === 'sint' || kind === 'fixed'
    || (!layout && item.format === 'signed'));
  return {real, numeric, fixed, scalar:real || scalar, signed, fraction,
    valid:real || (Number.isInteger(fraction) && fraction >= 0 && fraction < (signal?.width || 1))};
}
function waveformAnalogEnabled(signal, item = {}, display = 'auto') {
  const mode = item.display || display;
  if (mode === 'digital') return false;
  const type = waveformNumericType(signal,item);
  return type.scalar && type.valid && (mode === 'analog'
    || (type.numeric && (type.real || type.fixed || signal?.width > 1)));
}
function waveformNumericValue(bits, signal, item = {}) {
  const type = waveformNumericType(signal,item);
  if (!type.valid || !type.scalar || typeof bits !== 'string') return null;
  if (type.real) {
    if (!bits.startsWith('real:')) return null;
    const text=bits.slice(5);
    if (text.length>128 || !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(text)) return null;
    const value=Number(text);return Number.isFinite(value)?value:null;
  }
  if (bits.length !== signal.width || bits.length > 4096 || !/^[01]+$/.test(bits)) return null;
  let raw=BigInt('0b'+bits);
  if (type.signed && bits[0]==='1') raw-=1n<<BigInt(signal.width);
  const negative=raw<0n, magnitude=negative?-raw:raw;
  // Scale before floating conversion so large fixed-point values do not
  // overflow merely because their *raw* integer is wider than binary64.
  const length=magnitude.toString(2).length,shift=Math.max(0,length-53);
  const leading=Math.min(52,length-1);
  const value=(Number(magnitude>>BigInt(shift))/2**leading)*2**(shift+leading-type.fraction);
  if (!Number.isFinite(value) || (value===0 && magnitude!==0n)) return null;
  return negative?-value:value;
}
function waveformNumericLabel(value) {
  if (!Number.isFinite(value)) return '—';
  return String(Number(value.toPrecision(6)));
}
function waveformAnalogRange(values) {
  let min=Infinity,max=-Infinity;
  for (const value of values) if (value!==null && Number.isFinite(value)) {
    min=Math.min(min,value);max=Math.max(max,value);
  }
  if (min===Infinity) return {min:-1,max:1};
  if (min===max) {
    return min===0?{min:-1,max:1}:{min:Math.min(0,min),max:Math.max(0,max)};
  }
  // Coordinates use a normalized ratio, avoiding max-min overflow.
  return {min,max};
}
function waveformAnalogRatio(value, range) {
  const scale=Math.max(Math.abs(range.min),Math.abs(range.max),Number.MIN_VALUE);
  return (value/scale-range.min/scale)/(range.max/scale-range.min/scale);
}
function waveformDefaultRowHeight(item,signal,display='auto') {
  return waveformAnalogEnabled(signal,item,display)?72:34;
}
module.exports={waveformNumericType,waveformAnalogEnabled,waveformNumericValue,
  waveformNumericLabel,waveformAnalogRange,waveformAnalogRatio,waveformDefaultRowHeight};
