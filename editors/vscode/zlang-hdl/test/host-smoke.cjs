// SPDX-License-Identifier: Apache-2.0
// Invoked by VS Code --extensionTestsPath, never shipped in the lexical VSIX.
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vscode = require('vscode');

const extensionId = 'postoroniy.zlang-hdl';
const languageId = 'zlang-hdl';
const snippetName = 'Clocked module';

function inside(parent, candidate) {
  const relative = path.relative(parent, candidate);
  return relative === '' || (
    relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative)
  );
}

async function settled(description, predicate, timeout = 5000) {
  const deadline = Date.now() + timeout;
  do {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 50));
  } while (Date.now() < deadline);
  assert.fail(`Timed out waiting for ${description}`);
}

function normalized(document) {
  return document.getText().replace(/\r\n/g, '\n');
}

async function replace(editor, text) {
  const document = editor.document;
  const range = new vscode.Range(document.positionAt(0), document.positionAt(document.getText().length));
  assert.equal(await editor.edit((builder) => builder.replace(range, text)), true);
  const end = document.positionAt(document.getText().length);
  editor.selection = new vscode.Selection(end, end);
}

async function checkHost() {
  console.log(`ZLang editor host smoke: VS Code ${vscode.version}`);
  const expectedPath = process.env.ZLANG_EDITOR_SMOKE_EXTENSION;
  assert.ok(expectedPath && path.isAbsolute(expectedPath),
    'ZLANG_EDITOR_SMOKE_EXTENSION must identify the absolute installed VSIX directory');
  const installedPath = fs.realpathSync(expectedPath);
  const sourceRepository = fs.realpathSync(path.resolve(__dirname, '../../../..'));
  assert.equal(inside(sourceRepository, installedPath), false,
    'The smoke must exercise an isolated installed VSIX, not repository sources');
  assert.equal(inside(fs.realpathSync(os.tmpdir()), installedPath), true,
    'The installed extension must be in the isolated temporary directory');

  await settled('installed extension registration', () => vscode.extensions.getExtension(extensionId));
  const extension = vscode.extensions.getExtension(extensionId);
  assert.equal(fs.realpathSync(extension.extensionPath), installedPath);
  assert.equal(extension.id, extensionId);
  const manifest = extension.packageJSON;
  assert.equal(manifest.version, '0.1.0');
  for (const field of ['main', 'browser', 'activationEvents']) {
    assert.equal(Object.hasOwn(manifest, field), false, `Unexpected runtime entry: ${field}`);
  }
  for (const field of ['dependencies', 'optionalDependencies', 'extensionDependencies']) {
    assert.equal(Object.keys(manifest[field] || {}).length, 0, `Unexpected runtime dependency: ${field}`);
  }
  assert.equal(fs.existsSync(path.join(installedPath, 'node_modules')), false);
  const language = manifest.contributes.languages.find((item) => item.id === languageId);
  assert.ok(language, 'Missing installed language contribution');
  assert.deepEqual(language.extensions, ['.zhl']);
  const snippet = manifest.contributes.snippets.find((item) => item.language === languageId);
  assert.ok(snippet, 'Missing installed snippet contribution');
  const snippets = JSON.parse(fs.readFileSync(path.join(installedPath, snippet.path), 'utf8'));
  assert.equal(snippets[snippetName].prefix, 'zmodule');

  const folders = vscode.workspace.workspaceFolders;
  assert.ok(folders && folders.length === 1, 'Launch with one fresh temporary workspace folder');
  assert.equal(folders[0].uri.scheme, 'file');
  const workspace = fs.realpathSync(folders[0].uri.fsPath);
  assert.equal(inside(fs.realpathSync(os.tmpdir()), workspace), true,
    'Never run the smoke in a personal or repository workspace');
  assert.equal(inside(sourceRepository, workspace), false);
  assert.equal(inside(installedPath, workspace), false);
  const scratch = fs.mkdtempSync(path.join(workspace, 'zlang-editor-smoke-'));
  const sourceText = 'module Smoke {\n    in x : u8\n    out y : u8 = x\n}\n';
  const documents = {};
  for (const suffix of ['zhl', 'zl', 'zlh']) {
    const filename = path.join(scratch, `automatic-association.${suffix}`);
    fs.writeFileSync(filename, sourceText, { encoding: 'utf8', flag: 'wx' });
    documents[suffix] = await vscode.workspace.openTextDocument(vscode.Uri.file(filename));
  }
  // Do not call setTextDocumentLanguage or install file associations: these
  // assertions exercise the installed contribution and VS Code's detection.
  await settled('automatic .zhl language association', () => documents.zhl.languageId === languageId);
  for (const suffix of ['zl', 'zlh']) {
    assert.notEqual(documents[suffix].languageId, languageId, `Unexpected .${suffix} association`);
  }

  const editor = await vscode.window.showTextDocument(documents.zhl, { preview: false });
  editor.options = { insertSpaces: true, tabSize: 4 };
  const configuration = vscode.workspace.getConfiguration('editor', documents.zhl.uri);
  await configuration.update('autoIndent', 'full', vscode.ConfigurationTarget.WorkspaceFolder);
  await configuration.update('autoClosingBrackets', 'languageDefined', vscode.ConfigurationTarget.WorkspaceFolder);
  const document = editor.document;

  await replace(editor, 'in x : u8');
  editor.selection = new vscode.Selection(0, 0, 0, document.lineAt(0).text.length);
  await vscode.commands.executeCommand('editor.action.commentLine');
  await settled('language-specific line comment', () => normalized(document) === '// in x : u8');
  await vscode.commands.executeCommand('editor.action.commentLine');
  await settled('line comment removal', () => normalized(document) === 'in x : u8');

  await replace(editor, 'module Smoke ');
  await vscode.commands.executeCommand('type', { text: '{' });
  await settled('configured bracket auto-closing', () => normalized(document) === 'module Smoke {}');
  await vscode.commands.executeCommand('type', { text: '\n' });
  await settled('Enter indentation between brackets', () =>
    normalized(document) === 'module Smoke {\n    \n}'
    && editor.selection.active.line === 1 && editor.selection.active.character === 4);

  await replace(editor, '');
  // Resolve the actual installed, contributed snippet by name. Supplying a
  // body here would bypass registration and would not be an extension test.
  await vscode.commands.executeCommand('editor.action.insertSnippet', {
    langId: languageId,
    name: snippetName,
  });
  await settled('registered Clocked module snippet insertion', () =>
    normalized(document).startsWith('module ModuleName {'));
  assert.deepEqual(normalized(document).split('\n').map((line) => line.trimEnd()), [
    'module ModuleName {',
    '    clock clk reset rst',
    '    in x : u8',
    '    out y : u8 = x',
    '',
    '}',
  ]);
  assert.doesNotMatch(document.getText(), /\$\{?\d/, 'Snippet placeholders must be expanded');
  await settled('snippet module-name tabstop', () => document.getText(editor.selection) === 'ModuleName');
  await vscode.commands.executeCommand('jumpToNextSnippetPlaceholder');
  await settled('snippet clock-name tabstop', () => document.getText(editor.selection) === 'clk');
  assert.equal(await document.save(), true);

  console.log(JSON.stringify({
    status: 'passed',
    vscode: vscode.version,
    extension: extensionId,
    version: manifest.version,
    installedPath,
    workspace,
    checks: [
      'isolated installed VSIX identity and static-only manifest',
      'automatic .zhl association; no .zl or .zlh association',
      'line-comment toggle and removal',
      'bracket auto-closing and Enter indentation',
      'registered Clocked module snippet and working tabstops',
    ],
    notChecked: ['rendered theme colors, font styling, and visual screenshot appearance'],
  }));
}

exports.run = async function run() {
  let timer;
  try {
    await Promise.race([
      checkHost(),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('Editor host smoke exceeded 45 seconds')), 45000);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
};
