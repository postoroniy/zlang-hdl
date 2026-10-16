// SPDX-License-Identifier: Apache-2.0
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
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
    '--disable-extension', 'GitHub.copilot-chat',
    // Keep the test inside xvfb even when the interactive host uses Wayland.
    '--ozone-platform=x11',
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

function assertCleanTransportLogs(userData) {
  const logs=path.join(userData,'logs');
  assert.ok(fs.existsSync(logs),'VS Code host did not create diagnostic logs');
  const visit=directory=>{
    for(const entry of fs.readdirSync(directory,{withFileTypes:true})){
      const file=path.join(directory,entry.name);
      if(entry.isDirectory())visit(file);
      else if(entry.isFile()&&entry.name.endsWith('.log')){
        assert.doesNotMatch(fs.readFileSync(file,'utf8'),
          /Unexpected SIGPIPE|Error: (?:write )?EPIPE|DAP session is disconnected/,
          `VS Code transport failed despite successful functional checks: ${file}`);
      }
    }
  };
  visit(logs);
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
    const port=await new Promise((resolve,reject)=>{
      const probe=net.createServer();probe.once('error',reject);
      probe.listen(0,'127.0.0.1',()=>{const selected=probe.address().port;probe.close(()=>resolve(selected));});
    });
    fs.writeFileSync(path.join(settings, 'settings.json'), `${JSON.stringify({
      'zlang.lsp.path': executableOnPath('zlang-lsp'),
      'debug.allowBreakpointsEverywhere':false,
      'zlang.waveform.wcp.enabled':true,
      'zlang.waveform.wcp.port':port,
    }, null, 2)}\n`);
    const executable = await downloadAndUnzipVSCode(toolchain.vscodeStable.version);
    await runVSCodeCommand(
      extensionInstallArguments({ vsix, extensions }),
      { version: toolchain.vscodeStable.version },
    );
    const installedNames = fs.readdirSync(extensions).filter((name) =>
      name === 'postoroniy.zlang-hdl-0.1.0'
      || name === 'postoroniy.zlang-hdl-0.1.0-linux-x64');
    assert.equal(installedNames.length, 1,
      `expected one installed ZLang extension, found: ${installedNames.join(', ')}`);
    const installed = path.join(extensions, installedNames[0]);
    const child = spawnSync(executable, hostLaunchArguments({
      extensions, userData, installed, workspace,
    }), {
      encoding: 'utf8',
      env: { ...process.env, ZLANG_EDITOR_SMOKE_EXTENSION: installed,ZLANG_EDITOR_SMOKE_WCP_PORT:String(port) },
      stdio: 'inherit',
    });
    assert.equal(child.status, 0, `VS Code host smoke failed with status ${child.status}`);
    assertCleanTransportLogs(userData);
  } finally {
    if(process.env.KEEP_TMP==='1')console.log('Retained VS Code host evidence: '+root);
    else fs.rmSync(root, { recursive: true, force: true });
  }
}

module.exports = { extensionInstallArguments, hostLaunchArguments, assertCleanTransportLogs };

if (require.main === module) {
  main().catch((error) => { console.error(error); process.exitCode = 1; });
}
