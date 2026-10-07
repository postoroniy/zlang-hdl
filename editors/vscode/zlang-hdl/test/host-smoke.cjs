// SPDX-License-Identifier: Apache-2.0
// Invoked by VS Code --extensionTestsPath, never shipped in the Community VSIX.
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vscode = require('vscode');
const toolchain = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../editor-toolchain.json'), 'utf8'));

const extensionId = 'postoroniy.zlang-hdl';
const languageId = 'zlang-hdl';
const snippetName = 'Clocked module';
const hostSmokeTimeoutMs = 300_000;

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

async function replaceDocument(document, text) {
  const edit = new vscode.WorkspaceEdit();
  edit.replace(
    document.uri,
    new vscode.Range(document.positionAt(0), document.positionAt(document.getText().length)),
    text,
  );
  assert.equal(await vscode.workspace.applyEdit(edit), true);
}

async function checkHost() {
  const started = Date.now();
  const phase = (name) => console.log(`ZLang editor host smoke phase ${name}: ${Date.now() - started} ms`);
  console.log(`ZLang editor host smoke: VS Code ${vscode.version}`);
  assert.equal(vscode.version, toolchain.vscodeStable.version,
    'host smoke must run on the exact current stable VS Code version');
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
  assert.equal(manifest.main, './dist/extension.js');
  assert.equal(manifest.engines.vscode, `^${toolchain.vscodeStable.version}`);
  assert.equal(Object.hasOwn(manifest, 'activationEvents'), false);
  assert.deepEqual(manifest.dependencies, { 'vscode-languageclient': toolchain.vscodeLanguageClient });
  assert.equal(fs.existsSync(path.join(installedPath, 'node_modules')), false,
    'node_modules must not be shipped');
  assert.equal(fs.existsSync(path.join(installedPath, 'vendor')), false,
    'legacy vendor dependency trees must not be shipped');
  assert.equal(fs.existsSync(path.join(installedPath, 'extension.js')), false,
    'the unbundled source entrypoint must not be shipped');
  assert.equal(fs.lstatSync(path.join(installedPath, 'dist', 'extension.js')).isFile(), true,
    'bundled runtime is missing');
  assert.equal(fs.lstatSync(path.join(installedPath, 'THIRD_PARTY_NOTICES.txt')).isFile(), true,
    'third-party notices are missing');
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

  let editor = await vscode.window.showTextDocument(documents.zhl, { preview: false });
  editor.options = { insertSpaces: true, tabSize: 4 };
  const configuration = vscode.workspace.getConfiguration('editor', documents.zhl.uri);
  await configuration.update('autoIndent', 'full', vscode.ConfigurationTarget.WorkspaceFolder);
  await configuration.update('autoClosingBrackets', 'languageDefined', vscode.ConfigurationTarget.WorkspaceFolder);
  const document = editor.document;

  // Exercise the real contributed LanguageClient immediately after extension
  // activation.  The activation promise must not resolve until the server has
  // registered its providers; otherwise this first request races start().
  const extensionApi = vscode.extensions.getExtension(extensionId);
  phase('before activation');
  await extensionApi.activate();
  phase('extension activated');
  const useOffset = document.getText().lastIndexOf('x');
  const definitions = await vscode.commands.executeCommand(
    'vscode.executeDefinitionProvider',
    document.uri,
    document.positionAt(useOffset),
  );
  assert.ok(definitions && definitions.length === 1,
    'installed LanguageClient did not provide F12 definition after activation');
  const definition = definitions[0];
  const definitionUri = definition.targetUri ?? definition.uri;
  const definitionRange = definition.targetRange ?? definition.range;
  assert.equal(definitionUri.toString(), document.uri.toString());
  assert.deepEqual(definitionRange.start, new vscode.Position(1, 7));
  phase('simple definition');

  // Mirror dogfooding with the repository opened above a nested locked ZLang
  // project.  Only the root document is opened; declaration targets must be
  // resolved from the document-local manifest/lock, never workspaceFolder.
  const wifiProject = path.join(scratch, 'examples', 'projects', '80211a_transmitter');
  fs.cpSync(
    path.join(sourceRepository, 'examples', 'projects', '80211a_transmitter'),
    wifiProject,
    { recursive: true },
  );
  const transmitter = await vscode.workspace.openTextDocument(vscode.Uri.file(
    path.join(wifiProject, 'src', 'transmitter.zhl'),
  ));
  assert.equal(transmitter.languageId, languageId);
  for (const [name, target, line, character] of [
    ['WifiTxCommand', 'data_types.zhl', 11, 7],
    ['IeeePacketMapper64', 'mapper.zhl', 357, 7],
  ]) {
    const offset = transmitter.getText().indexOf(name);
    assert.notEqual(offset, -1, `missing ${name} occurrence`);
    const locations = await vscode.commands.executeCommand(
      'vscode.executeDefinitionProvider',
      transmitter.uri,
      // Mirror F12 after VS Code has selected the symbol: selection.active is
      // the exclusive right edge, not a character inside the identifier.
      transmitter.positionAt(offset + name.length),
    );
    assert.ok(locations && locations.length === 1, `no definition for ${name}`);
    const location = locations[0];
    const targetUri = location.targetUri ?? location.uri;
    const targetRange = location.targetRange ?? location.range;
    assert.equal(
      targetUri.toString(),
      vscode.Uri.file(path.join(wifiProject, 'src', target)).toString(),
    );
    assert.deepEqual(targetRange.start, new vscode.Position(line, character));
    assert.equal(
      vscode.workspace.textDocuments.some((item) => item.uri.toString() === targetUri.toString()),
      false,
      `${name} target was opened instead of resolved from the locked project`,
    );
  }
  phase('project definitions');

  // Exercise the installed References provider, not only the JSON-RPC server
  // test.  The declaration sits above another module in the same source.
  const syntaxPath = path.join(scratch, 'all_syntax.zhl');
  fs.copyFileSync(path.join(sourceRepository, 'examples', 'all_syntax.zhl'), syntaxPath);
  const syntax = await vscode.workspace.openTextDocument(vscode.Uri.file(syntaxPath));
  const syntaxLines = syntax.getText().split('\n');
  const enumLine = syntaxLines.findIndex((line) => line.startsWith('enum CorpusCode :'));
  assert.ok(enumLine >= 0, 'missing CorpusCode declaration');
  const enumPosition = new vscode.Position(enumLine, syntaxLines[enumLine].indexOf('CorpusCode'));
  const enumReferences = await vscode.commands.executeCommand(
    'vscode.executeReferenceProvider', syntax.uri, enumPosition, { includeDeclaration: true },
  );
  assert.equal(enumReferences?.length, 5, 'installed Shift+F12 omitted same-file enum uses');
  assert.equal(enumReferences.filter((item) => item.range.start.line === enumLine).length, 1);
  assert.ok(enumReferences.every((item) => item.uri.toString() === syntax.uri.toString()));
  assert.ok(enumReferences.every((item) =>
    item.range.end.character - item.range.start.character === 'CorpusCode'.length));
  const enumUseLine = syntaxLines.findIndex((line) => line.includes('enum_decode<CorpusCode>'));
  assert.ok(enumUseLine >= 0);
  const useReferences = await vscode.commands.executeCommand(
    'vscode.executeReferenceProvider', syntax.uri,
    new vscode.Position(enumUseLine, syntaxLines[enumUseLine].indexOf('CorpusCode')),
    { includeDeclaration: true },
  );
  const coordinates = (items) => items.map((item) => [
    item.uri.toString(), item.range.start.line, item.range.start.character,
    item.range.end.line, item.range.end.character,
  ]);
  assert.deepEqual(coordinates(useReferences), coordinates(enumReferences),
    'declaration/use Shift+F12 returned different semantic reference sets');
  phase('same-file references');

  for (const [name, declarationFile, expectedCount] of [
    ['WifiTxCommand', 'data_types.zhl', 6],
    ['IeeePacketMapper64', 'mapper.zhl', 2],
  ]) {
    const offset = transmitter.getText().indexOf(name);
    const references = await vscode.commands.executeCommand(
      'vscode.executeReferenceProvider', transmitter.uri,
      transmitter.positionAt(offset), { includeDeclaration: true },
    );
    assert.equal(references?.length, expectedCount, `installed Shift+F12 omitted ${name} uses`);
    assert.ok(references.some((item) => item.uri.fsPath ===
      path.join(wifiProject, 'src', declarationFile)));
  }
  phase('project references');

  // An unsaved imported source and both accepted instance spellings must use
  // one editor snapshot for diagnostics, completion, F12, and Shift+F12.
  const mapper = await vscode.workspace.openTextDocument(vscode.Uri.file(
    path.join(wifiProject, 'src', 'mapper.zhl'),
  ));
  const ifft = await vscode.workspace.openTextDocument(vscode.Uri.file(
    path.join(wifiProject, 'src', 'ifft.zhl'),
  ));
  const mapperText = mapper.getText();
  const transmitterText = transmitter.getText();
  await replaceDocument(mapper, `// unsaved installed-VSIX overlay\n${mapperText}`);

  const explicitInstanceText = transmitterText.replace(
    '    packet_mapper : IeeePacketMapper64',
    '    inst packet_mapper : IeeePacketMapper64',
  );
  assert.notEqual(explicitInstanceText, transmitterText);
  await replaceDocument(transmitter, explicitInstanceText);
  phase('unsaved project edits');
  await new Promise((resolve) => setTimeout(resolve, 400));
  for (const current of [transmitter, mapper, ifft]) {
    assert.equal(
      vscode.languages.getDiagnostics(current.uri)
        .filter((item) => item.severity === vscode.DiagnosticSeverity.Error).length,
      0,
      `live edit produced an error diagnostic in ${path.basename(current.uri.fsPath)}`,
    );
  }
  const explicitModuleOffset = transmitter.getText().indexOf('IeeePacketMapper64');
  const explicitDefinitions = await vscode.commands.executeCommand(
    'vscode.executeDefinitionProvider',
    transmitter.uri,
    transmitter.positionAt(explicitModuleOffset + 'IeeePacketMapper64'.length),
  );
  assert.equal(explicitDefinitions?.length, 1, 'F12 failed with explicit inst and dirty import');
  phase('dirty definition');
  const explicitTarget = explicitDefinitions[0].targetUri ?? explicitDefinitions[0].uri;
  assert.equal(explicitTarget.toString(), mapper.uri.toString());
  const explicitReferences = await vscode.commands.executeCommand(
    'vscode.executeReferenceProvider', transmitter.uri,
    transmitter.positionAt(explicitModuleOffset), { includeDeclaration: true },
  );
  assert.equal(explicitReferences?.length, 2,
    'Shift+F12 failed with explicit inst and dirty import');
  phase('dirty references');
  await vscode.commands.executeCommand(
    'vscode.executeCompletionItemProvider',
    transmitter.uri,
    transmitter.positionAt(transmitter.getText().indexOf('packet_mapper.command')),
  );

  await replaceDocument(transmitter, transmitterText);
  await new Promise((resolve) => setTimeout(resolve, 400));
  assert.equal(
    vscode.languages.getDiagnostics(transmitter.uri)
      .filter((item) => item.severity === vscode.DiagnosticSeverity.Error).length,
    0,
    'removing inst produced a stale or false diagnostic',
  );
  const conciseModuleOffset = transmitter.getText().indexOf('IeeePacketMapper64');
  const conciseDefinitions = await vscode.commands.executeCommand(
    'vscode.executeDefinitionProvider',
    transmitter.uri,
    transmitter.positionAt(conciseModuleOffset + 'IeeePacketMapper64'.length),
  );
  assert.equal(conciseDefinitions?.length, 1, 'F12 failed after removing inst');
  await replaceDocument(mapper, mapperText);
  phase('live edit restored');

  // Navigation providers may change VS Code's active editor; restore the
  // scratch UI fixture before exercising typing and snippet commands.
  editor = await vscode.window.showTextDocument(document, { preview: false });
  assert.equal(vscode.window.activeTextEditor, editor,
    'scratch fixture must be the active editor before editor commands run');

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
      'isolated installed VSIX identity and standard LSP client manifest',
      'automatic .zhl association; no .zl or .zlh association',
      'activation-complete LanguageClient and real F12 definition provider',
      'nested-project type/module F12 with unopened declaration targets',
      'same-file enum and nested-project type/module Shift+F12 from installed VSIX',
      'multi-document unsaved live edit with explicit and concise instances',
      'line-comment toggle and removal',
      'bracket auto-closing and Enter indentation',
      'registered Clocked module snippet and working tabstops',
    ],
    notChecked: [
      'rendered theme colors, font styling, and visual screenshot appearance',
    ],
  }));
}

exports.run = async function run() {
  let timer;
  try {
    await Promise.race([
      checkHost(),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(
          `Editor host smoke exceeded ${hostSmokeTimeoutMs / 1000} seconds`,
        )), hostSmokeTimeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
};
