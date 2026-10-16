// SPDX-License-Identifier: Apache-2.0
// Thin Community client for the repository's standard zlang-lsp process.
'use strict';
const {addVariableToWaveform} = require('./debug_waveform');

const fs = require('node:fs');
const path = require('node:path');
const childProcess = require('node:child_process');
const vscode = require('vscode');

// Development resolves the pinned package normally.  Production packaging
// bundles it into the single generated extension entrypoint.
const { LanguageClient, TransportKind } = require('vscode-languageclient/node');
const { waveformHtml } = require('./waveform_view');
const { WaveformQueryClient } = require('./waveform_client');
const { WaveformDocument, watchWaveformFile } = require('./waveform_document');
const { parseWaveformClipboard, encodeWaveformClipboard } = require('./waveform_clipboard');
const { createWaveformChannel } = require('./waveform_channel');
const { LiveWaveformFeed } = require('./waveform_live');
const {saveSession,readSession,exportLiveTrace}=require('./waveform_session_io');
const { WaveformControlServer } = require('./waveform_wcp');

const LANGUAGE_ID = 'zlang-hdl';
const CONFIGURATION_SECTION = 'zlang.lsp';
const SERVER_PATH_SETTING = 'path';
const DEBUG_ADAPTER_SCHEMA = 'zlang-debug-adapter-v1';
const WAVEFORM_QUERY_SCHEMA = 'zlang-waveform-query-v3';

let client;
let output;
let nativeTools;
let nativeToolsError;
let liveWaveformPanel;
let liveWaveformChannel;
let liveWaveformFeed;
let activeWaveformChannel;
let activeWaveformUri;
const pendingWaveformSessions = new Map();
const openWaveformChannels = new Map();
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

async function exchangeWaveformClipboard(message,channel){
  try{
    let result;
    if(message.action==='read'){
      const text=await vscode.env.clipboard.readText();result={text:encodeWaveformClipboard(parseWaveformClipboard(text))};
    }else if(message.action==='write'){
      const text=encodeWaveformClipboard(parseWaveformClipboard(message.text));
      await vscode.env.clipboard.writeText(text);result={written:true};
    }else throw new Error('Unknown waveform clipboard action');
    await channel.post({type:'queryResult',requestId:message.requestId,result});
  }catch(error){await channel.post({type:'queryError',requestId:message.requestId,error:error.message});}
}

class WaveformEditorProvider {
  constructor(context) { this.context = context; }
  openCustomDocument(uri) {
    return new WaveformDocument(uri,new WaveformQueryClient(nativeTools.indexer),
      changed=>watchWaveformFile(vscode,uri,changed));
  }
  async resolveCustomEditor(document, panel) {
    panel.webview.options = { enableScripts: true };
    const layoutKey = `waveform.layout:${document.uri.toString()}`;
    const panelKey='panel-'+(++document.panelSequence);
    const channel = createWaveformChannel(panel.webview, async (message) => {
      if(message?.type==='clipboard'){await exchangeWaveformClipboard(message,channel);return;}
      try {
        if(message?.type==='saveSession'){await saveSession(vscode,channel,document.uri);}
        else if(message?.type==='loadSession'){await loadWaveformSession(this.context);}
        else if (message?.type === 'search') {
          const result = await document.queries.request(panelKey+'search','search', { query: message.query });
          await channel.post({ type: 'searchResult', requestId: message.requestId, result });
        } else if (message?.type === 'statistics') {
          const result = await document.cachedRequest('statistics', {
            signals: message.signals, start: message.start, end: message.end,
          },panelKey+'statistics');
          await channel.post({ type: 'statisticsResult', requestId: message.requestId, result });
        } else if (message?.type === 'valueAt') {
          const result = await document.cachedRequest('value_at', {
            signals: message.signals, timestamp: message.timestamp,
          },panelKey+(message.purpose||'valueAt'));
          await channel.post({ type: 'valueResult', requestId: message.requestId, result });
        } else if (message?.type === 'renderRange') {
          const result = await document.cachedRequest('render_range', {
            signals: message.signals, start: message.start, end: message.end,
            pixels: message.pixels,
          },panelKey+(message.overview?'overview':'viewport-'+message.viewport));
          await channel.post({
            type: 'renderResult', requestId: message.requestId,
            viewport: message.viewport, overview: Boolean(message.overview), result,
          });
        } else if (message?.type === 'persist') {
          await this.context.workspaceState.update(layoutKey, message.layout);
          await channel.post({type:'layoutSaved',revision:message.revision});
        } else if (message?.type === 'reload') {
          await document.reload();
        } else if (message?.type === 'load' && typeof message.source === 'string') {
          const uri = vscode.Uri.file(message.source);
          await vscode.commands.executeCommand('vscode.openWith', uri, 'zlang-hdl.waveform');
        }
      } catch (error) {
        if(message?.type==='reload')return; // The document notifies every editor.
        await channel.post({
          type: 'queryError', requestId: message?.requestId, error: error.message,
        });
      }
    });
    const traceSubscription=document.subscribe(message=>{void channel.post(message);});
    panel.webview.html = waveformHtml(fs.readFileSync(
      path.join(this.context.extensionPath, 'dist', 'waveform.js'), 'utf8',
    ));
    activeWaveformChannel = channel;
    activeWaveformUri = document.uri;
    openWaveformChannels.set(document.uri.toString(),channel);
    panel.onDidChangeViewState(() => {
      if (panel.active && panel.viewColumn === vscode.window.tabGroups.activeTabGroup.viewColumn) {
        activeWaveformChannel = channel;
        activeWaveformUri = document.uri;
      }
    });
    panel.onDidDispose(() => {
      traceSubscription.dispose();
      channel.dispose();
      if(openWaveformChannels.get(document.uri.toString())===channel)
        openWaveformChannels.delete(document.uri.toString());
      if (activeWaveformChannel === channel) {
        activeWaveformChannel = undefined;
        activeWaveformUri = undefined;
      }
    });
    try {
      const catalog = await document.open();
      await channel.post({
        type: 'catalog', catalog, fileChanged:document.changed,
        layout: this.context.workspaceState.get(layoutKey),
      });
      const pending = pendingWaveformSessions.get(document.uri.toString());
      if (pending !== undefined) {
        pendingWaveformSessions.delete(document.uri.toString());
        await channel.request({ command: 'import_session', session: pending });
      }
    } catch (error) {
      await channel.post({ type: 'queryError', error: `Cannot index waveform: ${error.message}` });
    }
  }
}

async function saveWaveformSession() {
  await saveSession(vscode,activeWaveformChannel,activeWaveformUri);
}

async function loadWaveformSession(context) {
  const loaded=await readSession(vscode);
  if(!loaded)return;
  const {session,uri,kind}=loaded;
  if(kind==='live'){
    if(vscode.debug.activeDebugSession?.type!=='zlang-hdl')
      throw new Error('Start a ZLang HDL debug session before loading a live waveform template.');
    await openLiveWaveform(context);
    await liveWaveformChannel.request({command:'import_session',session});
    return;
  }
  const tracePath = path.isAbsolute(session.trace)
    ? session.trace : path.resolve(path.dirname(uri.fsPath), session.trace);
  const traceUri = vscode.Uri.file(tracePath);
  const existing=openWaveformChannels.get(traceUri.toString());
  if (existing!==undefined) {
    await vscode.commands.executeCommand('vscode.openWith',traceUri,'zlang-hdl.waveform');
    await existing.request({ command: 'import_session', session });
    return;
  }
  pendingWaveformSessions.set(traceUri.toString(), session);
  await vscode.commands.executeCommand('vscode.openWith', traceUri, 'zlang-hdl.waveform');
}

async function refreshLiveWaveform(session) {
  if(vscode.debug.activeDebugSession?.id!==session?.id)return;
  await liveWaveformFeed?.update(session);
}

async function openLiveWaveform(context) {
  if(vscode.debug.activeDebugSession?.type!=='zlang-hdl')
    throw new Error('Start a ZLang HDL debug session before opening live waveforms.');
  if (liveWaveformPanel === undefined) {
    const panel = vscode.window.createWebviewPanel(
      'zlang-hdl.liveWaveform', 'ZLang HDL Live Waveform', vscode.ViewColumn.Beside,
      { enableScripts: true, retainContextWhenHidden: true },
    );
    let feed;
    const channel = createWaveformChannel(panel.webview, async message=>{
      if(message?.type==='clipboard'){await exchangeWaveformClipboard(message,channel);return;}
      const commands={renderRange:'render_range',statistics:'statistics',valueAt:'value_at',search:'search'};
      try {
        if(message?.type==='persist'){
          await feed.persist(message.layout,message.sessionId);
          await channel.post({type:'layoutSaved',revision:message.revision,sessionId:message.sessionId});return;
        }
        if(message?.type==='saveSession'){await saveSession(vscode,channel);return;}
        if(message?.type==='loadSession'){await loadWaveformSession(context);return;}
        if(message?.type==='addTraceSignals'){await vscode.commands.executeCommand('zlang-hdl.addTraceSignals');return;}
        if(message?.type==='exportTrace'){await exportLiveTrace(vscode,vscode.debug.activeDebugSession);return;}
        if(message?.type==='load'){
          if(typeof message.source!=='string'||!['.fst','.vcd'].includes(path.extname(message.source).toLowerCase()))
            throw new Error('Open a VCD or FST waveform file.');
          await vscode.commands.executeCommand('vscode.openWith',vscode.Uri.file(message.source),'zlang-hdl.waveform');return;
        }
        const session=vscode.debug.activeDebugSession;
        if(session?.type!=='zlang-hdl'||session.id!==feed.sessionId)
          throw new Error('The live debugger session is no longer active');
        if(message?.type==='reload'){await feed.update(session);return;}
        if(!commands[message?.type])return;
        const {type,requestId,purpose,...fields}=message;
        delete fields.viewport;delete fields.overview;
        const result=await feed.request(commands[type],fields,purpose||type);
        await channel.post({type:'queryResult',requestId,result});
      }catch(error){await channel.post({type:'queryError',requestId:message?.requestId,error:error.message});}
    });
    feed = new LiveWaveformFeed(payload => channel.post(payload),context.workspaceState);
    liveWaveformPanel=panel;liveWaveformChannel=channel;liveWaveformFeed=feed;
    panel.webview.html = waveformHtml(fs.readFileSync(
      path.join(context.extensionPath, 'dist', 'waveform.js'), 'utf8',
    ));
    panel.onDidDispose(() => {
      feed.dispose();channel.dispose();
      if(activeWaveformChannel===channel){activeWaveformChannel=undefined;activeWaveformUri=undefined;}
      if(liveWaveformPanel===panel){liveWaveformFeed=undefined;liveWaveformPanel=undefined;liveWaveformChannel=undefined;}
    });
    panel.onDidChangeViewState(()=>{
      if(panel.active&&panel.viewColumn===vscode.window.tabGroups.activeTabGroup.viewColumn){
        activeWaveformChannel=channel;activeWaveformUri=undefined;
      }
    });
  } else {
    liveWaveformPanel.reveal();
  }
  activeWaveformChannel=liveWaveformChannel;activeWaveformUri=undefined;
  await refreshLiveWaveform(vscode.debug.activeDebugSession);
}

function registerNativeSurface(context) {
  if (nativeTools === null) return;
  context.subscriptions.push(
    vscode.debug.registerDebugAdapterTrackerFactory('zlang-hdl',{
      createDebugAdapterTracker(session){
        const end=()=>liveWaveformFeed?.end(session.id);
        return {onWillReceiveMessage(message){
          if(message.type==='request'&&['disconnect','terminate'].includes(message.command))end();
        },onExit:end};
      },
    }),
    vscode.debug.registerDebugConfigurationProvider(
      'zlang-hdl', new ZLangDebugConfigurationProvider(context),
    ),
    vscode.debug.registerDebugAdapterDescriptorFactory(
      'zlang-hdl', new ZLangDebugAdapterFactory(),
    ),
    vscode.window.registerCustomEditorProvider(
      'zlang-hdl.waveform', new WaveformEditorProvider(context),
      // Keep the bounded workspace/query state when another editor is active.
      { supportsMultipleEditorsPerDocument: true,
        webviewOptions: { retainContextWhenHidden: true } },
    ),
    vscode.commands.registerCommand('zlang-hdl.openWaveform', async () => {
      const selected = await vscode.window.showOpenDialog({
        canSelectMany: false, filters: { 'Waveforms': ['fst', 'vcd'] },
      });
      if (selected?.[0]) await vscode.commands.executeCommand(
        'vscode.openWith', selected[0], 'zlang-hdl.waveform',
      );
    }),
    vscode.commands.registerCommand('zlang-hdl.openLiveWaveform', () => openLiveWaveform(context)),
    vscode.commands.registerCommand('zlang-hdl.addToWaveform', (item) =>
      addVariableToWaveform(vscode, context, openLiveWaveform, item)),
    vscode.commands.registerCommand('zlang-hdl.saveWaveformSession', saveWaveformSession),
    vscode.commands.registerCommand('zlang-hdl.loadWaveformSession', ()=>loadWaveformSession(context)),
    vscode.commands.registerCommand('zlang-hdl.addTraceSignals', async () => {
      const session = vscode.debug.activeDebugSession;
      if (session?.type !== 'zlang-hdl') {
        throw new Error('Start a ZLang HDL debug session before selecting trace signals.');
      }
      const catalog = await session.customRequest('zlang/signalCatalog');
      const selected = await vscode.window.showQuickPick(
        catalog.signals.filter((item) => item.kind !== 'memory').map((item) => ({
          label: item.name,
          description: `${item.type_hint || item.kind}${item.traced ? ', already traced' : ''}`,
          picked: item.traced,
        })),
        { canPickMany: true, title: 'Add signals to the live trace' },
      );
      if (selected?.length) {
        await session.customRequest('zlang/addTraceSignals', {
          signals: selected.map((item) => item.label),
        });
        openLiveWaveform(context);
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
  for(const [action,fallback] of [['cut','editor.action.clipboardCutAction'],
    ['copy','editor.action.clipboardCopyAction'],['paste','editor.action.clipboardPasteAction']]){
    context.subscriptions.push(vscode.commands.registerCommand('zlang-hdl.'+action+'WaveformRows',async()=>{
      if(!activeWaveformChannel)throw new Error('Open and focus a ZLang HDL waveform first.');
      const result=await activeWaveformChannel.request({command:'edit_rows',action});
      if(!result.handled)await vscode.commands.executeCommand(fallback);
    }));
  }
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
