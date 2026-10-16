// SPDX-License-Identifier: Apache-2.0
'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');
const vm = require('node:vm');
const { buildBundle, runtimePackages, sha256, thirdPartyNotices } = require('./bundle.cjs');
const {
  extensionInstallArguments,
  hostLaunchArguments,
} = require('./run-host-smoke.cjs');
const { waveformHtml } = require('../waveform_view');
const { WaveformTileCache } = require('../waveform_cache');
const { waveformSearchBounds } = require('../waveform_navigation');
const { waveformMeasurement } = require('../waveform_measurement');
const {
  WAVEFORM_SESSION_SCHEMA, rebindSessionLayout, serializeSessionLayout,
} = require('../waveform_session');

async function main() {
  const first = await buildBundle();
  const second = await buildBundle();
  assert.equal(sha256(first.bytes), sha256(second.bytes), 'runtime bundle is not deterministic');
  assert.deepEqual(first.bytes, second.bytes, 'runtime bundle bytes changed between builds');
  assert.deepEqual(first.waveform, second.waveform, 'waveform browser bundle is not deterministic');
  assert.ok(first.waveform.length < 150_000);
  assert.equal(first.waveform.includes('sourceMappingURL='), false);
  assert.equal(first.waveform.includes('waveformProfile'), false,
    'private browser profiling probes must not enter the packaged waveform');
  assert.equal(first.waveform.includes('/home/'), false);
  assert.ok(first.bytes.length < 600_000, `runtime bundle is unexpectedly large: ${first.bytes.length}`);
  const text = first.bytes.toString('utf8');
  assert.equal(text.includes('sourceMappingURL='), false);
  assert.equal(text.includes('/home/'), false);
  assert.equal(text.includes('vendor/node_modules'), false);
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
  assert.deepEqual(waveformSearchBounds(3.6, 0, 21, false), { start: 4, end: 21 });
  assert.deepEqual(waveformSearchBounds(3.6, 0, 21, true), { start: 0, end: 3 });
  assert.deepEqual(waveformSearchBounds(4, 0, 21, false), { start: 5, end: 21 });
  assert.deepEqual(waveformSearchBounds(4, 0, 21, true), { start: 0, end: 3 });
  assert.equal(waveformSearchBounds(21, 0, 21, false), null);
  assert.equal(waveformSearchBounds(0, 0, 21, true), null);
  assert.equal(WAVEFORM_SESSION_SCHEMA, 'zlang-waveform-session-v1');
  const cache = new WaveformTileCache(48);
  cache.set('a', { value: 'one' });
  cache.set('b', { value: 'two' });
  assert.deepEqual(cache.get('a'), { value: 'one' });
  cache.set('c', { value: 'a payload which forces deterministic eviction' });
  assert.ok(cache.bytes <= 48);
  const catalog = [{ id: 'v1', name: 'Top.count', width: 8 }];
  const serialized = serializeSessionLayout([
    { id: 'row-1', type: 'signal', signalId: 'v1', format: 'unsigned' },
  ], new Map(catalog.map((item) => [item.id, item])));
  assert.deepEqual(serialized[0].signal, { name: 'Top.count', width: 8 });
  assert.equal(rebindSessionLayout(serialized, catalog)[0].signalId, 'v1');
  assert.equal(rebindSessionLayout(serialized, [])[0].unresolved.name, 'Top.count');
  const measurement = waveformMeasurement({width:1,start_bits:'0',end_bits:'0',
    rising:1,falling:1,transitions:2,high_ticks:4,low_ticks:4,unknown_ticks:0},
  0,8,{time_domain:'zlang_event_ticks',ticks_per_event:2});
  assert.deepEqual(
    { rising: measurement.rising, falling: measurement.falling, high: measurement.highTicks,
      low: measurement.lowTicks, events: measurement.eventDelta },
    { rising: 1, falling: 1, high: 4, low: 4, events: 4 },
  );
  const waveform = waveformHtml(first.waveform.toString('utf8'));
  for (const required of [
    'Design Browser', 'Signal / group', 'Inspector &amp; Measurements', 'Shift+wheel',
    'middle drag', 'signal: all displayed', 'selected change',
    'Jump to the next change of the selected signal',
    'zlang-waveform-session-v1', 'Enter X', 'Enter U',
  ]) assert.ok(waveform.includes(required), `waveform UI omitted ${required}`);
  assert.equal(waveform.includes('/home/'), false);
  const inlineScript = waveform.match(/<script nonce="[^"]+">([\s\S]*?)<\/script>/);
  assert.ok(inlineScript, 'waveform UI omitted its inline webview script');
  assert.doesNotThrow(
    () => new vm.Script(inlineScript[1], { filename: 'zlang-waveform-webview.js' }),
    'generated waveform webview script must parse before VS Code loads it',
  );
  console.log(`bundle ${first.bytes.length} bytes sha256=${sha256(first.bytes)} runtime-packages=${packages.length}`);
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
