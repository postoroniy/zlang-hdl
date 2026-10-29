// SPDX-License-Identifier: Apache-2.0
'use strict';

const assert = require('node:assert/strict');
const {test} = require('node:test');
const source = require('../package.json');
const {editorOnlyManifest} = require('./package.cjs');

const references = {
  command:'editor.action.referenceSearch.trigger', key:'shift+f12',
  when:'editorTextFocus && editorLangId == zlang-hdl && editorHasReferenceProvider'
    + ' && !inReferenceSearchEditor && !isInEmbeddedEditor',
};

test('Shift+F12 peeks references only in the main ZLang editor with a provider', () => {
  assert.deepEqual(source.contributes.keybindings.filter(binding => binding.key === 'shift+f12'),
    [references]);
});

test('universal packaging retains References and removes native-only shortcuts', () => {
  const before = structuredClone(source);
  const universal = editorOnlyManifest(source);
  assert.deepEqual(universal.contributes.keybindings,[references]);
  assert.deepEqual(universal.contributes.commands,[]);
  for (const name of ['breakpoints','debuggers','customEditors','menus'])
    assert.equal(universal.contributes[name],undefined);
  assert.deepEqual(source,before,'projecting universal metadata mutated the native manifest');
});
