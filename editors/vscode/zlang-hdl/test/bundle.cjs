// SPDX-License-Identifier: Apache-2.0
'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const moduleBuiltin = require('node:module').builtinModules;
const path = require('node:path');
const esbuild = require('esbuild');

const extension = path.resolve(__dirname, '..');
const toolchain = JSON.parse(fs.readFileSync(path.join(extension, 'editor-toolchain.json'), 'utf8'));

function json(relative) {
  return JSON.parse(fs.readFileSync(path.join(extension, relative), 'utf8'));
}

function assertToolchain() {
  const manifest = json('package.json');
  const installedClient = json('node_modules/vscode-languageclient/package.json');
  const installedEsbuild = json('node_modules/esbuild/package.json');
  const installedTest = json('node_modules/@vscode/test-electron/package.json');
  assert.equal(manifest.engines.vscode, `^${toolchain.vscodeStable.version}`);
  assert.equal(manifest.dependencies['vscode-languageclient'], toolchain.vscodeLanguageClient);
  assert.equal(manifest.devDependencies.esbuild, toolchain.esbuild);
  assert.equal(manifest.devDependencies['@vscode/test-electron'], toolchain.vscodeTestElectron);
  assert.equal(installedClient.version, toolchain.vscodeLanguageClient);
  assert.equal(installedEsbuild.version, toolchain.esbuild);
  assert.equal(installedTest.version, toolchain.vscodeTestElectron);
}

async function buildBundle() {
  assertToolchain();
  const result = await esbuild.build({
    absWorkingDir: extension,
    entryPoints: ['extension.js'],
    bundle: true,
    platform: 'node',
    format: 'cjs',
    target: 'node20',
    minify: true,
    legalComments: 'none',
    sourcemap: false,
    sourcesContent: false,
    write: false,
    metafile: true,
    external: ['vscode'],
    logLevel: 'silent',
  });
  assert.equal(result.outputFiles.length, 1, 'the runtime build must have one output');
  const output = result.outputFiles[0].contents;
  const builtin = new Set(moduleBuiltin.flatMap((name) => [name, `node:${name}`]));
  const outputMetadata = result.metafile.outputs[Object.keys(result.metafile.outputs)[0]];
  for (const item of outputMetadata.imports) {
    assert.ok(item.external, `unbundled internal import: ${item.path}`);
    assert.ok(item.path === 'vscode' || builtin.has(item.path), `unexpected external import: ${item.path}`);
  }
  for (const input of Object.keys(result.metafile.inputs)) {
    const normalized = input.replaceAll('\\', '/');
    assert.ok(normalized === 'extension.js' || normalized.includes('/node_modules/') || normalized.startsWith('node_modules/'),
      `unexpected bundle input: ${input}`);
  }
  return { bytes: Buffer.from(output), metafile: result.metafile };
}

function licenseFile(packageDirectory) {
  const matches = fs.readdirSync(packageDirectory)
    .filter((name) => /^licen[cs]e(?:\.|$)/i.test(name))
    .sort((left, right) => left.localeCompare(right));
  assert.ok(matches.length > 0, `missing license text in ${packageDirectory}`);
  const selected = path.join(packageDirectory, matches[0]);
  assert.ok(fs.lstatSync(selected).isFile(), `license is not a regular file: ${selected}`);
  return selected;
}

function runtimePackages() {
  const lock = json('package-lock.json');
  const records = [];
  for (const [packagePath, metadata] of Object.entries(lock.packages)) {
    if (!packagePath.startsWith('node_modules/') || metadata.dev === true) continue;
    const directory = path.join(extension, packagePath);
    const manifest = JSON.parse(fs.readFileSync(path.join(directory, 'package.json'), 'utf8'));
    const license = licenseFile(directory);
    records.push({
      name: manifest.name,
      version: manifest.version,
      license: manifest.license,
      licenseText: fs.readFileSync(license, 'utf8').trimEnd(),
    });
  }
  records.sort((left, right) => `${left.name}@${left.version}`.localeCompare(`${right.name}@${right.version}`));
  return records;
}

function thirdPartyNotices(packages = runtimePackages()) {
  const sections = packages.map((record) => [
    `${record.name}@${record.version}`,
    `Declared license: ${record.license}`,
    '',
    record.licenseText,
  ].join('\n'));
  return [
    'ZLang HDL VS Code extension — third-party notices',
    '',
    'The extension runtime bundles the following packages. Their license texts follow.',
    '',
    sections.join('\n\n' + '='.repeat(72) + '\n\n'),
    '',
  ].join('\n');
}

function sha256(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}

module.exports = {
  assertToolchain,
  buildBundle,
  extension,
  runtimePackages,
  sha256,
  thirdPartyNotices,
  toolchain,
};
