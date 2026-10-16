// SPDX-License-Identifier: Apache-2.0
'use strict';

const net = require('node:net');

const VERSION = '0';
const OPTIONAL_VERSION = '1';
const MAX_FRAME_BYTES = 1024 * 1024;
const COMMANDS = [
  'get_item_list', 'get_item_info', 'set_item_color', 'add_items',
  'reload', 'set_viewport_to',
  'set_viewport_range', 'remove_items', 'focus_item', 'clear', 'load',
  'zoom_to_fit', 'set_cursor', 'shutdown',
];
const COMMANDS_V1 = [
  ...COMMANDS, 'set_selection', 'set_cursors', 'set_bookmarks',
  'export_session', 'import_session', 'get_measurements', 'set_viewport_sync',
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
    socket.on('error', error => {
      this.sockets.delete(socket);
      this.log(`WCP connection error: ${error.code || 'socket_error'}`);
      socket.destroy();
    });
    if(this.sockets.size>=8){socket.destroy();return;}
    this.sockets.add(socket);
    let buffered = '';
    let greeted = false;
    let negotiatedVersion = null;
    let responses = Promise.resolve();
    let queued=0;
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
        if(message===null||typeof message!=='object'||Array.isArray(message)){
          this.send(socket,{type:'error',error:'invalid_message',arguments:[],message:'WCP message must be an object'});continue;
        }
        if (!greeted) {
          const requestedVersion = String(message.version);
          if(message.type==='greeting'&&Array.isArray(message.commands)
            &&![VERSION,OPTIONAL_VERSION].includes(requestedVersion)){
            this.send(socket,{type:'error',error:'unsupported_version',arguments:[],message:'Unsupported WCP version'});continue;
          }
          if (message.type !== 'greeting' || ![VERSION, OPTIONAL_VERSION].includes(requestedVersion)
              || !Array.isArray(message.commands)
              || !message.commands.every((command) => typeof command === 'string')) {
            this.send(socket, { type: 'error', error: 'greeting_required', arguments: [], message: 'WCP v0 or v1 greeting required' });
            continue;
          }
          greeted = true;
          negotiatedVersion = requestedVersion;
          this.send(socket, {
            type: 'greeting', version: negotiatedVersion,
            commands: negotiatedVersion === OPTIONAL_VERSION ? COMMANDS_V1 : COMMANDS,
          });
          continue;
        }
        const supported = negotiatedVersion === OPTIONAL_VERSION ? COMMANDS_V1 : COMMANDS;
        if (message.type !== 'command' || !supported.includes(message.command)) {
          this.send(socket, { type: 'error', error: 'unsupported_command', arguments: [], message: 'Unsupported WCP command' });
          continue;
        }
        if(queued>=64){this.send(socket,{type:'error',error:'busy',arguments:[],message:'WCP command queue is full'});continue;}
        queued++;
        responses = responses.then(() => Promise.resolve(this.dispatch(message)).then(
          (response) => this.send(socket, { type: 'response', ...(response ?? { command: 'ack' }) }),
          (error) => this.send(socket, { type: 'error', error: message.command, arguments: [], message: error.message }),
        )).finally(()=>{queued--;});
      }
    });
    socket.on('close', () => this.sockets.delete(socket));
  }

  send(socket, message) {
    if (!socket.destroyed) socket.write(`${JSON.stringify(message)}\0`);
  }

  async stop() {
    for (const socket of this.sockets) socket.destroy();
    this.sockets.clear();
    if (this.server === null) return;
    const server = this.server;
    this.server = null;
    await new Promise((resolve) => server.close(resolve));
  }
}

module.exports = {
  WaveformControlServer, WCP_OPTIONAL_VERSION: OPTIONAL_VERSION, WCP_VERSION: VERSION,
};
