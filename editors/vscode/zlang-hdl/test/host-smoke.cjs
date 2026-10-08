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
  const sourceExtension = fs.realpathSync(path.resolve(__dirname, '..'));
  // Project policy keeps temporary files below the checkout, so repository
  // containment alone no longer distinguishes source from an installed VSIX.
  assert.equal(inside(sourceExtension, installedPath), false,
    'The smoke must exercise an isolated installed VSIX, not extension sources');
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
  // VS Code 1.141 correctly focus-gates UI keybinding commands in the
  // extension-test host.  Inspect the isolated installed contribution here;
  // the package tests separately compile every shipped snippet default.
  const languageConfiguration = JSON.parse(fs.readFileSync(
    path.join(installedPath, language.configuration), 'utf8',
  ));
  assert.equal(languageConfiguration.comments.lineComment, '//');
  assert.ok(languageConfiguration.autoClosingPairs.some(
    (item) => item.open === '{' && item.close === '}',
  ));
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
  assert.equal(inside(sourceExtension, workspace), false,
    'Never run the smoke in the extension source workspace');
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

  const completions = await vscode.commands.executeCommand(
    'vscode.executeCompletionItemProvider',
    document.uri,
    document.positionAt(useOffset),
  );
  assert.ok(
    completions?.items.some((item) => item.label === 'x'),
    'installed LanguageClient did not provide compiler-owned completion',
  );
  phase('simple completion');

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
      'compiler-owned completion through the installed LanguageClient',
      'nested-project type/module F12 with unopened declaration targets',
      'same-file enum and nested-project type/module Shift+F12 from installed VSIX',
      'multi-document unsaved live edit with explicit and concise instances',
      'installed language configuration and registered snippet assets',
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
