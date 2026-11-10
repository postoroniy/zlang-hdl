// SPDX-License-Identifier: Apache-2.0
'use strict';
const assert = require('node:assert/strict');

// Test-only publication barrier. An old empty diagnostic collection cannot
// satisfy the first phase. Repair is sent only after observing this unique
// semantic error, so its later clear must come from a fresh publication.
async function checkDiagnosticRoundTrip({readText, replace, diagnostics, wait, errorSeverity}) {
  const original = readText();
  const missing = 'HostSmokeDiagnosticBarrierMissingType';
  try {
    await replace(`module HostSmokeDiagnosticBarrier { in value:${missing} }\n`);
    await wait('fresh diagnostic barrier error', () => diagnostics().some(item =>
      item.severity === errorSeverity && item.message.includes(`unknown type '${missing}'`)));
  } finally {
    await replace(original);
  }
  await wait('fresh diagnostic barrier repair', () => diagnostics().length === 0);
  assert.equal(readText(), original, 'diagnostic barrier must restore the exact fixture');
}

module.exports = {checkDiagnosticRoundTrip};
