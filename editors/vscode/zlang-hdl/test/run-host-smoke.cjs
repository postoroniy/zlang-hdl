// SPDX-License-Identifier: Apache-2.0
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { downloadAndUnzipVSCode, runVSCodeCommand } = require('@vscode/test-electron');
const { toolchain } = require('./bundle.cjs');

function executableOnPath(name) {
  for (const directory of (process.env.PATH ?? '').split(path.delimiter)) {
    if (!directory) continue;
    const candidate = path.join(directory, name);
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      if (fs.lstatSync(candidate).isFile()) return fs.realpathSync(candidate);
    } catch {
      // Continue through PATH; the host must receive one exact executable.
    }
  }
  throw new Error(`${name} is unavailable on PATH`);
}

function hostLaunchArguments({ extensions, userData, installed, workspace }) {
  return [
    // The downloaded VS Code test host is an isolated, ephemeral CI process.
    // GitHub-hosted runners cannot use Chromium's setuid sandbox because the
    // downloaded chrome-sandbox is not installed root-owned with mode 4755.
    '--no-sandbox',
    '--disable-workspace-trust', '--disable-telemetry', '--skip-release-notes',
    '--extensions-dir', extensions, '--user-data-dir', userData,
    '--extensionDevelopmentPath', installed,
    '--extensionTestsPath', path.join(__dirname, 'host-smoke.cjs'), workspace,
  ];
}

function extensionInstallArguments({ vsix, extensions }) {
  return [
    '--no-sandbox',
    '--install-extension', vsix, '--force', '--extensions-dir', extensions,
  ];
}

async function main() {
  assert.equal(process.argv.length, 3, 'Usage: npm run test:host -- /absolute/path/to/zlang-hdl.vsix');
  const vsix = path.resolve(process.argv[2]);
  assert.ok(path.isAbsolute(process.argv[2]) && fs.lstatSync(vsix).isFile(), 'VSIX must be an absolute regular file');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'zlang-vsix-host-'));
  try {
    const extensions = path.join(root, 'extensions');
    const userData = path.join(root, 'user-data');
    const workspace = path.join(root, 'workspace');
    fs.mkdirSync(extensions);
    fs.mkdirSync(userData);
    fs.mkdirSync(workspace);
    const settings = path.join(workspace, '.vscode');
    fs.mkdirSync(settings);
    fs.writeFileSync(path.join(settings, 'settings.json'), `${JSON.stringify({
      'zlang.lsp.path': executableOnPath('zlang-lsp'),
    }, null, 2)}\n`);
    const executable = await downloadAndUnzipVSCode(toolchain.vscodeStable.version);
    await runVSCodeCommand(
      extensionInstallArguments({ vsix, extensions }),
      { version: toolchain.vscodeStable.version },
    );
    const installed = path.join(extensions, 'postoroniy.zlang-hdl-0.1.0');
    assert.ok(fs.existsSync(installed), `installed extension is missing: ${installed}`);
    const child = spawnSync(executable, hostLaunchArguments({
      extensions, userData, installed, workspace,
    }), {
      encoding: 'utf8',
      env: { ...process.env, ZLANG_EDITOR_SMOKE_EXTENSION: installed },
      stdio: 'inherit',
    });
    assert.equal(child.status, 0, `VS Code host smoke failed with status ${child.status}`);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

module.exports = { extensionInstallArguments, hostLaunchArguments };

if (require.main === module) {
  main().catch((error) => { console.error(error); process.exitCode = 1; });
}
