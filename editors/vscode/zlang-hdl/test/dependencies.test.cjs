// SPDX-License-Identifier: Apache-2.0
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const { createRequire } = require('node:module');
const path = require('node:path');
const test = require('node:test');

const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '../package.json'), 'utf8'));
const vsceRequire = createRequire(require.resolve('@vscode/vsce'));
const identityRequire = createRequire(vsceRequire.resolve('@azure/identity'));
const restRequire = createRequire(vsceRequire.resolve('typed-rest-client'));

test('VSIX tooling resolves the reviewed MSAL override through Azure Identity', () => {
  const expected = manifest.overrides['@azure/identity']['@azure/msal-node'];
  assert.equal(identityRequire('@azure/msal-node/package.json').version, expected);
  const msal = identityRequire('@azure/msal-node');
  const client = new msal.PublicClientApplication({ auth: {
    clientId: '00000000-0000-0000-0000-000000000001',
    authority: 'https://login.microsoftonline.com/common',
  } });
  assert.equal(typeof client.acquireTokenByCode, 'function');
  assert.equal(typeof client.getTokenCache().getAllAccounts, 'function');
  assert.equal(typeof require('@vscode/vsce').createVSIX, 'function');
});

test('the packaging credential chain loads without authentication or network calls', () => {
  const identity = vsceRequire('@azure/identity');
  const credential = new identity.ChainedTokenCredential(
    new identity.AzureCliCredential(), new identity.ManagedIdentityCredential(),
    new identity.AzurePowerShellCredential(), new identity.AzureDeveloperCliCredential(),
  );
  assert.equal(typeof credential.getToken, 'function');
});

test('REST query serialization handles a non-callable own toString in comma arrays', () => {
  const qs = restRequire('qs');
  assert.equal(qs.stringify({ items: [{ toString: 'non-callable' }] }, { arrayFormat: 'comma' }),
    'items=%5Bobject%20Object%5D');
});
