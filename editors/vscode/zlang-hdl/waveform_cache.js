// SPDX-License-Identifier: Apache-2.0
'use strict';

const DEFAULT_MAX_BYTES = 128 * 1024 * 1024;

// One WaveformDocument owns one cache. Values are bounded query responses;
// waveform samples never become process-global editor state.
class WaveformTileCache {
  constructor(maxBytes = DEFAULT_MAX_BYTES) {
    this.maxBytes = maxBytes;
    this.bytes = 0;
    this.entries = new Map();
  }

  get(key) {
    const entry = this.entries.get(key);
    if (entry === undefined) return undefined;
    this.entries.delete(key);
    this.entries.set(key, entry);
    return entry.value;
  }

  set(key, value) {
    const size = Buffer.byteLength(key, 'utf8')+Buffer.byteLength(JSON.stringify(value), 'utf8');
    const previous = this.entries.get(key);
    if (previous !== undefined) this.bytes -= previous.size;
    this.entries.delete(key);
    if (size > this.maxBytes) return;
    this.entries.set(key, { size, value });
    this.bytes += size;
    while (this.bytes > this.maxBytes && this.entries.size > 0) {
      const oldestKey = this.entries.keys().next().value;
      const oldest = this.entries.get(oldestKey);
      this.entries.delete(oldestKey);
      this.bytes -= oldest.size;
    }
  }

  clear() {
    this.entries.clear();
    this.bytes = 0;
  }
}

function waveformQueryKey(command, fields) {
  return `${command}:${JSON.stringify(fields)}`;
}

module.exports = { DEFAULT_MAX_BYTES, WaveformTileCache, waveformQueryKey };
