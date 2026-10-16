// SPDX-License-Identifier: Apache-2.0
'use strict';

function waveformSearchBounds(cursor, minimum, maximum, reverse) {
  const lower = Math.ceil(minimum);
  const upper = Math.floor(maximum);
  const start = reverse ? lower : Math.max(lower, Math.floor(cursor) + 1);
  const end = reverse ? Math.min(upper, Math.ceil(cursor) - 1) : upper;
  return start <= end ? { start, end } : null;
}

module.exports = { waveformSearchBounds };
