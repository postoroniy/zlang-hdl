// SPDX-License-Identifier: Apache-2.0
'use strict';

// Scope checks only: syntax, undefined names, and unused locals/parameters.
// This is not a TypeScript type-checking gate for the JavaScript application.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const { waveformSource, browserFiles } = require('./waveform-assets.cjs');

const root = path.resolve(__dirname, '..');
const pin = JSON.parse(fs.readFileSync(path.join(root, 'editor-toolchain.json'), 'utf8'));
assert.equal(ts.version, pin.typescript, 'Install the locked JavaScript static toolchain with npm ci');

const unused = new Set([6133, 6138, 6192, 6196, 6198, 6199]);
const undefinedName = new Set([2304, 2552, 2580]);
const ambient = path.join(root, '__javascript_scope_globals.d.ts');
// ES/DOM globals come from the pinned compiler libraries. These additional
// names are injected by Node or VS Code, never inferred from application use.
const globals = `declare const require: any, module: any, exports: any,
  Buffer: any, process: any, __dirname: string, __filename: string,
  global: any, setImmediate: any, acquireVsCodeApi: any;`;

function auditJavaScript(sources, browserModules = new Set()) {
  const inputs = new Map(sources.map(source => [source.file, source.text]));
  inputs.set(ambient, globals);
  const options = {
    allowJs: true, checkJs: true, noEmit: true, noUnusedLocals: true,
    noUnusedParameters: true, noResolve: true, types: [],
    lib: ['lib.es2022.d.ts', 'lib.dom.d.ts'],
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
  };
  const host = ts.createCompilerHost(options);
  const original = host.getSourceFile;
  host.getSourceFile = (file, version, ...args) => inputs.has(file)
    ? ts.createSourceFile(file, inputs.get(file), version, true)
    : original(file, version, ...args);
  const program = ts.createProgram([...inputs.keys()], options, host);
  const diagnostics = [
    ...program.getSyntacticDiagnostics(),
    ...program.getSemanticDiagnostics().filter(item => unused.has(item.code)
      || (undefinedName.has(item.code) && !browserModules.has(item.file?.fileName))),
  ];
  return diagnostics.filter(item => inputs.has(item.file?.fileName)).map(item => {
    const location = item.file.getLineAndCharacterOfPosition(item.start || 0);
    return { file: item.file.fileName, line: location.line + 1, column: location.character + 1,
      code: item.code, message: ts.flattenDiagnosticMessageText(item.messageText, ' ') };
  });
}

function browserScopes(sources, source = waveformSource()) {
  const browserModules = new Set(browserFiles.map(file => path.join(root, file)));
  const scoped = [{ file: path.join(root, '__waveform_webview.js'),
    text: `(() => {\n${source}\n})();` }];
  for (const fragment of sources) {
    if (!fragment.text.split('\n').includes('// @zlang-webview-scope')) continue;
    browserModules.add(fragment.file);
    scoped.push({ file: `${fragment.file}.webview.js`,
      text: `(() => {\n${source}\n${fragment.text}\n})();` });
  }
  return { sources: [...sources, ...scoped], browserModules };
}

function repositorySources() {
  const files = [
    ...fs.readdirSync(root).filter(name => name.endsWith('.js')),
    ...fs.readdirSync(__dirname).filter(name => name.endsWith('.cjs')).map(name => `test/${name}`),
  ].sort();
  const sources = files.map(name => ({ file: path.join(root, name),
    text: fs.readFileSync(path.join(root, name), 'utf8') }));
  // Use the asset builder's exact source, not an independent function list.
  return browserScopes(sources);
}

function main() {
  const { sources, browserModules } = repositorySources();
  const diagnostics = auditJavaScript(sources, browserModules);
  for (const item of diagnostics) console.error(
    `${path.relative(root, item.file)}:${item.line}:${item.column}: JS${item.code} ${item.message}`,
  );
  if (diagnostics.length) process.exitCode = 1;
  else console.log(`JavaScript static gate passed: ${sources.length} source/scope inputs`);
}

module.exports = { auditJavaScript, browserScopes, repositorySources };
if (require.main === module) main();
