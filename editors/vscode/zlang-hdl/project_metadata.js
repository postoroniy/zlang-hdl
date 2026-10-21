// SPDX-License-Identifier: Apache-2.0
'use strict';
const fs = require('node:fs');
const path = require('node:path');

function metadataPaths(workspace) {
  const paths = new Set();
  for (const document of workspace.textDocuments) {
    if (document.languageId !== 'zlang-hdl' || document.uri.scheme !== 'file') continue;
    // Watch potential project owners, including not-yet-created nested
    // manifests. No directory walk, TOML parsing, or source resolution in JS.
    let directory = path.dirname(document.uri.fsPath);
    while (true) {
      for (const name of ['zlang.toml', 'zlang.lock']) paths.add(path.join(directory, name));
      const parent = path.dirname(directory);
      if (parent === directory) break;
      directory = parent;
    }
  }
  return [...paths].sort();
}

function statIdentity(stat) {
  if (stat.nlink === 0n) return null; // watchFile's missing-file sentinel
  return [stat.dev, stat.ino, stat.mode, stat.size, stat.mtimeNs, stat.ctimeNs].join(':');
}

// Owns only the metadata inventory and delivery lifecycle for open sources.
// Polling individual stat records avoids recursive-watcher readiness gaps,
// watcherExclude settings, and replacement of a lock inode by atomic rename.
function watchProjectMetadata(workspace, toUri, notify, log, filesystem = fs) {
  const records = new Map();
  const pending = new Map();
  let ready = false, disposed = false, sending = false;
  const read = file => {
    try { return statIdentity(filesystem.statSync(file, {bigint:true})); }
    catch (error) {
      if (['ENOENT', 'ENOTDIR'].includes(error.code)) return null;
      log(`stat failed ${file}: ${error.code}`);
      return `error:${error.code}`;
    }
  };
  const flush = async () => {
    if (!ready || disposed || sending || pending.size === 0) return;
    sending = true;
    const changes = [...pending.values()];
    pending.clear();
    try {
      await notify({changes});
      log(`sent ${JSON.stringify(changes)}`);
    } catch (error) {
      // Do not silently discard failed delivery or let an async callback
      // become an unhandled rejection. A later start/event retries it.
      if (!disposed) for (const change of changes)
        if (!pending.has(change.uri)) pending.set(change.uri, change);
      log(`notification failed: ${error}`);
      return;
    } finally { sending = false; }
    await flush();
  };
  const sample = (file, record, identity) => {
    if (disposed || records.get(file) !== record || identity === record.identity) return;
    const type = identity === null ? 3 : record.identity === null ? 1 : 2;
    record.identity = identity;
    const uri = toUri(file);
    pending.set(uri, {uri, type});
    log(`observed ${type} ${uri}`);
    void flush();
  };
  const sync = () => {
    if (disposed) return;
    const wanted = new Set(metadataPaths(workspace));
    for (const [file, record] of records) if (!wanted.has(file)) {
      filesystem.unwatchFile(file, record.listener);
      records.delete(file);
      pending.delete(toUri(file));
    }
    for (const file of wanted) if (!records.has(file)) {
      const record = {identity:read(file)};
      record.listener = () => sample(file, record, read(file));
      records.set(file, record);
      // Capture the baseline synchronously; watchFile's first async stat may
      // occur after deletion. Never rely on its "previous" stat as baseline.
      filesystem.watchFile(file, {persistent:false, bigint:true, interval:250}, record.listener);
      sample(file, record, read(file));
    }
    log(`inventory ${records.size} metadata paths`);
  };
  const subscriptions = [
    workspace.onDidOpenTextDocument(sync), workspace.onDidCloseTextDocument(sync),
    workspace.onDidChangeWorkspaceFolders(sync),
  ];
  sync();
  return {
    async start() {
      // Reconcile the gap between subscription and LanguageClient readiness.
      for (const [file, record] of records) sample(file, record, read(file));
      ready = true;
      await flush();
    },
    dispose() {
      disposed = true;
      subscriptions.forEach(item => item.dispose());
      for (const [file, record] of records) filesystem.unwatchFile(file, record.listener);
      records.clear(); pending.clear();
    },
  };
}

module.exports = {metadataPaths, watchProjectMetadata};
