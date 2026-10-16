// SPDX-License-Identifier: Apache-2.0
'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const path = require('node:path');
const { auditJavaScript, browserScopes } = require('./javascript-static.cjs');

const fixture = text => [{ file: path.join(__dirname, '__scope_fixture.cjs'), text }];

test('static gate rejects unused functions, constants and parameters', () => {
  const diagnostics = auditJavaScript(fixture(`
    const abandoned = 1;
    function oldConverter() { return 2; }
    function active(unusedArgument) { return 3; }
    module.exports = {active};
  `));
  for (const name of ['abandoned', 'oldConverter', 'unusedArgument'])
    assert.ok(diagnostics.some(item => item.message.includes(name)), name);
});

test('static gate rejects syntax errors and undefined Node names', () => {
  assert.ok(auditJavaScript(fixture('module.exports = ;')).length);
  const diagnostics = auditJavaScript(fixture('module.exports = () => missingHelper();'));
  assert.ok(diagnostics.some(item => item.message.includes('missingHelper')));
  assert.equal(diagnostics[0].line, 1);
  assert.ok(diagnostics[0].column > 0);
  assert.ok(auditJavaScript(fixture('module.exports = () => state.cursor;'))
    .some(item => item.message.includes('state')));
});

test('Node globals, destructuring aliases and intentionally ignored parameters are accepted', () => {
  assert.deepEqual(auditJavaScript(fixture(`
    const {join: concatenate} = require('node:path');
    module.exports = (_event) => Buffer.from(concatenate(__dirname, process.platform));
  `)), []);
});

test('composed browser scope accepts cross-domain references and catches missing helpers', () => {
  const source = `(() => {
    const state = {cursor: 0};
    function format(value) { return String(value); }
    function paint() { document.title = format(state.cursor); }
    window.addEventListener('message', paint);
  })();`;
  assert.deepEqual(auditJavaScript(fixture(source)), []);
  assert.ok(auditJavaScript(fixture(source.replace('format(state.cursor)', 'lostHelper(state.cursor)')))
    .some(item => item.message.includes('lostHelper')));
  assert.ok(auditJavaScript(fixture(source.replace('const state', 'function deadBrowserHelper() {}\nconst state')))
    .some(item => item.message.includes('deadBrowserHelper')));
});

test('browser module names are resolved by composition, not a global application allow-list', () => {
  const sources = fixture('module.exports = () => state.cursor;');
  assert.deepEqual(auditJavaScript(sources, new Set([sources[0].file])), []);
  sources.push({ file: path.join(__dirname, '__composed_fixture.js'),
    text: '(() => { window.paint = () => state.cursor; })();' });
  assert.ok(auditJavaScript(sources, new Set([sources[0].file]))
    .some(item => item.file.endsWith('__composed_fixture.js') && item.message.includes('state')));
});

test('serialized test instrumentation is checked inside its real browser scope', () => {
  const sources = fixture('// @zlang-webview-scope\nmodule.exports = () => sharedName();');
  const valid = browserScopes(sources, 'function sharedName() {}\nwindow.use = sharedName;');
  assert.deepEqual(auditJavaScript(valid.sources, valid.browserModules), []);
  const broken = browserScopes(sources, 'window.use = 1;');
  assert.ok(auditJavaScript(broken.sources, broken.browserModules)
    .some(item => item.file.endsWith('.webview.js') && item.message.includes('sharedName')));
});
