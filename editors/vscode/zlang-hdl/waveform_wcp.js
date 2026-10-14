// SPDX-License-Identifier: Apache-2.0
'use strict';

const net = require('node:net');

const VERSION = '0';
const MAX_FRAME_BYTES = 1024 * 1024;
const COMMANDS = [
  'get_item_list', 'get_item_info', 'set_item_color', 'add_items',
  'reload', 'set_viewport_to',
  'set_viewport_range', 'remove_items', 'focus_item', 'clear', 'load',
  'zoom_to_fit', 'set_cursor', 'shutdown',
];

class WaveformControlServer {
  constructor(dispatch, log) {
    this.dispatch = dispatch;
    this.log = log;
    this.server = null;
    this.sockets = new Set();
  }

  async start(port = 0) {
    if (this.server !== null) return this.address();
    this.server = net.createServer((socket) => this.connect(socket));
    await new Promise((resolve, reject) => {
      this.server.once('error', reject);
      this.server.listen({ host: '127.0.0.1', port }, resolve);
    });
    this.log(`WCP listening on ${this.address()}`);
    return this.address();
  }

  address() {
    const address = this.server?.address();
    return typeof address === 'object' && address !== null
      ? `127.0.0.1:${address.port}` : null;
  }

  connect(socket) {
    this.sockets.add(socket);
    let buffered = '';
    let greeted = false;
    let responses = Promise.resolve();
    socket.setEncoding('utf8');
    socket.on('data', (chunk) => {
      buffered += chunk;
      const messages = buffered.split('\0');
      buffered = messages.pop();
      if (Buffer.byteLength(buffered, 'utf8') > MAX_FRAME_BYTES) {
        this.send(socket, { type: 'error', error: 'frame_too_large', arguments: [], message: 'WCP frame exceeds 1 MiB' });
        socket.destroy();
        return;
      }
      for (const raw of messages) {
        if (!raw.trim()) continue;
        if (Buffer.byteLength(raw, 'utf8') > MAX_FRAME_BYTES) {
          this.send(socket, { type: 'error', error: 'frame_too_large', arguments: [], message: 'WCP frame exceeds 1 MiB' });
          socket.destroy();
          return;
        }
        let message;
        try { message = JSON.parse(raw); } catch (error) {
          this.send(socket, { type: 'error', error: 'invalid_json', arguments: [], message: error.message });
          continue;
        }
        if (!greeted) {
          if (message.type !== 'greeting' || String(message.version) !== VERSION
              || !Array.isArray(message.commands)
              || !message.commands.every((command) => typeof command === 'string')) {
            this.send(socket, { type: 'error', error: 'greeting_required', arguments: [], message: 'WCP v0 greeting required' });
            continue;
          }
          greeted = true;
          this.send(socket, { type: 'greeting', version: VERSION, commands: COMMANDS });
          continue;
        }
        if (message.type !== 'command' || !COMMANDS.includes(message.command)) {
          this.send(socket, { type: 'error', error: 'unsupported_command', arguments: [], message: 'Unsupported WCP command' });
          continue;
        }
        responses = responses.then(() => Promise.resolve(this.dispatch(message)).then(
          (response) => this.send(socket, { type: 'response', ...(response ?? { command: 'ack' }) }),
          (error) => this.send(socket, { type: 'error', error: message.command, arguments: [], message: error.message }),
        ));
      }
    });
    socket.on('close', () => this.sockets.delete(socket));
  }

  send(socket, message) { socket.write(`${JSON.stringify(message)}\0`); }

  async stop() {
    for (const socket of this.sockets) socket.destroy();
    this.sockets.clear();
    if (this.server === null) return;
    const server = this.server;
    this.server = null;
    await new Promise((resolve) => server.close(resolve));
  }
}

module.exports = { WaveformControlServer, WCP_VERSION: VERSION };
