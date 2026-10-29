// SPDX-License-Identifier: Apache-2.0
'use strict';

const assert = require('node:assert/strict');
const vscode = require('vscode');

async function checkGenericReferences(source) {
  const previous = vscode.window.activeTextEditor?.document;
  const document = await vscode.workspace.openTextDocument(vscode.Uri.file(source));
  try {
    const editor = await vscode.window.showTextDocument(document, {preview:false});
    assert.equal(document.languageId, 'zlang-hdl');
    const bindings = vscode.extensions.getExtension('postoroniy.zlang-hdl')
      .packageJSON.contributes.keybindings.filter(binding => binding.key === 'shift+f12');
    assert.equal(bindings.length,1,'installed extension lost its Shift+F12 binding');
    const references = bindings[0];
    assert.equal(references.command,'editor.action.referenceSearch.trigger');
    const rows = document.getText().split(/\r?\n/);
    const results = [];
    for (let line=0; line<rows.length; ++line) {
      if (!rows[line].includes('FpDecoded')) continue;
      for (const edge of [false,true]) {
        const column = rows[line].indexOf('FpDecoded') + (edge ? 'FpDecoded'.length : 2);
        const locations = await vscode.commands.executeCommand('vscode.executeReferenceProvider',
          document.uri, new vscode.Position(line,column));
        assert.equal(locations?.length,3,'installed Shift+F12 omitted deferred generic type references');
        assert.ok(locations.every(item=>item.uri.toString()===document.uri.toString()));
        assert.deepEqual(locations.map(item=>item.range.start.line+1),[3,35,42]);
        results.push({line:line+1,edge,count:locations.length});
      }
    }
    assert.equal(results.length,6);
    // Exercise the real References controller lifecycle too: a provider-only
    // check cannot catch state lost when the peek widget is closed.
    editor.selection = new vscode.Selection(41,13,41,13);
    for (const command of ['editor.action.goToReferences',
      'editor.action.referenceSearch.trigger', 'editor.action.goToReferences']) {
      await vscode.commands.executeCommand(command);
      await vscode.commands.executeCommand('closeReferenceSearch');
      const active = vscode.window.activeTextEditor;
      assert.equal(active?.document.uri.toString(),document.uri.toString());
      const locations = await vscode.commands.executeCommand('vscode.executeReferenceProvider',
        document.uri,active.selection.active);
      assert.deepEqual(locations?.map(item=>item.range.start.line+1),[3,35,42],
        'closing References lost deferred generic type references');
      results.push({command,closed:true,count:locations.length});
    }
    // Use the command from the actual extension manifest. Go to References
    // would compact this declaration + one use into a direct jump instead.
    editor.selection = new vscode.Selection(34,8,34,8);
    for (let repeat=0; repeat<3; ++repeat) {
      await vscode.commands.executeCommand(references.command);
      assert.equal(vscode.window.activeTextEditor?.selection.active.line,34,
        'Shift+F12 navigated away instead of keeping the single-use References list');
      const locations = await vscode.commands.executeCommand('vscode.executeReferenceProvider',
        document.uri,editor.selection.active);
      assert.deepEqual(locations?.map(item=>item.range.start.line+1),[35,62]);
      await vscode.commands.executeCommand('closeReferenceSearch');
      results.push({name:'fp_decode',repeat,peekRetainedSource:true,count:locations.length});
    }
    return results;
  } finally {
    if (previous) await vscode.window.showTextDocument(previous,{preview:false});
  }
}

module.exports = {checkGenericReferences};
