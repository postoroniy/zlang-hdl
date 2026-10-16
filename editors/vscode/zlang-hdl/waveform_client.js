// SPDX-License-Identifier: Apache-2.0
'use strict';

const childProcess = require('node:child_process');
const readline = require('node:readline');

// Keep one running query and only the newest waiting query for each viewport
// or cursor. Rapid panning cannot build an unbounded native RPC backlog.
class WaveformQueryQueue {
  constructor(send) {this.send=send;this.jobs=new Map();this.active=false;this.closed=false;}
  request(purpose,command,fields) {
    if(this.closed)return Promise.reject(new Error('Waveform query session is closed'));
    const old=this.jobs.get(purpose);if(old)old.resolve(null);
    if(!old&&this.jobs.size>=32)return Promise.reject(new Error('Waveform query queue is full'));
    return new Promise((resolve,reject)=>{
      this.jobs.set(purpose,{purpose,command,fields,resolve,reject});void this.drain();
    });
  }
  async drain() {
    if(this.active)return;
    this.active=true;
    try{
      while(this.jobs.size&&!this.closed){
        const candidates=[...this.jobs.values()];
        const job=candidates.find(item=>item.command==='search')
          ||candidates.find(item=>item.command==='value_at')||candidates[0];
        this.jobs.delete(job.purpose);
        try{job.resolve(await this.send(job.command,job.fields));}catch(error){job.reject(error);}
      }
    }finally{this.active=false;}
  }
  dispose(){this.closed=true;for(const job of this.jobs.values())job.resolve(null);this.jobs.clear();}
}

class WaveformQueryClient {
  constructor(executable, { onTiming = null } = {}) {
    this.onTiming = onTiming;
    this.sequence = 0;
    this.pending = new Map();
    this.closing = null;
    this.exited = false;
    this.failure = null;
    this.process = childProcess.spawn(executable, [], { stdio: ['pipe', 'pipe', 'pipe'] });
    this.stderr = '';
    this.process.stderr.setEncoding('utf8');
    this.process.stderr.on('data', (chunk) => { this.stderr += chunk; });
    this.process.on('error', (error) => this.failAll(error));
    this.process.stdin.on('error', (error) => this.failAll(error));
    const lines = readline.createInterface({ input: this.process.stdout });
    lines.on('line', (line) => this.receive(line));
    this.process.on('exit', () => { this.exited = true; });
    // `exit` can precede the last stdout data. Drain final replies first.
    this.process.on('close', (code) => {
      const message = this.stderr.trim()
        || `waveform indexer exited with status ${code ?? 'unknown'}`;
      this.failAll(new Error(message));
    });
  }

  receive(line) {
    const started = this.onTiming ? performance.now() : 0;
    let response;
    try { response = JSON.parse(line); } catch (error) {
      this.failAll(error);
      return;
    }
    const pending = this.pending.get(response.id);
    if (pending === undefined) return;
    this.pending.delete(response.id);
    clearTimeout(pending.timer);
    if (pending.timing) this.onTiming({
      ...pending.timing, id: response.id, decodeMs: performance.now() - started,
      roundtripMs: performance.now() - pending.timing.started,
      responseBytes: Buffer.byteLength(line, 'utf8'),
    });
    if (response.ok) pending.resolve(response.result);
    else pending.reject(new Error(response.error || 'waveform query failed'));
  }

  request(command, fields = {}) {
    if (this.failure) return Promise.reject(this.failure);
    if (this.closing || this.exited || this.process.stdin.writableEnded)
      return Promise.reject(new Error('Waveform query session is closed'));
    return this.send(command, fields);
  }

  send(command, fields = {}) {
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`waveform query '${command}' timed out`));
      }, 30_000);
      this.pending.set(id, { resolve, reject, timer });
      const started = this.onTiming ? performance.now() : 0;
      const frame = `${JSON.stringify({ id, command, ...fields })}\n`;
      if (this.onTiming) this.pending.get(id).timing = {
        command, started, encodeMs: performance.now() - started,
      };
      this.process.stdin.write(frame, (error) => {
        if (error) {
          this.pending.delete(id);
          clearTimeout(timer);
          reject(error);
        }
      });
    });
  }

  failAll(error) {
    this.failure = error;
    for (const { reject, timer } of this.pending.values()) {
      clearTimeout(timer);
      reject(error);
    }
    this.pending.clear();
  }

  close() {
    if (this.closing) return this.closing;
    if (this.failure || this.exited || this.process.exitCode !== null) {
      this.closing = Promise.resolve();
      this.process.stdin.destroy();
      return this.closing;
    }
    // Stop admission synchronously. The terminal frame and EOF are queued
    // together, before the peer acknowledges close and exits. No later query
    // or duplicate dispose may write to the peer's already-closed pipe.
    this.closing = this.send('close').catch(() => {});
    this.process.stdin.end();
    return this.closing;
  }
}

module.exports = { WaveformQueryClient, WaveformQueryQueue };
