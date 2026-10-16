// SPDX-License-Identifier: Apache-2.0
'use strict';

const assert = require('node:assert/strict');
const net = require('node:net');
const { EventEmitter } = require('node:events');
const { WaveformControlServer } = require('../waveform_wcp');

function waitForFrames(socket, count) {
  return new Promise((resolve, reject) => {
    let buffered = '';
    const frames = [];
    socket.setEncoding('utf8');
    socket.on('error', reject);
    socket.on('data', (chunk) => {
      buffered += chunk;
      const parts = buffered.split('\0');
      buffered = parts.pop();
      for (const raw of parts) {
        if (raw) frames.push(JSON.parse(raw));
      }
      if (frames.length >= count) resolve(frames);
    });
  });
}

async function main() {
  const failures = [];
  const isolated = new WaveformControlServer(async () => ({}), message => failures.push(message));
  const reset = new EventEmitter();
  reset.setEncoding = () => {};
  reset.destroyed = false;
  reset.destroy = () => { reset.destroyed = true; reset.emit('close'); };
  isolated.connect(reset);
  assert.doesNotThrow(() => reset.emit('error', Object.assign(new Error('connection reset'), {
    code: 'ECONNRESET',
  })));
  assert.equal(reset.destroyed, true);
  assert.equal(isolated.sockets.size, 0);
  assert.match(failures[0], /ECONNRESET/);
  assert.doesNotThrow(() => isolated.send(reset, { command: 'ack' }));

  const server = new WaveformControlServer(async (message) => {
    if (message.command === 'reload') {
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    return { command: message.command };
  }, () => {});
  const address = await server.start(0);
  assert.match(address, /^127\.0\.0\.1:\d+$/);
  const socket = net.createConnection(Number(address.split(':')[1]), '127.0.0.1');
  await new Promise((resolve, reject) => {
    socket.once('connect', resolve);
    socket.once('error', reject);
  });
  const received = waitForFrames(socket, 4);
  socket.write(`${JSON.stringify({ type: 'greeting', version: '0' })}\0`);
  socket.write(`${JSON.stringify({ type: 'greeting', version: '0', commands: [] })}\0`);
  socket.write(`${JSON.stringify({ type: 'command', command: 'reload' })}\0`);
  socket.write(`${JSON.stringify({ type: 'command', command: 'zoom_to_fit' })}\0`);
  const frames = await received;
  assert.equal(frames[0].type, 'error');
  assert.equal(frames[0].error, 'greeting_required');
  assert.equal(frames[1].type, 'greeting');
  assert.equal(frames[1].version, '0');
  assert.deepEqual(frames.slice(2).map((item) => item.command), ['reload', 'zoom_to_fit']);
  socket.destroy();
  const v1 = net.createConnection(Number(address.split(':')[1]), '127.0.0.1');
  await new Promise((resolve, reject) => {
    v1.once('connect', resolve);
    v1.once('error', reject);
  });
  const v1Received = waitForFrames(v1, 4);
  v1.write('null\0');
  v1.write(`${JSON.stringify({ type: 'greeting', version: '99', commands: [] })}\0`);
  v1.write(`${JSON.stringify({ type: 'greeting', version: '1', commands: [] })}\0`);
  v1.write(`${JSON.stringify({ type: 'command', command: 'get_measurements' })}\0`);
  const v1Frames = await v1Received;
  assert.equal(v1Frames[0].error,'invalid_message');
  assert.equal(v1Frames[1].error,'unsupported_version');
  assert.equal(v1Frames[2].version, '1');
  assert.ok(v1Frames[2].commands.includes('export_session'));
  assert.equal(v1Frames[3].command, 'get_measurements');
  const disconnected = new Promise(resolve => v1.once('close', resolve));
  const accepted = [...server.sockets].find(item => item.remotePort === v1.localPort);
  assert.ok(accepted);
  accepted.emit('error', Object.assign(new Error('peer reset'), { code: 'ECONNRESET' }));
  await disconnected;
  assert.equal(server.sockets.has(accepted), false);
  const next = net.createConnection(Number(address.split(':')[1]), '127.0.0.1');
  const nextFrames = waitForFrames(next, 1);
  next.write(`${JSON.stringify({ type: 'greeting', version: '1', commands: [] })}\0`);
  assert.equal((await nextFrames)[0].type, 'greeting');
  next.destroy();
  await server.stop();
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
