// gameLink.ts — TCP clients for BuddyDoom's two loopback listeners plus the
// protocol emitters. One client per listener (engine constraint). All line
// vocabulary is the exact parser vocabulary verified in files/p_ai_llm.c.

import { createConnection, Socket } from 'node:net';
import type { BridgeConfig } from './config.js';
import type { MonsterOrder, Observation, PendingLine } from './types.js';

interface LinkEvents {
  onObservation: (obs: Observation) => void;
  onStatus: (link: 'director' | 'player', up: boolean, detail: string) => void;
}

export class GameLink {
  private cfg: BridgeConfig;
  private events: LinkEvents;
  private directorSock: Socket | null = null;
  private playerSock: Socket | null = null;
  private dirBuf = '';
  private plBuf = '';
  private pending: PendingLine[] = [];   // flushed on connect + on each poll cycle
  private pollTimer: NodeJS.Timeout | null = null;
  private stopped = false;

  constructor(cfg: BridgeConfig, events: LinkEvents) {
    this.cfg = cfg;
    this.events = events;
  }

  start() {
    this.connectDirector();
    this.connectPlayer();
    // Poll: the protocol is request/response — we ask, the game answers.
    this.pollTimer = setInterval(() => {
      this.flushPending();
      if (this.directorSock && !this.directorSock.destroyed) {
        this.directorSock.write('observe\n');
      }
    }, 100);
  }

  stop() {
    this.stopped = true;
    if (this.pollTimer) clearInterval(this.pollTimer);
    this.directorSock?.destroy();
    this.playerSock?.destroy();
  }

  private connectDirector() {
    if (this.stopped) return;
    const sock = createConnection({ host: this.cfg.game.host, port: this.cfg.game.directorPort }, () => {
      this.events.onStatus('director', true, 'connected');
      this.flushPending();
    });
    sock.on('error', (e) => {
      this.events.onStatus('director', false, (e as Error).message);
      sock.destroy();
      setTimeout(() => this.connectDirector(), 2000);
    });
    sock.on('close', () => {
      this.directorSock = null;
      if (!this.stopped) setTimeout(() => this.connectDirector(), 2000);
    });
    sock.on('data', (d: Buffer) => { this.dirBuf += d.toString('utf8'); this.drainDirector(); });
    this.directorSock = sock;
  }

  private connectPlayer() {
    if (this.stopped) return;
    const sock = createConnection({ host: this.cfg.game.host, port: this.cfg.game.playerPort }, () => {
      this.events.onStatus('player', true, 'connected');
    });
    sock.on('error', (e) => {
      // The player agent is optional (-aiplayer may not be running).
      this.events.onStatus('player', false, (e as Error).message);
      sock.destroy();
      setTimeout(() => this.connectPlayer(), 5000);
    });
    sock.on('close', () => {
      this.playerSock = null;
      if (!this.stopped) setTimeout(() => this.connectPlayer(), 5000);
    });
    sock.on('data', (d: Buffer) => { this.plBuf += d.toString('utf8'); this.drainPlayer(); });
    this.playerSock = sock;
  }

  private drainDirector() {
    let idx: number;
    while ((idx = this.dirBuf.indexOf('\n')) >= 0) {
      const line = this.dirBuf.slice(0, idx).trim();
      this.dirBuf = this.dirBuf.slice(idx + 1);
      if (!line) continue;
      try {
        const obs = JSON.parse(line) as Observation;
        if (!obs.nolevel) this.events.onObservation(obs);
      } catch { /* partial or non-JSON line; ignore */ }
    }
  }

  private drainPlayer() {
    let idx: number;
    while ((idx = this.plBuf.indexOf('\n')) >= 0) {
      const line = this.plBuf.slice(0, idx).trim();
      this.plBuf = this.plBuf.slice(idx + 1);
      try {
        const obs = JSON.parse(line) as Observation;
        if (!obs.nolevel && obs.player) this.events.onObservation({ ...obs, nolevel: false });
      } catch { /* ignore */ }
    }
  }

  private flushPending() {
    if (!this.pending.length) return;
    const sock = this.directorSock;
    if (!sock || sock.destroyed) return;   // buffered until reconnect
    const batch = this.pending.splice(0, this.pending.length);
    for (const p of batch) sock.write(p.line + '\n');
  }

  // ------------------------------------------------------------------
  // Protocol emitters. Vocabulary below matches AI_OrderByName(),
  // AI_BuddyTactic(), P_Director_LLMSpawn() and the watchdog semantics.
  // ------------------------------------------------------------------

  act(order: MonsterOrder, ids: number[], opts?: { focus?: number; x?: number; y?: number; forTics?: number; afterTics?: number }) {
    if (!ids.length) return;
    const parts = [`act order=${order}`, `ids=${ids.join(',')}`];
    if (opts?.focus !== undefined) parts.push(`focus=${opts.focus}`);
    if (opts?.x !== undefined) parts.push(`x=${Math.round(opts.x)}`);
    if (opts?.y !== undefined) parts.push(`y=${Math.round(opts.y)}`);
    parts.push(`for=${opts?.forTics ?? 70}`);
    if (opts?.afterTics) parts.push(`after=${opts.afterTics}`);
    this.pending.push({ line: parts.join(' '), kind: 'act' });
  }

  buddy(order: string, opts?: { focus?: number; x?: number; y?: number; forTics?: number }) {
    const parts = [`buddy order=${order}`];
    if (opts?.focus !== undefined) parts.push(`focus=${opts.focus}`);
    if (opts?.x !== undefined) parts.push(`x=${Math.round(opts.x)}`);
    if (opts?.y !== undefined) parts.push(`y=${Math.round(opts.y)}`);
    if (opts?.forTics) parts.push(`for=${opts.forTics}`);
    this.pending.push({ line: parts.join(' '), kind: 'buddy' });
  }

  /** Monster spawn — name must be one of P_Director_TypeByName's accepted names. */
  spawn(type: string, count = 1) {
    const c = Math.max(1, Math.min(8, Math.round(count)));
    this.pending.push({ line: `spawn type=${type} count=${c}`, kind: 'spawn' });
  }

  item(kind: 'medkit' | 'ammo') {
    this.pending.push({ line: `spawn item=${kind}`, kind: 'item' });
  }

  relax() {
    this.pending.push({ line: 'director relax', kind: 'relax' });
  }

  /**
   * Nemesis weight-shift proposal (our files/p_nemesis.c extension).
   * The engine clamps every delta — a buggy bridge cannot unbalance the table.
   */
  nemesisPropose(deltas: string[]) {
    if (!deltas.length) return;
    this.pending.push({ line: `nemesis ${deltas.join(' ')}`, kind: 'nemesis' });
  }

  connected(): boolean {
    return !!(this.directorSock && !this.directorSock.destroyed);
  }
}
