// SPDX-License-Identifier: Apache-2.0
'use strict';
const assert=require('node:assert/strict');
const {test}=require('node:test');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {assertCleanTransportLogs, retainHostFailure}=require('./run-host-smoke.cjs');

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

test('failed host retains watcher/LSP logs outside disposable TMPDIR but not workspace or extension files', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'host-retain.'));
  t.after(() => fs.rmSync(root, {recursive:true, force:true}));
  const host = path.join(root, 'temporary-host'), destination = path.join(root, 'retained');
  const logs = path.join(host, 'user-data', 'logs', 'session');
  fs.mkdirSync(logs, {recursive:true});
  fs.mkdirSync(path.join(host, 'workspace'));
  fs.writeFileSync(path.join(host, 'workspace', 'source.zhl'), 'do not retain');
  fs.writeFileSync(path.join(logs, 'lsp.log'), 'workspace/didChangeWatchedFiles');
  const vsix = path.join(root, 'test.vsix'); fs.writeFileSync(vsix, 'test package');
  const evidence = retainHostFailure(host, vsix, Error('lock recheck timeout'), destination);
  fs.rmSync(host, {recursive:true, force:true});
  assert.deepEqual(fs.readdirSync(evidence).sort(), ['failure.json', 'logs']);
  assert.equal(fs.readFileSync(path.join(evidence, 'logs/session/lsp.log'), 'utf8'),
    'workspace/didChangeWatchedFiles');
  const summary = JSON.parse(fs.readFileSync(path.join(evidence, 'failure.json')));
  assert.match(summary.error, /lock recheck timeout/);
  assert.match(summary.sha256, /^[0-9a-f]{64}$/);
});
