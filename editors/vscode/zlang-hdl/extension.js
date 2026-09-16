// SPDX-License-Identifier: Apache-2.0
// Thin Community client for the repository's standard zlang-lsp process.
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vscode = require('vscode');

// Development resolves the pinned package normally.  Production packaging
// bundles it into the single generated extension entrypoint.
const { LanguageClient, TransportKind } = require('vscode-languageclient/node');

const LANGUAGE_ID = 'zlang-hdl';
const CONFIGURATION_SECTION = 'zlang.lsp';
const SERVER_PATH_SETTING = 'path';

let client;
let output;

function workspaceRoot() {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? null;
}

function expandConfiguredPath(value) {
  const root = workspaceRoot();
  if (value.includes('${workspaceFolder}')) {
    if (root === null) {
      throw new Error(
        'zlang.lsp.path uses ${workspaceFolder}, but no workspace is open',
      );
    }
    return value.replaceAll('${workspaceFolder}', root);
  }
  return value;
}

function isExecutableFile(candidate) {
  try {
    const stat = fs.statSync(candidate);
    if (!stat.isFile()) return false;
    if (process.platform === 'win32') return true;
    fs.accessSync(candidate, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function findOnPath(command) {
  if (path.isAbsolute(command) || command.includes(path.sep)) {
    return isExecutableFile(command) ? command : null;
  }
  const suffixes = process.platform === 'win32' ? ['', '.exe', '.cmd', '.bat'] : [''];
  for (const directory of (process.env.PATH ?? '').split(path.delimiter)) {
    if (!directory) continue;
    for (const suffix of suffixes) {
      const candidate = path.join(directory, `${command}${suffix}`);
      if (isExecutableFile(candidate)) return candidate;
    }
  }
  return null;
}

function resolveServerCommand() {
  const configured = vscode.workspace
    .getConfiguration(CONFIGURATION_SECTION)
    .get(SERVER_PATH_SETTING, '');
  if (typeof configured !== 'string') {
    throw new Error('zlang.lsp.path must be a direct executable path or command name');
  }
  const requested = configured.trim() || 'zlang-lsp';
  const expanded = expandConfiguredPath(requested);
  const candidate = path.isAbsolute(expanded)
    ? expanded
    : (expanded.includes(path.sep) ? path.resolve(workspaceRoot() ?? process.cwd(), expanded) : expanded);
  const resolved = findOnPath(candidate);
  if (resolved === null) {
    const hint = requested === 'zlang-lsp'
      ? 'Install ZLang in the active environment or set zlang.lsp.path.'
      : 'Set zlang.lsp.path to an executable zlang-lsp path.';
    throw new Error(`Cannot find zlang-lsp (${requested}). ${hint}`);
  }
  return resolved;
}

function reportStartupError(error) {
  const message = error instanceof Error ? error.message : String(error);
  output?.appendLine(`[error] ${message}`);
  void vscode.window.showErrorMessage(`ZLang HDL: ${message}`, 'Open Settings').then((choice) => {
    if (choice === 'Open Settings') {
      void vscode.commands.executeCommand('workbench.action.openSettings', '@ext:postoroniy.zlang-hdl zlang.lsp.path');
    }
  });
}

async function activate(context) {
  // Language Client 10 consumes the structured LogOutputChannel surface.
  output = vscode.window.createOutputChannel('ZLang HDL', { log: true });
  context.subscriptions.push(output);
  let command;
  try {
    command = resolveServerCommand();
  } catch (error) {
    reportStartupError(error);
    return;
  }

  output.appendLine(`Starting zlang-lsp: ${command}`);
  const serverOptions = {
    run: { command, transport: TransportKind.stdio },
    debug: { command, transport: TransportKind.stdio },
  };
  const clientOptions = {
    documentSelector: [{ scheme: 'file', language: LANGUAGE_ID }],
    outputChannel: output,
    synchronize: { configurationSection: CONFIGURATION_SECTION },
  };
  client = new LanguageClient(
    'zlangHdlLanguageServer',
    'ZLang HDL Language Server',
    serverOptions,
    clientOptions,
  );
  context.subscriptions.push(client);
  try {
    // VS Code waits for the activation promise before dispatching providers.
    // Returning while start() is still registering capabilities creates a
    // real F12 race: the first request sees no definition provider at all.
    await client.start();
  } catch (error) {
    reportStartupError(error);
  }
}

async function deactivate() {
  if (client === undefined) return undefined;
  const running = client;
  client = undefined;
  if (!running.isRunning()) return undefined;
  return running.stop();
}

module.exports = { activate, deactivate };
