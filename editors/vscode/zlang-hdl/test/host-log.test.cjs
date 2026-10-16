// SPDX-License-Identifier: Apache-2.0
'use strict';
const assert=require('node:assert/strict');
const {test}=require('node:test');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {assertCleanTransportLogs}=require('./run-host-smoke.cjs');

test('a green host exit cannot hide SIGPIPE or EPIPE in nested VS Code logs',t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'host-log-test.'));
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  assert.throws(()=>assertCleanTransportLogs(root),/did not create/);
  const logs=path.join(root,'logs','session','window1','exthost');
  fs.mkdirSync(logs,{recursive:true});
  const file=path.join(logs,'exthost.log');
  fs.writeFileSync(file,'debugger and waveform passed\n');
  assert.doesNotThrow(()=>assertCleanTransportLogs(root));
  for(const error of ['Error: Unexpected SIGPIPE','Error: write EPIPE','Error: EPIPE','DAP session is disconnected']){
    fs.writeFileSync(file,'debugger and waveform passed\n'+error+'\n');
    assert.throws(()=>assertCleanTransportLogs(root),/transport failed/);
  }
});
