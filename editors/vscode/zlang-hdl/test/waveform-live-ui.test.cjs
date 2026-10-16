// SPDX-License-Identifier: Apache-2.0
'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const vm = require('node:vm');
const { appendTypedLiveRows, receiveLiveStatus } = require('../waveform_live_ui');
const { walk } = require('../waveform_workspace');
const {
  MAX_SESSION_ITEMS, MAX_LAYOUT_ITEMS, waveformLayoutUsage, serializeSessionLayout, rebindSessionLayout,
} = require('../waveform_session');

function liveRows(layout) {
  const state = { layout };
  const messages = [];
  let sequence = 0;
  const append = vm.runInNewContext(`(${appendTypedLiveRows.toString()})`, {
    state, walk, MAX_SESSION_ITEMS, MAX_LAYOUT_ITEMS, waveformLayoutUsage,
    uid: prefix => `${prefix}-${++sequence}`,
    el: () => ({ set textContent(value) { messages.push(value); } }),
  });
  return { append, state, messages };
}

function roundTrip(layout, catalog) {
  const byId = new Map(catalog.map(signal => [signal.id, signal]));
  const encoded = serializeSessionLayout(layout, byId);
  assert.doesNotThrow(() => rebindSessionLayout(encoded, catalog));
}

const q = { id: 'q', name: 'Top.q', width: 1 };
const r = { id: 'r', name: 'Top.r', width: 1 };
const copies = count => Array.from({ length: count }, (_, index) => ({
  id: `copy-${index}`, type: 'signal', signalId: q.id,
}));

test('live additions count collapsed rows and duplicate channels against the signal cap', () => {
  const layout = [{ id: 'g', type: 'group', name: 'G', collapsed: true,
    children: copies(MAX_SESSION_ITEMS) }];
  const before = JSON.stringify(layout);
  const { append, messages } = liveRows(layout);
  append([r]);
  assert.ok(JSON.stringify(layout) === before, 'full layout must remain unchanged');
  assert.match(messages.at(-1), /4096/);
  roundTrip(layout, [q, r]);
});

test('live additions retain exactly the last available signal slot', () => {
  const layout = [{ id: 'g', type: 'group', name: 'G', collapsed: true,
    children: copies(MAX_SESSION_ITEMS - 1) }];
  const { append } = liveRows(layout);
  append([r, { id: 'extra', name: 'Top.extra', width: 1 }]);
  assert.equal(layout.length, 2);
  assert.equal(layout[1].signalId, r.id);
  roundTrip(layout, [q, r]);
});

test('live additions respect the total row cap without leaving partial groups', () => {
  const layout = Array.from({ length: MAX_LAYOUT_ITEMS - 1 }, (_, index) => ({
    id: `divider-${index}`, type: 'divider', name: '',
  }));
  const before = JSON.stringify(layout);
  const { append, messages } = liveRows(layout);
  append([{ ...r, groups: ['Top', 'Top.payload'] }]);
  assert.ok(JSON.stringify(layout) === before, 'failed insertion must not leave partial groups');
  assert.match(messages.at(-1), /8192/);
  roundTrip(layout, [r]);
  append([r]);
  assert.equal(layout.length, MAX_LAYOUT_ITEMS);
  roundTrip(layout, [r]);
});

test('live additions reuse existing groups without charging them twice', () => {
  const layout = [{ id: 'g', type: 'group', name: 'Top', collapsed: true, children: [] },
    ...Array.from({ length: MAX_LAYOUT_ITEMS - 2 }, (_, index) => ({
      id: `divider-${index}`, type: 'divider', name: '',
    }))];
  const { append } = liveRows(layout);
  append([{ ...r, groups: ['Top'] }]);
  assert.deepEqual(waveformLayoutUsage(layout), { rows: MAX_LAYOUT_ITEMS, signals: 1 });
  assert.equal(layout[0].children[0].signalId, r.id);
  roundTrip(layout, [r]);
});

test('live update delegates the hierarchy refresh to one full render', () => {
  let renders = 0, trees = 0;
  const state = {
    live: true, liveSession: 's', liveEpoch: 0, catalog: [], layout: [],
    selectedRowIds: new Set(), selectedSignalIds: new Set(), viewports: [{ start: 0, end: 10 }],
  };
  const update = vm.runInNewContext(`(${receiveLiveStatus.toString()})`, {
    state, scope: () => '', exportWaveformSession: () => ({}),
    updateTraceStatus() {}, cancelWaveformQueries() {}, scheduleQuery() {},
    buildSourceTree: () => { trees++; },
    renderAll: () => { renders++; trees++; },
    el: () => ({ classList: { toggle() {} } }),
  });
  update({ sessionId: 's', epoch: 0, event: 2, signals: [] });
  assert.equal(renders, 1);
  assert.equal(trees, 1);
});
