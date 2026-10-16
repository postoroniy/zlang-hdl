// SPDX-License-Identifier: Apache-2.0
'use strict';

// Presentation only: edge counts and occupancy durations come from Rust.
function waveformBusDelta(valueA,valueB,width,signed=false) {
  if(!/^[01]+$/.test(valueA||'')||!/^[01]+$/.test(valueB||''))return null;
  const decode=bits=>{
    let value=BigInt('0b'+bits);
    if(signed&&(value&(1n<<BigInt(width-1))))value-=1n<<BigInt(width);
    return value;
  };
  return String(decode(valueB)-decode(valueA));
}
function waveformTimeScale(metadata) {
  if(metadata?.time_domain!=='physical')return null;
  const match=String(metadata.timescale||'').match(/^\s*(\d+)\s+(attoseconds|femtoseconds|picoseconds|nanoseconds|microseconds|milliseconds|seconds|as|fs|ps|ns|us|ms|s)/i);
  if(!match)return null;
  const factors={attoseconds:1e-18,as:1e-18,femtoseconds:1e-15,fs:1e-15,picoseconds:1e-12,ps:1e-12,nanoseconds:1e-9,ns:1e-9,microseconds:1e-6,us:1e-6,milliseconds:1e-3,ms:1e-3,seconds:1,s:1};
  return Number(match[1])*factors[match[2].toLowerCase()];
}
function waveformMeasurement(statistics,cursorA,cursorB,metadata) {
  if(!statistics)return null;
  const delta=Math.abs(cursorB-cursorA),reverse=cursorB<cursorA;
  const valueA=reverse?statistics.end_bits:statistics.start_bits;
  const valueB=reverse?statistics.start_bits:statistics.end_bits;
  const seconds=waveformTimeScale(metadata);
  return {
    delta,eventDelta:metadata.time_domain==='zlang_event_ticks'?delta/(metadata.ticks_per_event||2):null,
    seconds:seconds===null?null:seconds*delta,
    reciprocalHz:seconds!==null&&delta>0?1/(seconds*delta):null,
    valueA,valueB,transitions:statistics.transitions,rising:statistics.rising,falling:statistics.falling,
    highTicks:statistics.high_ticks,lowTicks:statistics.low_ticks,unknownTicks:statistics.unknown_ticks,
    dutyCycle:statistics.width===1&&delta>0&&statistics.unknown_ticks===0?statistics.high_ticks/delta:null,
    unsignedDelta:waveformBusDelta(valueA,valueB,statistics.width),
    signedDelta:waveformBusDelta(valueA,valueB,statistics.width,true),
  };
}
module.exports={waveformBusDelta,waveformTimeScale,waveformMeasurement};
