// SPDX-License-Identifier: Apache-2.0
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const vscode = require('vscode');

async function checkProjectMetadata(scratch, settled, {excludeRecursive=false} = {}) {
  const name = excludeRecursive ? 'metadata-without-recursive-feed' : 'metadata-lifecycle';
  const project = path.join(scratch, name);
  const config = vscode.workspace.getConfiguration('files', vscode.Uri.file(project));
  const previousExcludes = config.get('watcherExclude');
  if (excludeRecursive) await config.update('watcherExclude', {
    ...previousExcludes, [`**/${name}/**`]:true,
  }, vscode.ConfigurationTarget.WorkspaceFolder);
  const record = (event, detail) => {
    const line = JSON.stringify({time:new Date().toISOString(), event, detail, excludeRecursive});
    console.log('ZLang metadata lifecycle: '+line);
    const log = process.env.ZLANG_EDITOR_SMOKE_METADATA_LOG;
    if (log) {
      fs.mkdirSync(path.dirname(log), {recursive:true});
      fs.appendFileSync(log, line+'\n');
    }
  };
  const watcher = vscode.workspace.createFileSystemWatcher(`**/${name}/{zlang.toml,zlang.lock}`);
  const subscriptions = [watcher,
    watcher.onDidCreate(uri => record('recursive-create', uri.toString())),
    watcher.onDidChange(uri => record('recursive-change', uri.toString())),
    watcher.onDidDelete(uri => record('recursive-delete', uri.toString())),
    vscode.languages.onDidChangeDiagnostics(event => {
      for (const uri of event.uris) if (uri.fsPath.startsWith(project+path.sep))
        record('diagnostics', {uri:uri.toString(), messages:vscode.languages.getDiagnostics(uri).map(d => d.message)});
    }),
  ];
  try {
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
    record('delete-lock', lock);
    fs.unlinkSync(lock);
    await settled('lock deletion recheck without source edit', clear, 15000);
    fs.writeFileSync(lock, payload);
    await settled('lock creation recheck without source edit', hasError, 15000);
    fs.writeFileSync(manifest, '[project\n');
    await settled('malformed manifest recheck without worker crash', clear, 15000);
    fs.writeFileSync(manifest, toml);
    await settled('manifest repair recheck without restart', hasError, 15000);
    assert.deepEqual(fs.readFileSync(lock), payload, 'LSP must not rewrite the lock');
  } catch (error) {
    record('failure', {message:String(error), documents:vscode.workspace.textDocuments
      .filter(d => d.uri.fsPath.startsWith(project+path.sep))
      .map(d => ({uri:d.uri.toString(), version:d.version,
        diagnostics:vscode.languages.getDiagnostics(d.uri).map(item => item.message)}))});
    throw error;
  } finally {
    subscriptions.forEach(item => item.dispose());
    if (excludeRecursive) await config.update('watcherExclude', previousExcludes,
      vscode.ConfigurationTarget.WorkspaceFolder);
  }
}

async function checkImportConflictLocations(scratch, settled) {
  const project = path.join(scratch, 'import-conflict-locations');
  const src = path.join(project, 'src');
  fs.mkdirSync(src, {recursive:true});
  const manifest = path.join(project, 'zlang.toml');
  fs.writeFileSync(manifest,
    'schema=1\n[project]\nname="collision"\nversion="1"\nsource-root="src"\n');
  fs.writeFileSync(path.join(src, 'left.zhl'), 'struct Collision { value:u8 }\n');
  const rightPath = path.join(src, 'right.zhl');
  const disk = '// second declaration\n\nstruct Collision { value:u8 }\n';
  fs.writeFileSync(rightPath, disk);
  const rootPath = path.join(src, 'top.zhl');
  const rootText = 'import collision.left as a\nimport collision.right as b\nmodule Top {}\n';
  fs.writeFileSync(rootPath, rootText);
  const server = vscode.workspace.getConfiguration('zlang').get('lsp.path');
  const locked = spawnSync(path.join(path.dirname(server), 'zlang'),
    ['lock', 'update', '--project', manifest], {encoding:'utf8'});
  assert.equal(locked.status, 0, locked.stderr);
  const root = await vscode.workspace.openTextDocument(vscode.Uri.file(rootPath));
  await vscode.window.showTextDocument(root, {preview:false});
  const rightUri = vscode.Uri.file(rightPath);
  const conflict = () => vscode.languages.getDiagnostics(rightUri)
    .find(item => item.code === 'ZL-IMPORT-CONFLICT');
  await settled('closed import conflict on declaration, not root',
    () => conflict()?.range.start.line === 2, 15000);
  assert.equal(conflict().range.start.character, 7);
  assert.equal(vscode.languages.getDiagnostics(root.uri).length, 0);
  const right = await vscode.workspace.openTextDocument(rightUri);
  const overlay = '// unsaved\r\n// second declaration\r\n\r\n/* 🔧 */ struct Collision { value:u8 }\r\n';
  const replace = async (document, text) => {
    const edit = new vscode.WorkspaceEdit();
    edit.replace(document.uri, new vscode.Range(document.positionAt(0),
      document.positionAt(document.getText().length)), text);
    assert.equal(await vscode.workspace.applyEdit(edit), true);
  };
  await replace(right, overlay);
  await replace(root, rootText+'\n');
  await settled('unsaved import conflict uses UTF-16 declaration range', () =>
    conflict()?.range.start.line === 3
    && conflict().range.start.character === overlay.split('\r\n')[3].indexOf('Collision'), 15000);
  assert.equal(fs.readFileSync(rightPath, 'utf8'), disk, 'host must not save the overlay');
  await replace(right, disk.replace('Collision', 'Other'));
  await replace(root, rootText+'\n\n');
  await settled('repaired import clears its old diagnostic', () =>
    vscode.languages.getDiagnostics(rightUri).length === 0, 15000);
}

module.exports = {checkProjectMetadata,checkImportConflictLocations};
