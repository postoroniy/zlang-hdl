// SPDX-License-Identifier: Apache-2.0
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const { createRequire } = require('node:module');
const path = require('node:path');
const test = require('node:test');

const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '../package.json'), 'utf8'));
const vsceRequire = createRequire(require.resolve('@vscode/vsce'));
const identityRequire = createRequire(vsceRequire.resolve('@azure/identity'));
const restRequire = createRequire(vsceRequire.resolve('typed-rest-client'));

test('VSIX tooling resolves the reviewed MSAL override through Azure Identity', () => {
  const expected = manifest.overrides['@azure/identity']['@azure/msal-node'];
  assert.equal(identityRequire('@azure/msal-node/package.json').version, expected);
  const msal = identityRequire('@azure/msal-node');
  const client = new msal.PublicClientApplication({ auth: {
    clientId: '00000000-0000-0000-0000-000000000001',
    authority: 'https://login.microsoftonline.com/common',
  } });
  assert.equal(typeof client.acquireTokenByCode, 'function');
  assert.equal(typeof client.getTokenCache().getAllAccounts, 'function');
  assert.equal(typeof require('@vscode/vsce').createVSIX, 'function');
});

test('the packaging credential chain loads without authentication or network calls', () => {
  const identity = vsceRequire('@azure/identity');
  const credential = new identity.ChainedTokenCredential(
    new identity.AzureCliCredential(), new identity.ManagedIdentityCredential(),
    new identity.AzurePowerShellCredential(), new identity.AzureDeveloperCliCredential(),
  );
  assert.equal(typeof credential.getToken, 'function');
});

test('REST query serialization handles a non-callable own toString in comma arrays', () => {
  const qs = restRequire('qs');
  assert.equal(qs.stringify({ items: [{ toString: 'non-callable' }] }, { arrayFormat: 'comma' }),
    'items=%5Bobject%20Object%5D');
});

const vm = require('node:vm');
const { spawnSync } = require('node:child_process');
const { patches, patchSource, assertPatchedClient } = require('./patch-languageclient.cjs');
const toolchain = require('../editor-toolchain.json');
const clientRequire = createRequire(require.resolve('vscode-languageclient/node'));
const clientRoot = path.join(__dirname, '../node_modules/vscode-languageclient');

test('packager and client both resolve the exact patched minimatch', () => {
  assert.equal(manifest.overrides.minimatch, toolchain.minimatch);
  for (const consumer of [vsceRequire, clientRequire]) {
    assert.equal(consumer('minimatch/package.json').version, toolchain.minimatch);
    assert.equal(consumer('minimatch').minimatch('.vscode/settings.json', '**/*.json', {dot:true}), true);
  }
});

test('the client patch is idempotent and rejects unreviewed inputs', () => {
  assertPatchedClient();
  for (const record of patches) {
    const source = fs.readFileSync(path.join(clientRoot, record.path));
    const original = Buffer.from(source.toString().replace('matcher.set.length === 0', '!matcher.makeRe()'));
    assert.deepEqual(patchSource(record.path, original), source);
    assert.deepEqual(patchSource(record.path, source), source);
    assert.throws(() => patchSource(record.path, Buffer.concat([source, Buffer.from('// changed')])));
  }
  assert.throws(() => patchSource('unknown.js', Buffer.from('')), /Unreviewed/);
});

function loadClientPart(relative, vscode, original = false) {
  const file = path.join(clientRoot, relative);
  let source = fs.readFileSync(file, 'utf8');
  if (original) source = source.replace('matcher.set.length === 0', '!matcher.makeRe()');
  const exports = {};
  const scopedRequire = createRequire(file);
  const load = new vm.Script(`(function(require, exports) {${source}\n})`, {filename:file}).runInThisContext();
  load(name => name === 'vscode' ? vscode : scopedRequire(name), exports);
  return exports;
}

test('real client glob matching retains string, relative, excluded and invalid patterns', () => {
  const relative = 'lib/common/utils/globPattern.js';
  const uri = value => ({path:value.replace('file://', ''),
    with: ({path:fsPath}) => ({fsPath})});
  const vscode = {Uri:{parse:uri}};
  const { matchGlobPattern } = loadClientPart(relative, vscode);
  for (const [pattern, file, expected] of [
    ['**/*.zhl', '/work/core.zhl', true], ['**/*.zhl', '/work/core.txt', false],
    ['**/*.{zhl,toml}', '/work/zlang.toml', true], ['#comment', '/work/core.zhl', false],
    [{baseUri:'file:///work',pattern:'**/*.zhl'}, '/work/sub/core.zhl', true],
    [{baseUri:{uri:'file:///work'},pattern:'**/*.zhl'}, '/elsewhere/core.zhl', false],
  ]) assert.equal(matchGlobPattern(pattern, {fsPath:file}), expected);
  assert.throws(() => loadClientPart(relative, vscode, true)
    .matchGlobPattern('**/*.zhl', {fsPath:'/work/core.zhl'}), /makeRe/);
});

test('real client file-operation registration retains scheme and glob filtering', async () => {
  const relative = 'lib/common/fileOperations.js';
  const vscode = {workspace:{onDidCreateFiles:() => ({dispose(){}})}};
  const client = {code2ProtocolConverter:{asCreateFilesParams:event => event}};
  const feature = new (loadClientPart(relative, vscode).DidCreateFilesFeature)(client);
  feature.register({id:'zlang',registerOptions:{filters:[
    {scheme:'file',pattern:{glob:'**/*.zhl',options:{ignoreCase:true}}},
  ]}});
  const files = [{fsPath:'/work/CORE.ZHL',scheme:'file'},
    {fsPath:'/work/core.txt',scheme:'file'}, {fsPath:'/work/core.zhl',scheme:'untitled'}];
  const filtered = await feature.filter({files}, item => item);
  assert.deepEqual(filtered.files, [files[0]]);
  assert.throws(() => feature.register({id:'invalid',registerOptions:{filters:[
    {pattern:{glob:'#comment'}},
  ]}}), /Invalid pattern/);
  feature.unregister('zlang');
  assert.equal(feature.filterSize(), 0);
  const unpatched = new (loadClientPart(relative, vscode, true).DidCreateFilesFeature)(client);
  assert.throws(() => unpatched.register({id:'zlang',registerOptions:{filters:[
    {pattern:{glob:'**/*.zhl'}},
  ]}}), /makeRe/);
});

test('adversarial glob preprocessing is bounded by the patched implementation', () => {
  // Exercise both upstream changes without risking an unbounded test process.
  const child = spawnSync(process.execPath, ['--max-old-space-size=128', '-e', `
    const assert = require('node:assert/strict');
    const {Minimatch} = require('minimatch');
    const pattern = Array(30).fill('**/../a/b').join('/');
    const limited = new Minimatch(pattern, {optimizationLevel:2, braceExpandMax:64});
    assert.equal(limited.set.length, 64);
    const small = new Minimatch('**/../a/b/**/../c/d', {optimizationLevel:2});
    assert.equal(small.match('../a/b/../c/d'), true);
    assert.equal(small.match('x/a/b/y/c/d'), true);
  `], {cwd:path.join(__dirname, '..'),timeout:5000,encoding:'utf8',maxBuffer:65536});
  assert.ifError(child.error);
  assert.equal(child.status, 0, child.stderr);
});
