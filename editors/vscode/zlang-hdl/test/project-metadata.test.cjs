// SPDX-License-Identifier: Apache-2.0
'use strict';
const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {EventEmitter} = require('node:events');
const {metadataPaths, watchProjectMetadata} = require('../project_metadata');

const doc = file => ({languageId:'zlang-hdl', uri:{scheme:'file', fsPath:file}});
function workspace(documents) {
  const emitter = new EventEmitter();
  const subscribe = name => listener => {
    emitter.on(name, listener);
    return {dispose:() => emitter.off(name, listener)};
  };
  return {textDocuments:documents, emitter,
    onDidOpenTextDocument:subscribe('open'), onDidCloseTextDocument:subscribe('close'),
    onDidChangeWorkspaceFolders:subscribe('folders')};
}
function fixture(t) {
  const w = workspace([doc('/project/src/a.zhl')]);
  const files = new Map(), listeners = new Map(), logs = [], sent = [];
  const filesystem = {
    statSync(file) {
      if (!files.has(file)) throw Object.assign(new Error('missing'), {code:'ENOENT'});
      return files.get(file);
    },
    watchFile(file, options, listener) {
      assert.deepEqual(options, {persistent:false, bigint:true, interval:250});
      listeners.set(file, listener);
    },
    unwatchFile(file, listener) {
      assert.equal(listeners.get(file), listener);
      listeners.delete(file);
    },
  };
  const put = (file, n) => files.set(file, {nlink:1n, dev:1n, ino:1n, mode:1n,
    size:1n, mtimeNs:BigInt(n), ctimeNs:BigInt(n)});
  const create = (notify = async p => sent.push(p)) => {
    const observer = watchProjectMetadata(w, file => `file://${file}`, notify,
      message => logs.push(message), filesystem);
    t.after(() => observer.dispose());
    return observer;
  };
  return {w, files, listeners, logs, sent, put, create};
}
const tick = () => new Promise(resolve => setImmediate(resolve));

test('metadata inventory deduplicates ancestors, includes absent/nested owners, ignores non-file and other languages', () => {
  const w = workspace([doc('/project/src/a.zhl'), doc('/project/src/b.zhl'),
    doc('/project/nested/top.zhl'), {...doc('/ignore.txt'), languageId:'plaintext'},
    {...doc('/untitled.zhl'), uri:{scheme:'untitled'}}]);
  const actual = metadataPaths(w);
  assert.deepEqual(actual, [...new Set(actual)].sort());
  assert.equal(actual.length, 8);
  for (const directory of ['/', '/project', '/project/src', '/project/nested'])
    for (const name of ['zlang.lock', 'zlang.toml']) assert.ok(actual.includes(path.join(directory, name)));
  assert.deepEqual(metadataPaths(workspace([])), []);
});

test('deletion before asynchronous watcher readiness is reconciled after LanguageClient start', async t => {
  const f = fixture(t), lock = '/project/zlang.lock';
  f.put(lock, 1);
  const observer = f.create();
  f.files.delete(lock); // No OS event; initial async watcher stat can miss it.
  assert.equal(f.sent.length, 0);
  await observer.start();
  assert.deepEqual(f.sent, [{changes:[{uri:'file:///project/zlang.lock', type:3}]}]);
  f.listeners.get(lock)(); // First async stat must not duplicate the transition.
  await tick();
  assert.equal(f.sent.length, 1);
});

test('changes received before client readiness are queued, not discarded', async t => {
  const f = fixture(t), lock = '/project/zlang.lock';
  const observer = f.create();
  f.put(lock, 1); f.listeners.get(lock)();
  assert.equal(f.sent.length, 0);
  await observer.start();
  assert.deepEqual(f.sent, [{changes:[{uri:'file:///project/zlang.lock', type:1}]}]);
});

test('create/delete, nanosecond changes and atomic inode replacement have exact lifecycle types', async t => {
  const f = fixture(t), file = '/project/zlang.toml';
  const observer = f.create(); await observer.start();
  f.put(file, 1); f.listeners.get(file)(); await tick();
  f.put(file, 2); f.listeners.get(file)(); await tick();
  f.files.set(file, {...f.files.get(file), ino:7n}); f.listeners.get(file)(); await tick();
  f.files.delete(file); f.listeners.get(file)(); await tick();
  assert.deepEqual(f.sent.map(p => p.changes[0].type), [1, 2, 2, 3]);
});

test('open/close rebinding preserves shared watches and disposal stops callbacks', async t => {
  const f = fixture(t), observer = f.create(); await observer.start();
  const listener = f.listeners.get('/project/zlang.lock');
  f.w.textDocuments.push(doc('/project/nested/b.zhl')); f.w.emitter.emit('open');
  assert.equal(f.listeners.get('/project/zlang.lock'), listener);
  assert.ok(f.listeners.has('/project/nested/zlang.lock'));
  f.w.textDocuments = [f.w.textDocuments[0]]; f.w.emitter.emit('close');
  assert.equal(f.listeners.has('/project/nested/zlang.lock'), false);
  observer.dispose(); observer.dispose();
  assert.equal(f.listeners.size, 0);
  f.put('/project/zlang.lock', 1); listener(); await tick();
  assert.equal(f.sent.length, 0);
  assert.equal(f.w.emitter.listenerCount('open'), 0);
});

test('delivery is serial, retains later events, and logs/retries failed notification', async t => {
  const f = fixture(t), lock = '/project/zlang.lock', manifest = '/project/zlang.toml';
  let release, reject = true;
  const observer = f.create(async params => {
    if (reject) throw Error('transport failed');
    f.sent.push(params);
    if (f.sent.length === 1) await new Promise(resolve => {release = resolve;});
  });
  f.put(lock, 1); f.listeners.get(lock)(); await observer.start();
  assert.ok(f.logs.some(line => line.includes('notification failed')));
  reject = false;
  const started = observer.start(); await tick();
  f.put(manifest, 1); f.listeners.get(manifest)();
  assert.equal(f.sent.length, 1);
  release(); await started;
  assert.deepEqual(f.sent.flatMap(p => p.changes).map(e => e.uri),
    ['file:///project/zlang.lock', 'file:///project/zlang.toml']);
});

test('real stat watcher rechecks deletion, recreation and atomic replacement without a recursive feed', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'metadata-watch.'));
  t.after(() => fs.rmSync(root, {recursive:true, force:true}));
  const lock = path.join(root, 'zlang.lock'), manifest = path.join(root, 'zlang.toml');
  fs.writeFileSync(lock, 'first');
  const sent = [], observer = watchProjectMetadata(workspace([doc(path.join(root, 'top.zhl'))]),
    file => file, async params => sent.push(...params.changes), () => {});
  t.after(() => observer.dispose());
  const event = async (file, type) => {
    const deadline = Date.now()+5000;
    while (!sent.some(e => e.uri === file && e.type === type) && Date.now() < deadline)
      await new Promise(resolve => setTimeout(resolve, 25));
    assert.ok(sent.some(e => e.uri === file && e.type === type), `missing ${type} ${file}`);
    sent.length = 0;
  };
  await observer.start();
  fs.unlinkSync(lock); await event(lock, 3);
  fs.writeFileSync(lock, 'second'); await event(lock, 1);
  const replacement = path.join(root, 'replacement');
  fs.writeFileSync(replacement, 'third'); fs.renameSync(replacement, lock); await event(lock, 2);
  fs.writeFileSync(manifest, 'manifest'); await event(manifest, 1);
  fs.unlinkSync(manifest); await event(manifest, 3);
});
