// SPDX-License-Identifier: Apache-2.0
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'typed-waveform-host.cjs'), 'utf8');

async function exerciseLaunch({ synchronous = false, accepted = true,
  entry = true, unrelated = false, requestError = 'variables reached' } = {}) {
  let factory;
  let pendingLaunch;
  let now = 0;
  const state = { oldRequests: 0, newRequests: 0, stopped: [], errors: [], disposed: false };
  const oldSession = {
    id: 'old', type: 'zlang-hdl', configuration: { top: 'DebugSmoke' },
    async customRequest() {
      state.oldRequests++;
      throw new Error('custom request failed');
    },
  };
  const debug = {
    activeDebugSession: oldSession,
    registerDebugAdapterTrackerFactory(type, value) {
      assert.equal(type, 'zlang-hdl');
      factory = value;
      return { dispose() { state.disposed = true; } };
    },
    async startDebugging(folder, configuration) {
      assert.equal(folder, undefined);
      if (!accepted) return false;
      if (unrelated) {
        factory?.createDebugAdapterTracker({
          id: 'unrelated', configuration: { ...configuration, program: '/other.zhl' },
        })?.onDidSendMessage({ event: 'stopped', body: { reason: 'entry' } });
      }
      const launch = () => {
        let tracker;
        const session = {
          id: 'new', type: 'zlang-hdl', configuration,
          async customRequest(command) {
            state.newRequests++;
            tracker?.onDidSendMessage({
              type: 'response', command, request_seq: 7, success: false,
              message: requestError,
            });
            throw new Error(requestError);
          },
        };
        tracker = factory?.createDebugAdapterTracker(session);
        debug.activeDebugSession = session;
        if (entry) tracker?.onDidSendMessage({ event: 'stopped', body: { reason: 'entry' } });
      };
      if (synchronous) launch();
      else pendingLaunch = launch;
      return true;
    },
    async stopDebugging(session) { state.stopped.push(session.id); },
  };
  const loadedModule = { exports: {} };
  vm.runInNewContext(source, {
    module: loadedModule, exports: loadedModule.exports, Promise, Date: { now: () => now },
    setTimeout(callback, delay) {
      now += delay;
      const launch = pendingLaunch;
      pendingLaunch = undefined;
      launch?.();
      Promise.resolve().then(callback);
    },
    console: { log() {}, error(value) { state.errors.push(value); } },
    require(name) {
      if (name === 'vscode') return { debug };
      if (name === 'node:fs') return { writeFileSync() {} };
      if (name === './waveform-host.cjs') return {};
      return require(name);
    },
  });
  try {
    await loadedModule.exports.checkTypedWaveformHost('/tmp/typed-host-fixture');
  } catch (error) {
    state.error = error.message;
  }
  state.elapsed = now;
  return state;
}

test('typed host waits for the matching session instead of querying the stale active session', async () => {
  const state = await exerciseLaunch();
  assert.equal(state.error, 'variables reached');
  assert.equal(state.oldRequests, 0);
  assert.equal(state.newRequests, 1);
  assert.deepEqual(state.stopped, ['new']);
  assert.equal(state.disposed, true);
});

test('typed host retains an entry stop delivered before startDebugging resolves', async () => {
  const state = await exerciseLaunch({ synchronous: true });
  assert.equal(state.error, 'variables reached');
  assert.equal(state.newRequests, 1);
  assert.equal(state.elapsed, 0);
});

test('typed host ignores an unrelated launch and its entry stop', async () => {
  const state = await exerciseLaunch({ unrelated: true });
  assert.equal(state.error, 'variables reached');
  assert.equal(state.oldRequests, 0);
  assert.deepEqual(state.stopped, ['new']);
});

test('an unrelated entry stop cannot satisfy readiness of the matching launch', async () => {
  const state = await exerciseLaunch({ unrelated: true, entry: false });
  assert.match(state.error, /Typed waveform entry did not become ready/);
  assert.equal(state.newRequests, 0);
  assert.equal(state.elapsed, 10000);
});

test('typed host preserves the existing entry deadline and never queries before entry', async () => {
  const state = await exerciseLaunch({ entry: false });
  assert.match(state.error, /Typed waveform entry did not become ready/);
  assert.equal(state.elapsed, 10000);
  assert.equal(state.oldRequests, 0);
  assert.equal(state.newRequests, 0);
  assert.deepEqual(state.stopped, ['new']);
  assert.equal(state.disposed, true);
});

test('a rejected typed launch leaves the old session alone and disposes its tracker', async () => {
  const state = await exerciseLaunch({ accepted: false });
  assert.equal(state.newRequests, 0);
  assert.deepEqual(state.stopped, []);
  assert.equal(state.disposed, true);
  assert.ok(state.error);
});

test('typed host retains failed DAP command details and propagates the native error', async () => {
  const state = await exerciseLaunch({ requestError: 'native request rejected' });
  assert.equal(state.error, 'native request rejected');
  assert.equal(state.newRequests, 1);
  assert.match(state.errors[0], /"command":"variables"/);
  assert.match(state.errors[0], /"request_seq":7/);
  assert.match(state.errors[0], /native request rejected/);
  assert.equal(state.disposed, true);
});
