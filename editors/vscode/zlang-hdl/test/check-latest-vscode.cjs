// SPDX-License-Identifier: Apache-2.0
'use strict';

const assert = require('node:assert/strict');
const https = require('node:https');
const { toolchain } = require('./bundle.cjs');

const endpoint = 'https://update.code.visualstudio.com/api/update/linux-x64/stable/latest';

function fetchLatest() {
  return new Promise((resolve, reject) => {
    const request = https.get(endpoint, { headers: { Accept: 'application/json' } }, (response) => {
      if (response.statusCode !== 200) {
        response.resume();
        reject(new Error(`VS Code update endpoint returned HTTP ${response.statusCode}`));
        return;
      }
      let body = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => {
        body += chunk;
        if (body.length > 1_000_000) request.destroy(new Error('VS Code update response is oversized'));
      });
      response.on('end', () => {
        try { resolve(JSON.parse(body)); } catch (error) { reject(error); }
      });
    });
    request.setTimeout(15_000, () => request.destroy(new Error('VS Code update request timed out')));
    request.on('error', reject);
  });
}

async function main() {
  const latest = await fetchLatest();
  assert.equal(latest.productVersion, toolchain.vscodeStable.version,
    'editor-toolchain.json does not name the current stable VS Code release');
  assert.equal(latest.version, toolchain.vscodeStable.commit,
    'editor-toolchain.json does not pin the current stable VS Code commit');
  console.log(`verified latest VS Code stable ${latest.productVersion} (${latest.version})`);
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
