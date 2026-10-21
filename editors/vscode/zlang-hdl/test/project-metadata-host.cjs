// SPDX-License-Identifier: Apache-2.0
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const vscode = require('vscode');

async function checkProjectMetadata(scratch, settled) {
  const project = path.join(scratch, 'metadata-lifecycle');
  fs.mkdirSync(path.join(project, 'src'), {recursive:true});
  const manifest = path.join(project, 'zlang.toml');
  const toml = 'schema=1\n[project]\nname="metadata"\nversion="1"\nsource-root="src"\n';
  fs.writeFileSync(manifest, toml);
  const source = path.join(project, 'src', 'top.zhl');
  fs.writeFileSync(source, 'module Metadata { in a:Missing }\n');
  // VS Code may rewrite PATH while resolving the user's shell environment.
  // Use the same explicit installation selected for this host's LSP.
  const server = vscode.workspace.getConfiguration('zlang').get('lsp.path');
  assert.ok(typeof server === 'string' && path.isAbsolute(server));
  const command = path.join(path.dirname(server), 'zlang');
  const locked = spawnSync(command, ['lock', 'update', '--project', manifest], {encoding:'utf8'});
  assert.equal(locked.status, 0, locked.stderr);
  const lock = path.join(project, 'zlang.lock');
  const payload = fs.readFileSync(lock);
  const uri = vscode.Uri.file(source);
  await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(uri));
  const hasError = () => vscode.languages.getDiagnostics(uri)
    .some(item => item.message.includes("unknown type 'Missing'"));
  const clear = () => vscode.languages.getDiagnostics(uri).length === 0;
  await settled('initial project semantic diagnostic', hasError, 15000);
  fs.unlinkSync(lock);
  await settled('lock deletion recheck without source edit', clear, 15000);
  fs.writeFileSync(lock, payload);
  await settled('lock creation recheck without source edit', hasError, 15000);
  fs.writeFileSync(manifest, '[project\n');
  await settled('malformed manifest recheck without worker crash', clear, 15000);
  fs.writeFileSync(manifest, toml);
  await settled('manifest repair recheck without restart', hasError, 15000);
  assert.deepEqual(fs.readFileSync(lock), payload, 'LSP must not rewrite the lock');
}

module.exports = {checkProjectMetadata};
