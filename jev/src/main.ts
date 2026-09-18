// main.ts — the JEV bridge runtime. Wires: game link -> state compiler ->
// Jev calls -> decision composer -> protocol lines back to the engine.
// Modes: live (default), --replay <file> (recorded observations), --dry-run.

import { mkdirSync, appendFileSync, readFileSync, existsSync, watch } from 'node:fs';
import { join } from 'node:path';
import { loadConfig, type CadencePreset, type BridgeConfig } from './config.js';
import { JevClient } from './jevClient.js';
import { GameLink } from './gameLink.js';
import { squadsOf, compileSquad, compilePacing, compileBuddy, compileAdapt, compileNemesis } from './stateCompiler.js';
import { DecisionComposer } from './composer.js';
import { LearningTracker } from './learning.js';
import type { JevAnswer, JevRequest, NemesisState, Observation } from './types.js';

const presetArg = (() => {
  const i = process.argv.indexOf('--cadence');
  return i >= 0 ? (process.argv[i + 1] as CadencePreset) : 'default';
})();
const replayPath = (() => {
  const i = process.argv.indexOf('--replay');
  return i >= 0 ? process.argv[i + 1] : undefined;
})();

const cfg: BridgeConfig = loadConfig(presetArg && ['aggressive', 'default', 'economy'].includes(presetArg) ? presetArg : 'default');
mkdirSync(cfg.logDir, { recursive: true });

const jev = new JevClient(cfg, join(cfg.logDir, 'calls.jsonl'));
const learning = new LearningTracker(cfg);
const composer = new DecisionComposer(cfg);
const link = new GameLink(cfg, {
  onObservation: (obs) => void handleObservation(obs).catch(e => console.error('[bridge] obs error', e)),
  onStatus: (name, up, detail) => console.log(`[link] ${name} ${up ? 'UP' : 'DOWN'} (${detail})`),
});

// ---------------------------------------------------------------------------
// Digest caching + cadence
// ---------------------------------------------------------------------------

let lastObs: Observation | null = null;
let lastSquadAt = 0;
let lastPacingAt = 0;
let lastBuddyAt = 0;
let lastAdaptAt = 0;
let lastNemesisEventIdx = 0;
const pendingDeathJudgments: Array<{ type: string; weapon: string; nem: NemesisState | undefined }> = [];

function noteLine(kind: string, detail: string) {
  appendFileSync(join(cfg.logDir, 'emitted.jsonl'), `${JSON.stringify({ t: Date.now(), kind, detail })}\n`);
}

// ---------------------------------------------------------------------------
// Observation intake
// ---------------------------------------------------------------------------

async function handleObservation(obs: Observation) {
  lastObs = obs;
  learning.tick(obs);

  const nem = obs.nemesis;
  learning.ingestNemesis(nem);

  // Death-judgment events from the engine ring: "N:kill:type:weapon".
  if (nem?.events?.length) {
    for (const label of nem.events) {
      const m = /^(\d+):kill:(\w+):(\w+)$/.exec(label);
      if (!m) continue;
      const idx = Number(m[1]);
      if (idx <= lastNemesisEventIdx) continue;
      lastNemesisEventIdx = idx;
      pendingDeathJudgments.push({ type: m[2]!, weapon: m[3]!, nem });
    }
  }

  const now = Date.now();
  const squads = squadsOf(obs);

  // --- Squad tactics (top squad only per cycle; rotation handles the rest) ---
  if (squads.length && now - lastSquadAt >= cfg.cadence.squadMs) {
    lastSquadAt = now;
    const squad = squads[0]!;
    await runSquad(squad);
  }

  // --- Pacing ---
  if (now - lastPacingAt >= cfg.cadence.pacingMs) {
    lastPacingAt = now;
    await runPacing(obs);
  }

  // --- Buddy ---
  if (obs.buddy && now - lastBuddyAt >= cfg.cadence.buddyMs) {
    lastBuddyAt = now;
    await runBuddy(obs);
  }

  // --- Adaptation ---
  if (now - lastAdaptAt >= cfg.cadence.playerModelMs) {
    lastAdaptAt = now;
    await runAdapt(obs);
  }

  // --- Death-time nemesis judgments (drain one per cycle) ---
  const pending = pendingDeathJudgments.shift();
  if (pending) await runNemesisJudgment(pending.type, pending.weapon, pending.nem);
}

// ---------------------------------------------------------------------------
// Call sites
// ---------------------------------------------------------------------------

function buildReq(purpose: string, c: { state: unknown; questions: Record<string, import('./types.js').JevQuestion> }): JevRequest {
  return { state: c.state, model: cfg.typesafe.model, questions: c.questions };
}

async function runSquad(squad: { region: number; members: Observation['monsters'] }) {
  if (!lastObs?.player) return;
  const compiled = compileSquad(lastObs, squad, learning, cfg);
  const answers = await jev.call('squad', buildReq('squad', compiled));
  const emit = composer.composeSquad(lastObs, squad, answers?.answers ?? null);
  for (const a of emit.acts) link.act(a.order, a.ids, { focus: a.focus, forTics: a.forTics });
  if (emit.acts.length) noteLine('act', JSON.stringify(emit.acts));
}

async function runPacing(obs: Observation) {
  const compiled = compilePacing(obs, learning, cfg);
  const answers = await jev.call('pacing', buildReq('pacing', compiled));
  const emit = composer.composePacing(obs, answers?.answers ?? null, learning.difficulty);

  for (const s of emit.spawns) {
    link.spawn(s.type, s.count);
    noteLine('spawn', `${s.type} x${s.count}`);
  }
  for (const it of emit.items) { link.item(it); noteLine('item', it); }
  if (emit.relax) { link.relax(); noteLine('relax', ''); }
}

async function runBuddy(obs: Observation) {
  const compiled = compileBuddy(obs, cfg);
  if (!compiled) return;
  const answers = await jev.call('buddy', buildReq('buddy', compiled));
  const emit = composer.composeBuddy(obs, answers?.answers ?? null);
  if (emit.buddy) { link.buddy(emit.buddy.order, { forTics: emit.buddy.forTics }); noteLine('buddy', emit.buddy.order); }
}

async function runAdapt(obs: Observation) {
  const compiled = compileAdapt(obs, learning, cfg);
  const answers = await jev.call('adapt', buildReq('adapt', compiled));
  const composed = composer.composeAdaptation(answers?.answers ?? null);
  if (!composed) return;
  const result = learning.applyAdaptation(composed);
  if (result.stepped) noteLine('difficulty', `${result.reason} ${result.from}->${result.to}`);
}

async function runNemesisJudgment(type: string, weapon: string, nem: NemesisState | undefined) {
  const compiled = compileNemesis(type, weapon, nem, learning, cfg);
  if (!compiled) return;
  const answers = await jev.call('nemesis', buildReq('nemesis', compiled));
  const deltas = composer.composeNemesis(type, answers?.answers ?? null);
  if (deltas.length) {
    link.nemesisPropose(deltas);
    noteLine('nemesis', JSON.stringify(deltas));
  }
}

// ---------------------------------------------------------------------------
// Replay mode: feed recorded observations through the identical pipeline.
// ---------------------------------------------------------------------------

function runReplay(path: string) {
  if (!existsSync(path)) { console.error(`[replay] no such file: ${path}`); process.exit(1); }
  const lines = readFileSync(path, 'utf8').split('\n').filter(l => l.trim());
  console.log(`[replay] ${lines.length} recorded observations`);
  let i = 0;
  const timer = setInterval(async () => {
    const line = lines[i++];
    if (!line) {
      clearInterval(timer);
      console.log('[replay] done. Emitted lines:', readFileSync(join(cfg.logDir, 'emitted.jsonl'), 'utf8').split('\n').filter(Boolean).length);
      console.log('[replay] Jev stats:', JSON.stringify(jev.stats()));
      process.exit(0);
    }
    try { await handleObservation(JSON.parse(line) as Observation); } catch (e) { console.error('[replay] line error', e); }
  }, 50);
}

// ---------------------------------------------------------------------------
// Status ticker
// ---------------------------------------------------------------------------

setInterval(() => {
  const s = jev.stats();
  console.log(`[jev] calls=${s.calls} ok=${s.ok} p50=${s.p50}ms p95=${s.p95}ms inflight=${s.inflight} model=${s.lastModel ?? '-'} | diff=${learning.difficulty.toFixed(2)}`);
}, 10000);

if (replayPath) {
  runReplay(replayPath);
} else {
  console.log(`[bridge] starting: cadence=${presetArg} dryRun=${cfg.dryRun} director=:${cfg.game.directorPort} player=:${cfg.game.playerPort}`);
  link.start();
}

process.on('SIGINT', () => { link.stop(); jev.close(); process.exit(0); });
