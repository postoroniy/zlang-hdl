// SPDX-License-Identifier: Apache-2.0
'use strict';

// One row geometry is shared by the signal list, canvas and hit testing.
function waveformRows(items, defaultHeight = () => 34) {
  const rows = [];
  let top = 0;
  function append(children, depth) {
    for (const item of children) {
      const height = item.type === 'group' ? 28 : item.type === 'divider' ? 24
        : Math.max(24, Math.min(128, Number(item.height) || defaultHeight(item)));
      rows.push({ item, depth, top, height });
      top += height;
      if (item.type === 'group' && !item.collapsed) append(item.children, depth + 1);
    }
  }
  append(items, 0);
  return { rows, height: top };
}

function waveformRowWindow(rows, scrollTop, height, overscan = 64) {
  let low = 0;
  let high = rows.length;
  const start = Math.max(0, scrollTop - overscan);
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (rows[middle].top + rows[middle].height <= start) low = middle + 1;
    else high = middle;
  }
  const first = low;
  while (low < rows.length && rows[low].top < scrollTop + height + overscan) low += 1;
  return rows.slice(first, low);
}

// Visual rows may repeat a channel; native queries must request it only once.
function waveformSignalIds(items){return [...new Set(items.filter(item=>item.type==='signal'&&item.signalId).map(item=>item.signalId))];}
module.exports = { waveformRows, waveformRowWindow, waveformSignalIds };
