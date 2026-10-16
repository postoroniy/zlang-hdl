// SPDX-License-Identifier: Apache-2.0
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const esbuild = require('esbuild');

const root = path.resolve(__dirname, '..');
const domains = [
  'navigation', 'measurement', 'session', 'rows', 'holders', 'clipboard', 'clipboard_ui', 'dialog', 'session_ui', 'workspace', 'renderer', 'browser',
  'viewport', 'presentation', 'inspector', 'queries', 'controls', 'live_ui',
];

// Compose the domain functions in one browser scope, then minify that scope
// once. Function source is never reconstructed from minified extension code.
function waveformSource({ instrumentation = '' } = {}) {
  const declarations = [];
  for (const domain of domains) {
    const exports = require(path.join(root, `waveform_${domain}.js`));
    for (const [name, value] of Object.entries(exports)) {
      declarations.push(typeof value === 'function'
        ? value.toString() : `const ${name}=${JSON.stringify(value)};`);
    }
  }
  const app = fs.readFileSync(path.join(root, 'waveform_app.js'), 'utf8');
  return `${declarations.join('\n')}\n${app}\n${instrumentation}`;
}

function buildWaveformBundle(options) {
  return Buffer.from(esbuild.transformSync(waveformSource(options), {
    target: 'es2022', minify: true, format: 'iife', legalComments: 'none',
    sourcemap: false, sourcefile: 'waveform.js',
  }).code, 'utf8');
}

module.exports = { buildWaveformBundle, waveformSource,
  browserFiles: [...domains.map(domain => `waveform_${domain}.js`), 'waveform_app.js'] };
