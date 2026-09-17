// SPDX-License-Identifier: Apache-2.0
// Build a reviewed local VSIX with one bundled runtime and no node_modules tree.
'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createVSIX } = require('@vscode/vsce');
const { buildBundle, extension, thirdPartyNotices } = require('./bundle.cjs');

const repository = path.resolve(extension, '../../..');
const staticAssets = [
  'README.md', 'CHANGELOG.md', 'language-configuration.json', 'recommended-settings.json',
  'syntaxes/zlang.tmLanguage.json', 'snippets/zlang-hdl.json',
];

async function main() {
  if (process.argv.length !== 3 || !path.isAbsolute(process.argv[2]) || !process.argv[2].endsWith('.vsix')) {
    throw new Error('Usage: npm run package -- /absolute/output/zlang-hdl-0.1.0.vsix');
  }
  const output = path.resolve(process.argv[2]);
  const reservation = fs.openSync(output, 'wx');
  fs.closeSync(reservation);
  const stage = fs.mkdtempSync(path.join(os.tmpdir(), 'zlang-editor-package-'));
  try {
    for (const relative of staticAssets) {
      const source = path.join(extension, relative);
      if (!fs.lstatSync(source).isFile()) throw new Error(`Not a regular asset: ${relative}`);
      const destination = path.join(stage, relative);
      fs.mkdirSync(path.dirname(destination), { recursive: true });
      fs.copyFileSync(source, destination);
    }
    const manifest = JSON.parse(fs.readFileSync(path.join(extension, 'package.json'), 'utf8'));
    manifest.main = './dist/extension.js';
    delete manifest.files;
    delete manifest.devDependencies;
    delete manifest.scripts;
    fs.writeFileSync(path.join(stage, 'package.json'), `${JSON.stringify(manifest, null, 2)}\n`);
    const bundle = await buildBundle();
    fs.mkdirSync(path.join(stage, 'dist'));
    fs.writeFileSync(path.join(stage, 'dist', 'extension.js'), bundle.bytes);
    fs.writeFileSync(path.join(stage, 'THIRD_PARTY_NOTICES.txt'), thirdPartyNotices());
    for (const relative of ['LICENSE', 'NOTICE']) {
      fs.copyFileSync(path.join(repository, relative), path.join(stage, relative));
    }
    fs.writeFileSync(path.join(stage, '.vscodeignore'), [
      'extension.js', 'package-lock.json', 'editor-toolchain.json', 'test/**', 'examples/**',
      'node_modules/**', '**/*.map', '**/*.ts', '**/*.d.ts',
    ].join('\n') + '\n');
    await createVSIX({ cwd: stage, packagePath: output, dependencies: false });
    const bytes = fs.readFileSync(output);
    const sha256 = crypto.createHash('sha256').update(bytes).digest('hex');
    console.log(`SHA256 ${sha256}  ${output}`);
    console.log('Packaged locally. Nothing was published. Audit the VSIX before installation.');
  } catch (error) {
    fs.unlinkSync(output);
    throw error;
  } finally {
    fs.rmSync(stage, { recursive: true, force: true });
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
