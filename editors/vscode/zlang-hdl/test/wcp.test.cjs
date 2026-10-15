// SPDX-License-Identifier: Apache-2.0
'use strict';

const assert = require('node:assert/strict');
const net = require('node:net');
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
  await server.stop();
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
