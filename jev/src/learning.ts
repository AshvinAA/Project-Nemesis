// learning.ts — the continuous learning layer. Ground truth comes from the
// engine's P_DamageMobj/P_KillMobj hooks (nemesys event labels); this module
// never infers events from polling — it consumes labels.

import type { NemesisRow, NemesisState, Observation } from './types.js';
import type { BridgeConfig } from './config.js';

export interface LearningState {
  skill: Record<'aim' | 'movement' | 'resource' | 'aggression' | 'routing' | 'adaptability', number>;
  evidence: Record<string, number>;
  difficulty: number;
  deathsRecent5m: number;
  stallSeconds: number;
  skillSummary(): string;
  trendLine(): string;
  boredomSignal(obs: Observation): string;
  noteEvent(label: string): void;
  tick(obs: Observation): void;
  /** Extract observation-side deltas from the engine nemesis event labels. */
  ingestNemesis(nem: NemesisState | undefined): void;
}

const AXES = ['aim', 'movement', 'resource', 'aggression', 'routing', 'adaptability'] as const;
type Axis = (typeof AXES)[number];

export class LearningTracker implements LearningState {
  private cfg: BridgeConfig;
  private events: Array<{ t: number; label: string; parts: string[] }> = [];
  private lastObsT = 0;
  private lastCombatT = 0;
  private lastDmgT = 0;
  private seenEventIdx = 0;    // cursor into the engine's ring (monotonic labels)

  skill: Record<Axis, number> = { aim: 0.5, movement: 0.5, resource: 0.5, aggression: 0.5, routing: 0.5, adaptability: 0.5 };
  evidence: Record<string, number> = { aim: 0, movement: 0, resource: 0, aggression: 0, routing: 0, adaptability: 0 };
  difficulty = 2;
  deathsRecent5m = 0;
  stallSeconds = 0;

  constructor(cfg: BridgeConfig) {
    this.cfg = cfg;
  }

  skillSummary(): string {
    return Object.entries(this.skill).map(([k, v]) => `${k} ${v.toFixed(2)}`).join(', ');
  }

  trendLine(): string {
    return `difficulty ${this.difficulty.toFixed(1)}, evidence ${Object.entries(this.evidence).filter(([, v]) => v > 0).map(([k, v]) => `${k}:${v}`).join(',') || 'none'}`;
  }

  boredomSignal(obs: Observation): string {
    const now = Date.now();
    const sinceCombat = this.lastCombatT ? (now - this.lastCombatT) / 1000 : 0;
    return `no_combat_s ${Math.round(sinceCombat)}, census ${obs.monsters.filter(m => m.hp > 0).length}`;
  }

  noteEvent(label: string) {
    // Bridge-side event (future: sounds/dodges from the player-agent stream).
    this.events.push({ t: Date.now(), label, parts: [] });
    if (this.events.length > 512) this.events.splice(0, 256);
  }

  /** Consume the engine's ground-truth event labels (nemesis.events). */
  ingestNemesis(nem: NemesisState | undefined) {
    if (!nem?.events?.length) return;
    // Engine ring labels are unique (event index embedded), so we can dedupe by
    // keeping a cursor: ingest only labels we have never seen.
    for (const label of nem.events) {
      if (this.processedLabels.has(label)) continue;
      this.processedLabels.add(label);
      this.applyEventLabel(label);
    }
    if (this.processedLabels.size > 4096) {
      // Keep the set bounded: labels embed a monotonic engine event index, so
      // dropping the oldest half is safe.
      const arr = [...this.processedLabels];
      arr.sort();
      this.processedLabels = new Set(arr.slice(arr.length / 2));
    }
  }

  private processedLabels = new Set<string>();

  private applyEventLabel(label: string) {
    // Labels are emitted by p_nemesis.c, e.g.:
    //   "12:hit:imp:shotgun:24"   event# : kind : monster type : weapon : damage
    //   "12:kill:imp:shotgun"
    const parts = label.split(':');
    if (parts.length < 4) return;
    const kind = parts[1]!;
    const mtype = parts[2]!;
    const weapon = parts[3]!;
    const dmg = Number(parts[4] ?? 0);

    if (kind === 'hit' || kind === 'kill') {
      // aim: damage throughput per weapon touch (ground truth)
      const bump = kind === 'kill' ? 0.06 : Math.min(0.02, dmg / 1500);
      this.bump('aim', bump, 1);
      // aggression: engaging at all
      this.bump('aggression', 0.004, 1);
      this.lastCombatT = Date.now();
    }
    if (kind === 'kill') {
      this.lastCombatT = Date.now();
      void mtype; void weapon; void dmg; // further per-type analytics live in the nemesis table itself
    }
  }

  private bump(axis: Axis, delta: number, evidenceWeight = 1) {
    const cur = this.skill[axis];
    const target = Math.min(1, Math.max(0, cur + delta));
    this.skill[axis] = cur + this.cfg.learning.ewmaLambda * (target - cur);
    this.evidence[axis] = (this.evidence[axis] ?? 0) + evidenceWeight;
  }

  tick(obs: Observation) {
    const now = Date.now();
    const dt = this.lastObsT ? now - this.lastObsT : 0;
    this.lastObsT = now;

    // Player took visible damage? director.recent_dmg > 0 is engine ground truth.
    const rd = obs.director?.recent_dmg ?? 0;
    if (rd > 0) {
      this.lastCombatT = now;
      // movement: taking damage while mobile — crude but engine-truthed
      this.bump('movement', rd > 40 ? -0.01 : -0.004, 1);
    }

    // Stall/boredom accounting.
    const census = obs.monsters.filter(m => m.hp > 0).length;
    if (census === 0 && now - this.lastCombatT > 20000) this.stallSeconds = Math.round((now - this.lastCombatT) / 1000);
    else if (census > 0) this.stallSeconds = 0;

    // Deaths: player health 0 in consecutive observations.
    if (obs.player?.dead || (obs.player && obs.player.health !== undefined && obs.player.health <= 0)) {
      const nowS = now;
      if (nowS - (this.lastDeathT ?? 0) > 3000) {
        this.lastDeathT = nowS;
        this.deathsRecent5m++;
        this.bump('adaptability', -0.05, 1);
      }
    }
    // Window decay of the 5-min death counter.
    if (this.deathWindow.length && now - this.deathWindow[0]! > 300_000) this.deathWindow.shift();
    this.deathsRecent5m = this.deathWindow.length;

    // resource axis: ammo_pct trend from the engine
    const ap = obs.director?.ammo_pct;
    if (ap !== undefined) {
      if (this.lastAmmoPct !== undefined && ap < this.lastAmmoPct - 10) this.bump('resource', -0.004, 1);
      if (this.lastAmmoPct !== undefined && ap > this.lastAmmoPct) this.bump('resource', 0.002, 1);
      this.lastAmmoPct = ap;
    }
  }

  private deathWindow: number[] = [];
  private lastDeathT = 0;
  private lastAmmoPct: number | undefined;

  /** Difficulty controller: bounded step, evidence-gated, frustration-first. */
  applyAdaptation(ans: {
    skill_is_outpacing_difficulty: { noul: number };
    boredom_risk: { noul: number };
    frustration_risk: { noul: number };
    strongest_axis?: { choice: string };
  }): { stepped: boolean; from: number; to: number; reason: string } {
    const c = this.cfg.learning;
    const evidenceMass = AXES.reduce((s, a) => s + (this.evidence[a] ?? 0), 0);
    const from = this.difficulty;

    // Frustration wins over boredom — never stack difficulty on a struggling player.
    if (ans.frustration_risk?.noul > 0.6) {
      this.difficulty = Math.max(c.difficultyFloor, this.difficulty - c.maxStepPerInterval);
      return { stepped: true, from, to: this.difficulty, reason: 'frustration' };
    }
    if (evidenceMass < c.minEvidence) {
      return { stepped: false, from, to: this.difficulty, reason: 'low-evidence' };
    }
    if (ans.skill_is_outpacing_difficulty?.noul > 0.65 && ans.boredom_risk?.noul < 0.4) {
      this.difficulty = Math.min(c.difficultyCeiling, this.difficulty + c.maxStepPerInterval);
      return { stepped: true, from, to: this.difficulty, reason: 'dominance' };
    }
    if (ans.boredom_risk?.noul > 0.6) {
      this.difficulty = Math.min(c.difficultyCeiling, this.difficulty + c.maxStepPerInterval / 2);
      return { stepped: true, from, to: this.difficulty, reason: 'boredom' };
    }
    return { stepped: false, from, to: this.difficulty, reason: 'hold' };
  }
}
