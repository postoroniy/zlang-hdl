// SPDX-License-Identifier: Apache-2.0
'use strict';
const assert=require('node:assert/strict');
const {test}=require('node:test');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {vscodeDownloadOptions, assertCleanTransportLogs, retainHostFailure}=require('./run-host-smoke.cjs');

test('a green host exit cannot hide SIGPIPE or EPIPE in nested VS Code logs',t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'host-log-test.'));
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  assert.throws(()=>assertCleanTransportLogs(root),/did not create/);
  const logs=path.join(root,'logs','session','window1','exthost');
  fs.mkdirSync(logs,{recursive:true});
  const file=path.join(logs,'exthost.log');
  fs.writeFileSync(file,'debugger and waveform passed\n');
  assert.doesNotThrow(()=>assertCleanTransportLogs(root));
  for(const error of ['Error: Unexpected SIGPIPE','Error: write EPIPE','Error: EPIPE','DAP session is disconnected',
    'compiler integration failed; see the language-server log',
    'compiler worker exceeded the bounded request time',
    'compiler worker exited unexpectedly']){
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

test('shared host cache changes neither the pinned version nor the default download options', () => {
  const version = require('../editor-toolchain.json').vscodeStable.version;
  assert.deepEqual(vscodeDownloadOptions({}), {version});
  const cachePath = path.join(os.tmpdir(), 'qualified-vscode-cache');
  assert.deepEqual(vscodeDownloadOptions({ZLANG_VSCODE_CACHE_PATH:cachePath}),
    {version, cachePath});
  for (const cache of ['', 'relative-cache']) {
    assert.throws(() => vscodeDownloadOptions({ZLANG_VSCODE_CACHE_PATH:cache}),
      /must be absolute/);
  }
});


test('diagnostic barrier requires the unique error then a fresh repair publication', async () => {
  const {checkDiagnosticRoundTrip} = require('./diagnostic-barrier.cjs');
  const original = 'module Original { in x:u8 out y:u8=x }\n';
  const error = {severity:0, message:"unknown type 'HostSmokeDiagnosticBarrierMissingType'"};
  for (const mode of ['normal', 'stale-empty', 'wrong-error', 'missing-repair']) {
    let text = original, observedError = false;
    const edits = [];
    const run = checkDiagnosticRoundTrip({
      readText: () => text,
      replace: async value => {text=value; edits.push(value);},
      diagnostics: () => {
        if (mode === 'stale-empty') return [];
        if (mode === 'wrong-error') return [{...error, message:'unrelated failure'}];
        if (text !== original || mode === 'missing-repair') return [error];
        return [];
      },
      wait: async (description, predicate) => {
        assert.equal(predicate(), true, description);
        if (description === 'fresh diagnostic barrier error') observedError = true;
      },
      errorSeverity:0,
    });
    if (mode === 'normal') {await run; assert.equal(observedError, true);}
    else await assert.rejects(run, /fresh diagnostic barrier (?:error|repair)/);
    assert.equal(text, original);
    assert.equal(edits.length, 2);
  }
});
