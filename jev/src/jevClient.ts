// jevClient.ts — Jev HTTP client: concurrency pool, exponential backoff on
// 429/529 (documented behavior), call log with latency + returned model string.

import { createWriteStream, WriteStream } from 'node:fs';
import type { BridgeConfig } from './config.js';
import type { JevRequest, JevResponse } from './types.js';

export interface CallRecord {
  t: number;               // epoch ms
  purpose: string;         // which call site
  latencyMs: number;
  ok: boolean;
  status?: number;
  model?: string;          // returned model id (pin thresholds per version!)
  inputTokens?: number;
  error?: string;
}

export class JevClient {
  private cfg: BridgeConfig;
  private inflight = 0;
  private queue: Array<() => void> = [];
  private records: CallRecord[] = [];
  private logStream: WriteStream | null = null;

  constructor(cfg: BridgeConfig, purposeLogPath?: string) {
    this.cfg = cfg;
    if (purposeLogPath) {
      this.logStream = createWriteStream(purposeLogPath, { flags: 'a' });
    }
  }

  stats() {
    const ok = this.records.filter(r => r.ok);
    const lat = ok.map(r => r.latencyMs).sort((a, b) => a - b);
    const pct = (p: number) => (lat.length ? lat[Math.min(lat.length - 1, Math.floor(p * lat.length))] : 0);
    return {
      calls: this.records.length,
      ok: ok.length,
      p50: pct(0.5), p95: pct(0.95),
      lastModel: ok.length ? ok[ok.length - 1]!.model : undefined,
      inflight: this.inflight,
      queued: this.queue.length,
    };
  }

  recentCalls(n = 20): CallRecord[] { return this.records.slice(-n); }

  private acquire(): Promise<() => void> {
    return new Promise(resolve => {
      const tryStart = () => {
        if (this.inflight < this.cfg.typesafe.concurrency) {
          this.inflight++;
          resolve(() => { this.inflight--; const next = this.queue.shift(); if (next) next(); });
        } else {
          this.queue.push(tryStart);
        }
      };
      tryStart();
    });
  }

  private log(r: CallRecord) {
    this.records.push(r);
    if (this.records.length > 2000) this.records.splice(0, 1000);
    this.logStream?.write(JSON.stringify(r) + '\n');
  }

  async call(purpose: string, req: JevRequest): Promise<JevResponse | null> {
    if (this.cfg.dryRun) {
      // Dry run: no key. Log the request shape so the pipeline is fully testable
      // offline, and return null (composer treats null as "no new information").
      this.log({ t: Date.now(), purpose, latencyMs: 0, ok: false, error: 'dry-run' });
      return null;
    }

    const maxAttempts = 4;
    let attempt = 0;
    let lastErr = '';

    while (attempt < maxAttempts) {
      attempt++;
      const release = await this.acquire();
      const t0 = Date.now();
      try {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), this.cfg.typesafe.timeoutMs);
        const res = await fetch(this.cfg.typesafe.endpoint, {
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${this.cfg.typesafe.apiKey}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify(req),
          signal: ctrl.signal,
        });
        clearTimeout(timer);

        if (res.status === 429 || res.status === 529) {
          // Documented rate-limit / overload: exponential backoff.
          const wait = Math.min(8000, 250 * 2 ** attempt) + Math.random() * 200;
          this.log({ t: Date.now(), purpose, latencyMs: Date.now() - t0, ok: false, status: res.status, error: 'backoff' });
          release();
          await new Promise(r => setTimeout(r, wait));
          continue;
        }
        if (!res.ok) {
          lastErr = `HTTP ${res.status}`;
          this.log({ t: Date.now(), purpose, latencyMs: Date.now() - t0, ok: false, status: res.status, error: lastErr });
          release();
          break;
        }
        const body = (await res.json()) as JevResponse;
        this.log({
          t: Date.now(), purpose, latencyMs: Date.now() - t0, ok: true,
          status: res.status, model: body.model, inputTokens: body.usage?.input_tokens,
        });
        release();
        return body;
      } catch (e) {
        lastErr = e instanceof Error ? e.message : String(e);
        this.log({ t: Date.now(), purpose, latencyMs: Date.now() - t0, ok: false, error: lastErr });
        release();
        // Network error: short backoff, retry.
        await new Promise(r => setTimeout(r, 300 * attempt));
      }
    }
    return null;
  }

  close() { this.logStream?.end(); }
}
