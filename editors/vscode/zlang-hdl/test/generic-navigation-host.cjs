// SPDX-License-Identifier: Apache-2.0
'use strict';

const assert = require('node:assert/strict');
const vscode = require('vscode');

function sourceRanges(rows, name, anchors) {
  return anchors.map(anchor => {
    const matches = rows.flatMap((row, line) => row.includes(anchor) ? [line] : []);
    assert.equal(matches.length, 1, `expected one source occurrence of ${anchor}`);
    const [line] = matches;
    const column = rows[line].indexOf(name, rows[line].indexOf(anchor));
    assert.ok(column >= 0, `${anchor} does not locate ${name}`);
    return new vscode.Range(line, column, line, column + name.length);
  });
}

function assertReferences(locations, document, ranges, message) {
  assert.equal(locations?.length, ranges.length, message);
  for (const [index, range] of ranges.entries()) {
    const location = locations[index];
    assert.equal(location.uri.toString(), document.uri.toString(), message);
    assert.ok(location.range.isEqual(range), message);
  }
}

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
    const decoded = sourceRanges(rows, 'FpDecoded',
      ['struct FpDecoded', '-> FpDecoded', '    FpDecoded {']);
    const decode = sourceRanges(rows, 'fp_decode', ['fn fp_decode<', 'd = fp_decode<']);
    const results = [];
    for (const range of decoded) {
      for (const edge of [false, true]) {
        const cursor = edge ? range.end : range.start.translate(0, 2);
        const locations = await vscode.commands.executeCommand('vscode.executeReferenceProvider',
          document.uri, cursor);
        assertReferences(locations, document, decoded,
          'installed Shift+F12 returned incorrect deferred generic type references');
        results.push({line:cursor.line+1,edge,count:locations.length});
      }
    }
    assert.equal(results.length,6);
    // Exercise the real References controller lifecycle too: a provider-only
    // check cannot catch state lost when the peek widget is closed.
    const constructor = decoded[2].end;
    editor.selection = new vscode.Selection(constructor, constructor);
    for (const command of ['editor.action.goToReferences',
      'editor.action.referenceSearch.trigger', 'editor.action.goToReferences']) {
      await vscode.commands.executeCommand(command);
      await vscode.commands.executeCommand('closeReferenceSearch');
      const active = vscode.window.activeTextEditor;
      assert.equal(active?.document.uri.toString(),document.uri.toString());
      const locations = await vscode.commands.executeCommand('vscode.executeReferenceProvider',
        document.uri,active.selection.active);
      assertReferences(locations, document, decoded,
        'closing References lost deferred generic type references');
      results.push({command,closed:true,count:locations.length});
    }
    // Use the command from the actual extension manifest. Go to References
    // would compact this declaration + one use into a direct jump instead.
    const declaration = decode[0].start.translate(0, 5);
    editor.selection = new vscode.Selection(declaration, declaration);
    for (let repeat=0; repeat<3; ++repeat) {
      await vscode.commands.executeCommand(references.command);
      const active = vscode.window.activeTextEditor;
      assert.equal(active?.document.uri.toString(), document.uri.toString());
      assert.ok(active.selection.active.isEqual(declaration),
        'Shift+F12 navigated away instead of keeping the single-use References list');
      const locations = await vscode.commands.executeCommand('vscode.executeReferenceProvider',
        document.uri,editor.selection.active);
      assertReferences(locations, document, decode,
        'installed Shift+F12 returned incorrect fp_decode function references');
      await vscode.commands.executeCommand('closeReferenceSearch');
      results.push({name:'fp_decode',repeat,peekRetainedSource:true,count:locations.length});
    }
    return results;
  } finally {
    if (previous) await vscode.window.showTextDocument(previous,{preview:false});
  }
}

module.exports = {checkGenericReferences};
