// SPDX-License-Identifier: Apache-2.0
'use strict';
const fs=require('node:fs');
function writeScaleVcd(filename,signals=10000,events=1000000){
  const file=fs.openSync(filename,'wx');
  try{
    let header='$timescale 1ps $end\n$scope module Large $end\n';
    for(let bank=0;bank<Math.ceil(signals/100);bank++){
      header+='$scope module Bank'+bank+' $end\n';
      for(let local=0;local<100&&bank*100+local<signals;local++){
        const index=bank*100+local;header+='$var wire 1 s'+index+' signal'+index+' $end\n';
      }
      header+='$upscope $end\n';
    }
    header+='$upscope $end\n$enddefinitions $end\n#0\n';
    for(let index=0;index<signals;index++)header+='0s'+index+'\n';
    fs.writeSync(file,header);
    for(let offset=1;offset<events;offset+=8192){
      let chunk='';const end=Math.min(events,offset+8192);
      for(let time=offset;time<end;time++)chunk+='#'+time+'\n'+(time%2)+'s0\n';
      fs.writeSync(file,chunk);
    }
  }finally{fs.closeSync(file);}
}
module.exports={writeScaleVcd};
