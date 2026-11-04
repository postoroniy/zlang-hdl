// SPDX-License-Identifier: Apache-2.0
'use strict';

// minimatch 11 removes the RegExp API. The pinned client uses makeRe() only
// to reject non-matching patterns; its actual filtering already uses match().
// Keep that check using the parsed pattern set. Never patch an unknown source.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const patches = [
  {
    "path": "lib/common/utils/globPattern.js",
    "original": "ec8f8ed49366dc71c63a6908f0de06568086058cef3e59d6665df4c721826129",
    "patched": "6bab870a3edd1d991af511e2e808afbaac10e236cb36422716b551403db75b7b"
  },
  {
    "path": "lib/common/fileOperations.js",
    "original": "5d38e3f825f80f4e72fd931c50d014cc86f636218b724740377fc7bc9023f907",
    "patched": "9187d6e9b8090916eaa82ad4cb0b61de6fa78dd2908f14852fe4f103e69e7030"
  }
];
const digest = source => crypto.createHash('sha256').update(source).digest('hex');

function patchSource(relative, source) {
  const record = patches.find(item => item.path === relative);
  assert.ok(record, `Unreviewed language-client patch target: ${relative}`);
  if (digest(source) === record.patched) return source;
  assert.equal(digest(source), record.original,
    `Unreviewed language-client source: ${relative}`);
  const result = Buffer.from(source.toString('utf8').replace(
    '!matcher.makeRe()', 'matcher.set.length === 0'));
  assert.equal(digest(result), record.patched, `Incomplete language-client patch: ${relative}`);
  return result;
}

function clientSources() {
  const extension = path.resolve(__dirname, '..');
  const manifest = JSON.parse(fs.readFileSync(path.join(extension, 'package.json'), 'utf8'));
  const toolchain = JSON.parse(fs.readFileSync(path.join(extension, 'editor-toolchain.json'), 'utf8'));
  const root = path.join(extension, 'node_modules/vscode-languageclient');
  assert.equal(manifest.overrides.minimatch, toolchain.minimatch);
  assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8')).version,
    toolchain.vscodeLanguageClient);
  return patches.map(record => {
    const file = path.join(root, record.path);
    const source = fs.readFileSync(file);
    return { file, source, patched: patchSource(record.path, source) };
  });
}

function assertPatchedClient() {
  for (const record of clientSources())
    assert.deepEqual(record.source, record.patched,
      'Run npm ci without --ignore-scripts to apply the reviewed client patch');
}

function main() {
  // Validate every input before writing either file; repeated npm installs are safe.
  const records = clientSources();
  for (const record of records)
    if (!record.source.equals(record.patched)) fs.writeFileSync(record.file, record.patched);
  assertPatchedClient();
  console.log('Reviewed language-client minimatch compatibility patch verified (2 files)');
}

module.exports = { patches, patchSource, assertPatchedClient };
if (require.main === module) main();
