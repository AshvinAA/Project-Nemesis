// composer.ts — the Decision Composer: turns Jev's calibrated probabilities
// into exact protocol lines. All judgment composition lives here as ordinary
// code: thresholds, hysteresis, safety gates, vocabulary mapping.
// Property invariants (tested): never emits an order outside the engine's
// vocabulary, never targets an id outside the current roster, never spawns
// beyond the cap, never spawns when the player is critically stressed.

import type { BridgeConfig } from './config.js';
import type {
  BuddyOrder, ChoiceAnswer, JevAnswer, MonsterOrder, NoulAnswer, Observation,
  ScoreAnswer, SpawnType,
} from './types.js';
import { MONSTER_ORDERS, SPAWN_TYPES, hpFraction } from './types.js';

interface ComposerState {
  lastSquadOrderAt: number;
  lastBuddyOrderAt: number;
  lastSpecies: SpawnType;
  consecutiveSpawnBlocks: number;
}

export interface Emit {
  acts: Array<{ order: MonsterOrder; ids: number[]; focus?: number; forTics: number }>;
  buddy?: { order: BuddyOrder; forTics?: number };
  spawns: Array<{ type: SpawnType; count: number }>;
  items: Array<'medkit' | 'ammo'>;
  relax: boolean;
  difficultyDelta?: 'up' | 'down' | 'hold';
}

function asChoice(a: JevAnswer | undefined): ChoiceAnswer | null {
  return a && a.type === 'choice' ? a : null;
}
function asNoul(a: JevAnswer | undefined): NoulAnswer | null {
  return a && a.type === 'noul' ? a : null;
}
function asScore(a: JevAnswer | undefined): ScoreAnswer | null {
  return a && a.type === 'score' ? a : null;
}

export class DecisionComposer {
  private cfg: BridgeConfig;
  private st: ComposerState;

  constructor(cfg: BridgeConfig) {
    this.cfg = cfg;
    this.st = { lastSquadOrderAt: 0, lastBuddyOrderAt: 0, lastSpecies: 'imp', consecutiveSpawnBlocks: 0 };
  }

  /** Critical-stress gate, ported from the rule director's own safety rules. */
  static playerStressed(obs: Observation): boolean {
    const hp = obs.player?.health ?? 100;
    const ammoPct = obs.director?.ammo_pct ?? 50;
    return hp < 30 || ammoPct < 10;
  }

  // ---------------------------------------------------------------------------
  // Squad tactics: choice argmax -> act lines (hysteresis per squad)
  // ---------------------------------------------------------------------------

  composeSquad(
    obs: Observation,
    squad: { region: number; members: Observation['monsters'] },
    answers: Record<string, JevAnswer> | null,
  ): Emit {
    const out: Emit = { acts: [], spawns: [], items: [], relax: false };
    const now = Date.now();

    // Hysteresis: an order must persist for its lifetime before re-evaluation.
    if (now - this.st.lastSquadOrderAt < this.cfg.composer.orderHoldMs.squad) return out;

    const stance = asChoice(answers?.squad_stance);
    if (!stance) return out;

    const orderRaw = stance.choice;
    if (!MONSTER_ORDERS.includes(orderRaw as MonsterOrder)) return out; // belt & braces: never emit outside vocabulary
    const order = orderRaw as MonsterOrder;

    // Safety gates before committing ids.
    const alive = squad.members.filter(m => m.hp > 0);
    if (!alive.length) return out;

    // flank orders need distance and an open route (link-graph check).
    if (order === 'flank_left' || order === 'flank_right') {
      const avgD = alive.reduce((s, m) => s + m.d_player, 0) / alive.length;
      const routeOpen = this.flankRouteOpen(obs, squad.region);
      if (avgD < 200 || !routeOpen) {
        // fall back to focus_fire: flanking is off the table this cycle
        out.acts.push({ order: 'focus_fire', ids: alive.map(m => m.id), focus: 0, forTics: this.cfg.composer.actForTics });
        this.st.lastSquadOrderAt = now;
        return out;
      }
    }

    // Wounded squads don't press: override a confident press when hp is low.
    const hpFrac = alive.reduce((s, m) => s + hpFraction(m.type, m.hp), 0) / alive.length;
    if ((order === 'focus_fire' || order === 'chase') && hpFrac < 0.25) {
      out.acts.push({ order: 'fallback', ids: alive.map(m => m.id), forTics: this.cfg.composer.actForTics });
      this.st.lastSquadOrderAt = now;
      return out;
    }

    const focus = order === 'focus_fire' ? 0 : undefined;
    out.acts.push({ order, ids: alive.map(m => m.id), focus, forTics: this.cfg.composer.actForTics });
    this.st.lastSquadOrderAt = now;
    return out;
  }

  /** True if a connected neighbour region of `region` exists that isn't the player's. */
  private flankRouteOpen(obs: Observation, region: number): boolean {
    if (!obs.links?.length || !obs.player?.region) return false;
    return obs.links.some(l =>
      l.kind !== 'locked' &&
      (l.a === region || l.b === region) &&
      l.a !== obs.player!.region && l.b !== obs.player!.region);
  }

  // ---------------------------------------------------------------------------
  // Pacing: noul/score/choice -> spawn / item / relax
  // ---------------------------------------------------------------------------

  composePacing(obs: Observation, answers: Record<string, JevAnswer> | null, difficulty: number): Emit {
    const out: Emit = { acts: [], spawns: [], items: [], relax: false };
    const stressed = DecisionComposer.playerStressed(obs);

    // Safety first: critical stress -> mercy item, never a horde. This runs even
    // with no Jev answers (it is the rule director's own gate, kept deterministic).
    if (stressed) {
      this.st.consecutiveSpawnBlocks++;
      if (obs.player && (obs.player.health ?? 100) < 45) out.items.push('medkit');
      if ((obs.director?.ammo_pct ?? 50) < 25) out.items.push('ammo');
      return out;
    }
    this.st.consecutiveSpawnBlocks = 0;

    const horde = asNoul(answers?.horde_now);
    const size = asScore(answers?.horde_size);
    const species = asChoice(answers?.spawn_species);
    if (!horde) return out;

    if (horde.noul >= 0.55) {
      let count = 0;
      if (size) count = Math.round(Math.min(4, Math.max(1, size.score)));
      else count = 2;

      let type: SpawnType = this.st.lastSpecies;
      const picked = species?.choice as SpawnType | undefined;
      if (picked && SPAWN_TYPES.includes(picked)) type = picked;

      // Baron floor: a miniboss is only allowed at real difficulty.
      if (type === 'baron' && difficulty < 4) type = 'knight';

      out.spawns.push({ type, count: Math.min(this.cfg.composer.maxSpawnCount, count) });
      this.st.lastSpecies = type;
    } else if (horde.noul <= 0.2) {
      // Model says ease off: formalize with a relax (also feeds the watchdog).
      out.relax = true;
    }
    return out;
  }

  // ---------------------------------------------------------------------------
  // Buddy
  // ---------------------------------------------------------------------------

  composeBuddy(obs: Observation, answers: Record<string, JevAnswer> | null): Emit {
    const out: Emit = { acts: [], spawns: [], items: [], relax: false };
    if (!obs.buddy) return out;
    const now = Date.now();
    if (now - this.st.lastBuddyOrderAt < this.cfg.composer.orderHoldMs.buddy) return out;

    const stance = asChoice(answers?.buddy_stance);
    if (!stance) return out;
    const order = stance.choice as BuddyOrder;
    const valid: BuddyOrder[] = ['engage', 'defend', 'hold', 'regroup', 'retreat', 'goto', 'grab'];
    if (!valid.includes(order)) return out;

    out.buddy = { order, forTics: 105 };
    this.st.lastBuddyOrderAt = now;
    return out;
  }

  // ---------------------------------------------------------------------------
  // Adaptation -> difficulty delta (the actual number stays bounded in learning.ts)
  // ---------------------------------------------------------------------------

  composeAdaptation(answers: Record<string, JevAnswer> | null):
    { frustration: number; boredom: number; outpacing: number; strongest: string } | null {
    if (!answers) return null;
    const fr = asNoul(answers.frustration_risk);
    const bo = asNoul(answers.boredom_risk);
    const op = asNoul(answers.skill_is_outpacing_difficulty);
    const st = asChoice(answers.strongest_axis);
    if (!fr || !bo || !op) return null;
    return { frustration: fr.noul, boredom: bo.noul, outpacing: op.noul, strongest: st?.choice ?? 'unknown' };
  }

  // ---------------------------------------------------------------------------
  // Nemesis death judgment -> clamped deltas for `nemesis propose`
  // ---------------------------------------------------------------------------

  composeNemesis(
    type: string,
    answers: Record<string, JevAnswer> | null,
  ): string[] {
    if (!answers) return [];
    const shift = asScore(answers.weight_shift);
    const conf = asNoul(answers.pattern_confident);
    const bias = asChoice(answers.bias_direction);
    const deltas: string[] = [];

    if (shift && conf && conf.noul > 0.6) {
      // Score levels: 0 hold, 1 nudge (-0.1), 2 clear (-0.25), 3 strong (-0.5).
      const mag = shift.score >= 2.5 ? 0.5 : shift.score >= 1.5 ? 0.25 : shift.score >= 0.5 ? 0.1 : 0;
      if (mag > 0) {
        deltas.push(`propose=${type} tactic_weight=chase:-${mag} tactic_weight=focus_fire:-${mag / 2}`);
      }
    }
    if (bias && bias.confidence > 0.55 && bias.choice !== 'none') {
      deltas.push(`propose=${type} bias=${bias.choice}`);
    }
    return deltas;
  }
}
