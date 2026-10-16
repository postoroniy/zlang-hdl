// SPDX-License-Identifier: Apache-2.0
'use strict';

// VS Code passes the DAP variable and its session, not a source expression AST.
// Only compiler-published evaluateName bindings are accepted by the adapter.
async function addVariableToWaveform(vscode, context, openLive, item) {
  const session = vscode.debug.activeDebugSession;
  if (session?.type !== 'zlang-hdl' || (item?.sessionId && item.sessionId !== session.id)) {
    throw new Error('Select a variable in the active paused ZLang HDL debug session.');
  }
  const name = item?.variable?.evaluateName || item?.expression;
  if (typeof name !== 'string' || !name) {
    throw new Error('This item has no compiler-owned signal binding. Select a signal or field in Variables/Watch.');
  }
  await session.customRequest('zlang/addTraceSignals', {signals:[name]});
  await openLive(context);
}

module.exports = {addVariableToWaveform};
