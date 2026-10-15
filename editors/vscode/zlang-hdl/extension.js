// SPDX-License-Identifier: Apache-2.0
// Thin Community client for the repository's standard zlang-lsp process.
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const childProcess = require('node:child_process');
const vscode = require('vscode');

// Development resolves the pinned package normally.  Production packaging
// bundles it into the single generated extension entrypoint.
const { LanguageClient, TransportKind } = require('vscode-languageclient/node');
const { waveformHtml } = require('./waveform_view');
const { WaveformQueryClient } = require('./waveform_client');
const { WaveformControlServer } = require('./waveform_wcp');

const LANGUAGE_ID = 'zlang-hdl';
const CONFIGURATION_SECTION = 'zlang.lsp';
const SERVER_PATH_SETTING = 'path';
const DEBUG_ADAPTER_SCHEMA = 'zlang-debug-adapter-v1';
const WAVEFORM_TRACE_SCHEMA = 'zlang-waveform-trace-v1';
const WAVEFORM_QUERY_SCHEMA = 'zlang-waveform-query-v2';

let client;
let output;
let nativeTools;
let nativeToolsError;
let liveWaveformPanel;
let liveWaveformChannel;
let activeWaveformChannel;
let waveformControlServer;

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

function resolveNativeTools(context) {
  if (process.platform !== 'linux' || process.arch !== 'x64') return null;
  const root = context.asAbsolutePath(path.join('bin', 'linux-x64'));
  const adapter = path.join(root, 'zlang-debug-adapter');
  const indexer = path.join(root, 'zlang-waveform-indexer');
  if (!isExecutableFile(adapter) || !isExecutableFile(indexer)) return null;
  const actual = childProcess.execFileSync(adapter, ['--version'], {
    encoding: 'utf8', timeout: 5000,
  }).trim();
  if (actual !== DEBUG_ADAPTER_SCHEMA) {
    throw new Error(
      `Native debug adapter schema mismatch: expected ${DEBUG_ADAPTER_SCHEMA}, got ${actual || '<empty>'}.`,
    );
  }
  const indexerSchema = childProcess.execFileSync(indexer, ['--version'], {
    encoding: 'utf8', timeout: 5000,
  }).trim();
  if (indexerSchema !== WAVEFORM_QUERY_SCHEMA) {
    throw new Error(
      `Native waveform query schema mismatch: expected ${WAVEFORM_QUERY_SCHEMA}, got ${indexerSchema || '<empty>'}.`,
    );
  }
  return { adapter, indexer };
}

function expandLaunchPath(value, file) {
  return expandConfiguredPath(value).replaceAll('${file}', file ?? '');
}

function activeProgram(config) {
  const editorFile = vscode.window.activeTextEditor?.document.uri.fsPath;
  if (typeof config.program === 'string' && config.program.length > 0) {
    const expanded = expandLaunchPath(config.program, editorFile);
    if (expanded.length === 0) throw new Error('${file} requires an active saved editor.');
    return expanded;
  }
  const editor = vscode.window.activeTextEditor;
  if (editor?.document.languageId !== LANGUAGE_ID) {
    throw new Error('Open a saved ZLang HDL source or set debug program.');
  }
  return editor.document.uri.fsPath;
}

async function prepareDebugConfiguration(context, config) {
  if (nativeTools === null) {
    throw new Error(nativeToolsError ?? 'Native debugging requires the ZLang HDL linux-x64 extension.');
  }
  if (client === undefined || !client.isRunning()) {
    throw new Error('zlang-lsp must be running before native debugging starts.');
  }
  if (config.logicState === false) {
    throw new Error('Native debugging requires four-state logic; set logicState to true.');
  }
  const program = activeProgram(config);
  const document = await vscode.workspace.openTextDocument(vscode.Uri.file(program));
  if (document?.isDirty) throw new Error('Save All before debugging.');
  const project = config.project ? expandLaunchPath(config.project, program) : undefined;
  const existingProject = project && fs.existsSync(project) ? project : undefined;
  const bundle = await client.sendRequest('zlang/prepareDebugBundle', {
    textDocument: { uri: vscode.Uri.file(program).toString() },
    top: config.top,
    project: existingProject,
    profile: config.profile,
    logicState: config.logicState ?? true,
  });
  const directory = path.join(context.globalStorageUri.fsPath, 'debug', bundle.identity);
  fs.mkdirSync(directory, { recursive: true });
  const planPath = path.join(directory, 'plan.json');
  const overlayPath = path.join(directory, 'overlay.json');
  fs.writeFileSync(planPath, Buffer.from(bundle.plan, 'base64'));
  const overlay = { ...bundle };
  delete overlay.plan;
  fs.writeFileSync(overlayPath, `${JSON.stringify(overlay)}\n`);
  return {
    ...config,
    type: 'zlang-hdl',
    request: 'launch',
    program,
    planPath,
    overlayPath,
    traceStore: path.join(directory, 'trace'),
    events: config.events ? expandLaunchPath(config.events, program) : undefined,
  };
}

class ZLangDebugConfigurationProvider {
  constructor(context) { this.context = context; }

  provideDebugConfigurations() {
    return [{
      type: 'zlang-hdl', request: 'launch', name: 'Debug ZLang HDL',
      program: '${file}', top: 'Top',
      project: '${workspaceFolder}/zlang.toml', profile: 'portable',
      logicState: true, strictUninitialized: false,
      initialRegisters: {}, traceSignals: [], stopOnEntry: true,
    }];
  }

  async resolveDebugConfiguration(_folder, config) {
    return prepareDebugConfiguration(this.context, config);
  }
}

class ZLangDebugAdapterFactory {
  createDebugAdapterDescriptor() {
    if (nativeTools === null) {
      throw new Error('The native ZLang debug adapter is unavailable on this platform.');
    }
    return new vscode.DebugAdapterExecutable(nativeTools.adapter, []);
  }
}

function createWaveformChannel(webview, receive) {
  let ready = false;
  let latest;
  let sequence = 0;
  const pending = new Map();
  webview.onDidReceiveMessage((message) => {
    if (message?.type === 'ready') {
      ready = true;
      if (latest !== undefined) void webview.postMessage(latest);
      return;
    }
    if (message?.type === 'controlResult') {
      const request = pending.get(message.requestId);
      if (request !== undefined) {
        pending.delete(message.requestId);
        if (message.error) request.reject(new Error(message.error));
        else request.resolve(message.result);
      }
      return;
    }
    receive?.(message);
  });
  return {
    post(payload) {
      latest = payload;
      return ready ? webview.postMessage(payload) : Promise.resolve(false);
    },
    request(command) {
      const requestId = `control-${++sequence}`;
      return new Promise((resolve, reject) => {
        pending.set(requestId, { resolve, reject });
        void webview.postMessage({ type: 'control', requestId, command });
      });
    },
  };
}

function rowsFromChanges(signals, changes, idField = 'name') {
  const byKey = new Map(signals.map((item) => [item[idField], {
    name: item.name, width: item.width, values: [],
  }]));
  for (const change of changes) {
    const row = byKey.get(change.id ?? change.name);
    if (row) row.values.push({ event: change.timestamp ?? change.event, bits: change.bits });
  }
  return [...byKey.values()];
}

class WaveformDocument {
  constructor(uri, executable) {
    this.uri = uri;
    this.client = new WaveformQueryClient(executable);
  }
  dispose() { void this.client.close(); }
}
class WaveformEditorProvider {
  constructor(context) { this.context = context; }
  openCustomDocument(uri) { return new WaveformDocument(uri, nativeTools.indexer); }
  async resolveCustomEditor(document, panel) {
    panel.webview.options = { enableScripts: true };
    const layoutKey = `waveform.layout:${document.uri.toString()}`;
    const channel = createWaveformChannel(panel.webview, async (message) => {
      try {
        if (message?.type === 'range') {
          const result = await document.client.request('range', {
            signals: message.signals, start: message.start, end: message.end,
          });
          await channel.post({
            type: 'rangeResult', requestId: message.requestId,
            viewport: message.viewport, result,
          });
        } else if (message?.type === 'search') {
          const result = await document.client.request('search', { query: message.query });
          await channel.post({ type: 'searchResult', requestId: message.requestId, result });
        } else if (message?.type === 'persist') {
          await this.context.workspaceState.update(layoutKey, message.layout);
        } else if (message?.type === 'reload') {
          const catalog = await document.client.request('reload');
          await channel.post({
            type: 'catalog', catalog,
            layout: this.context.workspaceState.get(layoutKey),
          });
        } else if (message?.type === 'load' && typeof message.source === 'string') {
          const uri = vscode.Uri.file(message.source);
          await vscode.commands.executeCommand('vscode.openWith', uri, 'zlang-hdl.waveform');
        }
      } catch (error) {
        await channel.post({
          type: 'queryError', requestId: message?.requestId, error: error.message,
        });
      }
    });
    panel.webview.html = waveformHtml(panel.webview);
    activeWaveformChannel = channel;
    panel.onDidChangeViewState(() => {
      if (panel.active) activeWaveformChannel = channel;
    });
    panel.onDidDispose(() => {
      if (activeWaveformChannel === channel) activeWaveformChannel = undefined;
    });
    try {
      const catalog = await document.client.request('open', { path: document.uri.fsPath });
      await channel.post({
        type: 'catalog', catalog,
        layout: this.context.workspaceState.get(layoutKey),
      });
    } catch (error) {
      await channel.post({ type: 'queryError', error: `Cannot index waveform: ${error.message}` });
    }
  }
}

async function refreshLiveWaveform(session) {
  if (liveWaveformPanel === undefined || session?.type !== 'zlang-hdl') return;
  try {
    const trace = await session.customRequest('zlang/waveformRange', { start: 0 });
    if (trace.schema !== WAVEFORM_TRACE_SCHEMA) {
      throw new Error(`Unsupported waveform schema: ${trace.schema}`);
    }
    const names = new Map();
    for (const delta of trace.deltas) {
      for (const name of Object.keys(delta.changes)) names.set(name, { name });
    }
    const changes = trace.deltas.flatMap((delta) => Object.entries(delta.changes).map(
      ([name, value]) => ({ name, event: delta.event, bits: value.bits }),
    ));
    await liveWaveformChannel?.post({
      summary: `${names.size} signals, ${trace.deltas.length} delta events (event-index time)`,
      signals: rowsFromChanges([...names.values()], changes),
    });
  } catch (error) {
    await liveWaveformChannel?.post({
      summary: `Cannot read live trace: ${error.message}`, signals: [],
    });
  }
}

function openLiveWaveform() {
  if (liveWaveformPanel === undefined) {
    liveWaveformPanel = vscode.window.createWebviewPanel(
      'zlang-hdl.liveWaveform', 'ZLang HDL Live Waveform', vscode.ViewColumn.Beside,
      { enableScripts: true },
    );
    liveWaveformChannel = createWaveformChannel(liveWaveformPanel.webview);
    liveWaveformPanel.webview.html = waveformHtml(liveWaveformPanel.webview);
    liveWaveformPanel.onDidDispose(() => {
      liveWaveformPanel = undefined;
      liveWaveformChannel = undefined;
    });
  } else {
    liveWaveformPanel.reveal(vscode.ViewColumn.Beside);
  }
  void refreshLiveWaveform(vscode.debug.activeDebugSession);
}

function registerNativeSurface(context) {
  if (nativeTools === null) return;
  context.subscriptions.push(
    vscode.debug.registerDebugConfigurationProvider(
      'zlang-hdl', new ZLangDebugConfigurationProvider(context),
    ),
    vscode.debug.registerDebugAdapterDescriptorFactory(
      'zlang-hdl', new ZLangDebugAdapterFactory(),
    ),
    vscode.window.registerCustomEditorProvider(
      'zlang-hdl.waveform', new WaveformEditorProvider(context),
      { supportsMultipleEditorsPerDocument: true },
    ),
    vscode.commands.registerCommand('zlang-hdl.openWaveform', async () => {
      const selected = await vscode.window.showOpenDialog({
        canSelectMany: false, filters: { 'Waveforms': ['fst', 'vcd'] },
      });
      if (selected?.[0]) await vscode.commands.executeCommand(
        'vscode.openWith', selected[0], 'zlang-hdl.waveform',
      );
    }),
    vscode.commands.registerCommand('zlang-hdl.openLiveWaveform', openLiveWaveform),
    vscode.commands.registerCommand('zlang-hdl.addTraceSignals', async () => {
      const session = vscode.debug.activeDebugSession;
      if (session?.type !== 'zlang-hdl') {
        throw new Error('Start a ZLang HDL debug session before selecting trace signals.');
      }
      const catalog = await session.customRequest('zlang/signalCatalog');
      const selected = await vscode.window.showQuickPick(
        catalog.signals.filter((item) => item.kind !== 'memory').map((item) => ({
          label: item.name,
          description: `${item.kind}, ${item.width}-bit${item.traced ? ', already traced' : ''}`,
          picked: item.traced,
        })),
        { canPickMany: true, title: 'Add signals to the live trace' },
      );
      if (selected?.length) {
        await session.customRequest('zlang/addTraceSignals', {
          signals: selected.map((item) => item.label),
        });
        openLiveWaveform();
      }
    }),
    vscode.commands.registerCommand('zlang-hdl.startSimulation', async () => {
      await vscode.debug.startDebugging(undefined, {
        type: 'zlang-hdl', request: 'launch', name: 'Debug ZLang HDL',
        program: vscode.window.activeTextEditor?.document.uri.fsPath,
        top: 'Top', logicState: true, stopOnEntry: true,
      });
    }),
    vscode.debug.onDidReceiveDebugSessionCustomEvent((event) => {
      if (event.event === 'zlang/waveformUpdated') void refreshLiveWaveform(event.session);
    }),
  );
  const wcp = vscode.workspace.getConfiguration('zlang.waveform.wcp');
  if (wcp.get('enabled', false) && vscode.workspace.isTrusted) {
    waveformControlServer = new WaveformControlServer(async (command) => {
      if (activeWaveformChannel === undefined) throw new Error('No active waveform editor');
      if (command.command === 'shutdown') {
        setImmediate(() => { void waveformControlServer?.stop(); });
        return { command: 'ack' };
      }
      return activeWaveformChannel.request(command);
    }, (message) => output.appendLine(message));
    void waveformControlServer.start(wcp.get('port', 0)).catch(reportStartupError);
  }
}

async function activate(context) {
  // Language Client 10 consumes the structured LogOutputChannel surface.
  output = vscode.window.createOutputChannel('ZLang HDL', { log: true });
  context.subscriptions.push(output);
  try {
    nativeTools = resolveNativeTools(context);
  } catch (error) {
    nativeTools = null;
    nativeToolsError = error instanceof Error ? error.message : String(error);
    output.appendLine(`[warning] ${nativeToolsError}`);
  }
  // Static FST/VCD viewing and native command registration do not depend on the
  // language server.  Register them before starting LSP so a slow or missing
  // server cannot leave an already-open custom editor without its provider.
  registerNativeSurface(context);
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
  await waveformControlServer?.stop();
  if (client === undefined) return undefined;
  const running = client;
  client = undefined;
  if (!running.isRunning()) return undefined;
  return running.stop();
}

module.exports = { activate, deactivate };
