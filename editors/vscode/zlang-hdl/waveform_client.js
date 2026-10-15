// SPDX-License-Identifier: Apache-2.0
'use strict';

const childProcess = require('node:child_process');
const readline = require('node:readline');

class WaveformQueryClient {
  constructor(executable) {
    this.sequence = 0;
    this.pending = new Map();
    this.process = childProcess.spawn(executable, [], { stdio: ['pipe', 'pipe', 'pipe'] });
    this.stderr = '';
    this.process.stderr.setEncoding('utf8');
    this.process.stderr.on('data', (chunk) => { this.stderr += chunk; });
    this.process.on('error', (error) => this.failAll(error));
    const lines = readline.createInterface({ input: this.process.stdout });
    lines.on('line', (line) => this.receive(line));
    this.process.on('exit', (code) => {
      const message = this.stderr.trim()
        || `waveform indexer exited with status ${code ?? 'unknown'}`;
      this.failAll(new Error(message));
    });
  }

  receive(line) {
    let response;
    try { response = JSON.parse(line); } catch (error) {
      this.failAll(error);
      return;
    }
    const pending = this.pending.get(response.id);
    if (pending === undefined) return;
    this.pending.delete(response.id);
    clearTimeout(pending.timer);
    if (response.ok) pending.resolve(response.result);
    else pending.reject(new Error(response.error || 'waveform query failed'));
  }

  request(command, fields = {}) {
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`waveform query '${command}' timed out`));
      }, 30_000);
      this.pending.set(id, { resolve, reject, timer });
      this.process.stdin.write(`${JSON.stringify({ id, command, ...fields })}\n`, (error) => {
        if (error) {
          this.pending.delete(id);
          clearTimeout(timer);
          reject(error);
        }
      });
    });
  }

  failAll(error) {
    for (const { reject, timer } of this.pending.values()) {
      clearTimeout(timer);
      reject(error);
    }
    this.pending.clear();
  }

  async close() {
    if (this.process.exitCode !== null) return;
    try { await this.request('close'); } catch {}
    this.process.stdin.end();
  }
}

module.exports = { WaveformQueryClient };
