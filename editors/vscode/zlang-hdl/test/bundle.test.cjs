// SPDX-License-Identifier: Apache-2.0
'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');
const { buildBundle, runtimePackages, sha256, thirdPartyNotices } = require('./bundle.cjs');
const {
  extensionInstallArguments,
  hostLaunchArguments,
} = require('./run-host-smoke.cjs');

async function main() {
  const first = await buildBundle();
  const second = await buildBundle();
  assert.equal(sha256(first.bytes), sha256(second.bytes), 'runtime bundle is not deterministic');
  assert.deepEqual(first.bytes, second.bytes, 'runtime bundle bytes changed between builds');
  assert.ok(first.bytes.length < 600_000, `runtime bundle is unexpectedly large: ${first.bytes.length}`);
  const text = first.bytes.toString('utf8');
  assert.equal(text.includes('sourceMappingURL='), false);
  assert.equal(text.includes('/home/'), false);
  assert.equal(text.includes('vendor/node_modules'), false);
  assert.equal(text.includes('.private/'), false);
  const packages = runtimePackages();
  assert.ok(packages.length > 0, 'runtime closure must not be empty');
  const notices = thirdPartyNotices(packages);
  for (const item of packages) assert.ok(notices.includes(`${item.name}@${item.version}`));
  const hostArgs = hostLaunchArguments({
    extensions: '/tmp/extensions',
    userData: '/tmp/user-data',
    installed: '/tmp/installed',
    workspace: '/tmp/workspace',
  });
  assert.equal(hostArgs.filter((item) => item === '--no-sandbox').length, 1,
    'ephemeral VS Code test host must disable the unavailable CI Chromium sandbox');
  assert.deepEqual(hostArgs.slice(-2), [
    path.join(__dirname, 'host-smoke.cjs'), '/tmp/workspace',
  ]);
  const installArgs = extensionInstallArguments({
    vsix: '/tmp/zlang-hdl.vsix',
    extensions: '/tmp/extensions',
  });
  assert.equal(installArgs.filter((item) => item === '--no-sandbox').length, 1,
    'ephemeral VS Code installer must disable the unavailable CI Chromium sandbox');
  assert.deepEqual(installArgs.slice(1), [
    '--install-extension', '/tmp/zlang-hdl.vsix', '--force',
    '--extensions-dir', '/tmp/extensions',
  ]);
  console.log(`bundle ${first.bytes.length} bytes sha256=${sha256(first.bytes)} runtime-packages=${packages.length}`);
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
