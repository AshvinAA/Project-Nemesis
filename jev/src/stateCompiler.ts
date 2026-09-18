// stateCompiler.ts — compiles raw observations into per-consumer Jev digests.
// Design rules (workflow §3.1): dense short-key JSON + one narrative line;
// hard token budget; atomic questions only — composition happens in the composer.

import type { BridgeConfig } from './config.js';
import type { JevQuestion, NemesisState, Observation } from './types.js';
import type { LearningState } from './learning.js';

export interface Compiled<Q extends Record<string, JevQuestion>> {
  state: Record<string, unknown>;
  questions: Q;
  bytes: number;
}

const WEAPON_NAMES = ['fist', 'pistol', 'shotgun', 'chaingun', 'rocket', 'plasma', 'bfg', 'chainsaw', 'ssg'];
export function weaponName(idx?: number): string {
  if (idx === undefined || idx < 0 || idx >= WEAPON_NAMES.length) return 'unknown';
  return WEAPON_NAMES[idx]!;
}

export class BudgetError extends Error {}

function estTokens(x: unknown, cfg: BridgeConfig): number {
  return Math.ceil(JSON.stringify(x).length / cfg.budget.charsPerToken);
}

function assertBudget(state: unknown, questions: unknown, cfg: BridgeConfig, tag: string) {
  const t = estTokens({ state, questions }, cfg);
  if (t > cfg.budget.maxDigestTokens) {
    throw new BudgetError(`${tag} digest ~${t} tok > budget ${cfg.budget.maxDigestTokens}`);
  }
  return t;
}

/** Group live monsters into squads by region (shared room = shared squad). */
export function squadsOf(obs: Observation): Array<{ region: number; members: typeof obs.monsters }> {
  const byRegion = new Map<number, typeof obs.monsters>();
  for (const m of obs.monsters) {
    if (m.hp <= 0) continue;
    const arr = byRegion.get(m.region) ?? [];
    arr.push(m);
    byRegion.set(m.region, arr);
  }
  return [...byRegion.entries()]
    .map(([region, members]) => ({ region, members }))
    .sort((a, b) => a.members.reduce((s, m) => s + m.d_player, 0) - b.members.reduce((s, m) => s + m.d_player, 0));
}

// ---------------------------------------------------------------------------
// 1. Squad tactics digest (2–4 Hz)
// ---------------------------------------------------------------------------

export interface SquadQuestions extends Record<string, JevQuestion> {
  squad_stance: { type: 'choice'; instructions: string; criteria: Record<string, string | null> };
  player_vulnerable: { type: 'score'; instructions: string; criteria: string[] };
  fire_discipline: { type: 'score'; instructions: string; criteria: string[] };
}

export function compileSquad(
  obs: Observation, squad: { region: number; members: Observation['monsters'] },
  learning: LearningState, cfg: BridgeConfig,
): Compiled<SquadQuestions> {
  const p = obs.player!;
  const types = [...new Set(squad.members.map(m => m.type))];
  const avgD = Math.round(squad.members.reduce((s, m) => s + m.d_player, 0) / squad.members.length);
  const anySee = squad.members.some(m => m.see_player);
  const nem = obs.nemesis;
  const rows = nem?.rows.filter(r => types.includes(r.type)) ?? [];

  const state = {
    narrative:
      `${squad.members.length} ${types.join('/')} in room ${squad.region}, avg ${avgD}u from player, ` +
      `${anySee ? 'they see the player' : 'no line of sight'}. ` +
      `Player: ${p.health ?? '?'} hp, ${p.armor ?? 0} armor, ${weaponName(p.weapon)}` +
      `${p.angle !== undefined ? `, facing ${p.angle}°` : ''}.`,
    squad: squad.members.map(m => ({
      id: m.id, t: m.type, d: m.d_player, see: m.see_player ? 1 : 0,
      hp: Math.round((m.hp / 60) * 100) / 100, ord: m.order,
    })),
    player: { hp: p.health ?? 0, armor: p.armor ?? 0, w: weaponName(p.weapon) },
    skill: learning.skillSummary(),
    difficulty: Math.round(learning.difficulty * 10) / 10,
    grudge: rows.map(r => ({
      t: r.type, top: Object.entries(r.weapon_bias).sort((a, b) => b[1] - a[1])[0]?.[0],
      deaths: r.deaths,
    })),
  };

  const questions: SquadQuestions = {
    squad_stance: {
      type: 'choice',
      instructions: `Given this squad's position, the player's state and their learned skill level, which single tactic should this squad execute now?`,
      criteria: {
        focus_fire: 'Press the attack directly at the player; good when the player is exposed, low, or out of position',
        flank_left: 'Route around to the player\'s left side through connected rooms; needs distance and open flanks',
        flank_right: 'Route around to the player\'s right side through connected rooms; needs distance and open flanks',
        hold: 'Hold position and shoot from range; good when the squad has cover and the player must come to them',
        fallback: 'Retreat from the player; good when wounded, outnumbered by the player\'s firepower, or baiting',
        ambush: 'Stop advancing and wait near the player\'s likely path; good when the player cannot see the squad',
        chase: 'Default vanilla pursuit; use when nothing else clearly beats it',
      },
    },
    player_vulnerable: {
      type: 'score',
      instructions: 'How exposed is the player to this squad right now?',
      criteria: [
        'Entrenched: full health and armor, cover available, knows where the squad is',
        'Cautious: healthy but exposed or low armor',
        'Hurt: below half health or low on ammo while in the open',
        'Cornered: low health, low ammo, or pinned with no cover',
      ],
    },
    fire_discipline: {
      type: 'score',
      instructions: 'How aggressively should the squad commit to firing versus repositioning this cycle?',
      criteria: [
        'Reposition first: keep moving, fire only on clean shots',
        'Balanced: fire when lined up but keep advancing',
        'Commit: stand and shoot whenever anything is in range',
      ],
    },
  };

  const bytes = assertBudget(state, questions, cfg, 'squad');
  return { state, questions, bytes };
}

// ---------------------------------------------------------------------------
// 2. Pacing digest (1–2 Hz)
// ---------------------------------------------------------------------------

export interface PacingQuestions extends Record<string, JevQuestion> {
  horde_now: { type: 'noul'; instructions: string; criteria: { true: string; false: string } };
  phase: { type: 'choice'; instructions: string; criteria: Record<string, string | null> };
  spawn_species: { type: 'choice'; instructions: string; criteria: Record<string, string | null> };
  horde_size: { type: 'score'; instructions: string; criteria: string[] };
}

export function compilePacing(obs: Observation, learning: LearningState, cfg: BridgeConfig): Compiled<PacingQuestions> {
  const p = obs.player!;
  const d = obs.director;
  const census = obs.monsters.filter(m => m.hp > 0).length;
  const byType: Record<string, number> = {};
  for (const m of obs.monsters) if (m.hp > 0) byType[m.type] = (byType[m.type] ?? 0) + 1;

  const state = {
    narrative:
      `Player has ${p.health ?? 0} hp, ${p.armor ?? 0} armor, ${weaponName(p.weapon)}. ` +
      `Difficulty tier ${Math.round(learning.difficulty * 10) / 10} ` +
      `(skill aim ${learning.skill.aim.toFixed(2)}, movement ${learning.skill.movement.toFixed(2)}). ` +
      `${census} monsters alive. Recent stress: ${d?.recent_dmg ?? 0} dmg burst, ammo ${d?.ammo_pct ?? 0}%.`,
    phase: d?.state ?? 0,
    intensity: d?.intensity ?? 0,
    census,
    byType,
    difficulty: Math.round(learning.difficulty * 10) / 10,
    player: { hp: p.health ?? 0, armor: p.armor ?? 0, ammo_pct: d?.ammo_pct ?? 50 },
    boredom: learning.boredomSignal(obs),
  };

  const questions: PacingQuestions = {
    horde_now: {
      type: 'noul',
      instructions: 'Should the director apply spike pressure (a horde) right now?',
      criteria: {
        true: 'Player is healthy, well armed, and either dominating encounters or approaching the objective',
        false: 'Player is hurt, low on ammo, just survived a spike, or pressure is already high',
      },
    },
    phase: {
      type: 'choice',
      instructions: 'Which pacing phase fits the player\'s current experience?',
      criteria: {
        buildup: 'Tension should rise; light trickle pressure with room to breathe',
        peak: 'Full assault; the player is strong and engaged',
        fade: 'Let the player recover; they recently survived a peak or are struggling',
      },
    },
    spawn_species: {
      type: 'choice',
      instructions: 'If pressure is applied, which monster type best challenges this specific player right now?',
      criteria: {
        zombie: 'Weak hitscan fodder; padding, never a real threat',
        shotgun: 'Close-range hitscan pressure; punishes careless approach',
        chaingun: 'Sustained hitscan; punishes standing in the open',
        imp: 'Projectile damage; punishes predictable strafing',
        pinky: 'Fast melee rush; punishes low mobility or corner camping',
        spectre: 'Near-invisible melee; punishes inattention and rewards panic',
        lost: 'Fast erratic flying; punishes slow tracking aim',
        caco: 'Flying ranged chunky; punishes wasted shots',
        pain: 'Spawns lost souls; punishes crowds and poor target prioritization',
        knight: 'Tanky ranged; punishes low ammo or weak weapons',
        baron: 'Miniboss; only at high difficulty or dominance',
        revenant: 'Fast homing rockets; punishes open ground',
        mancubus: 'Wide spread; punishes corridor crowds',
        arachnotron: 'Sustained plasma; punishes static play',
      },
    },
    horde_size: {
      type: 'score',
      instructions: 'How large should the next spawn wave be, given the player\'s state and difficulty tier?',
      criteria: [
        'Nothing — apply no pressure this cycle',
        'A single monster — light trickle',
        'A pair — measured pressure',
        'A squad — real threat',
        'A horde — peak pressure on a strong player',
      ],
    },
  };

  const bytes = assertBudget(state, questions, cfg, 'pacing');
  return { state, questions, bytes };
}

// ---------------------------------------------------------------------------
// 3. Buddy digest (0.5–1 Hz)
// ---------------------------------------------------------------------------

export interface BuddyQuestions extends Record<string, JevQuestion> {
  buddy_stance: { type: 'choice'; instructions: string; criteria: Record<string, string | null> };
  buddy_in_danger: { type: 'noul'; instructions: string; criteria: { true: string; false: string } };
}

export function compileBuddy(obs: Observation, cfg: BridgeConfig): Compiled<BuddyQuestions> | null {
  const b = obs.buddy;
  if (!b) return null;
  const nearestThreat = Math.min(...obs.monsters.filter(m => m.hp > 0).map(m => m.d_buddy ?? 9999), 9999);

  const state = {
    narrative: `Buddy ${b.health} hp at ${b.d_player ?? '?'}u from player, state ${b.state ?? 'follow'}. ` +
      `Nearest monster to buddy: ${nearestThreat === 9999 ? 'none visible' : nearestThreat + 'u'}.`,
    buddy: { hp: b.health, armor: b.armor ?? 0, ammo: b.ammo ?? -1, state: b.state ?? 'follow', d_player: b.d_player ?? -1 },
    nearest_threat: nearestThreat === 9999 ? null : nearestThreat,
  };
  const questions: BuddyQuestions = {
    buddy_stance: {
      type: 'choice',
      instructions: 'What should the AI buddy do this cycle?',
      criteria: {
        engage: 'Attack nearby threats; default when healthy and enemies are close',
        defend: 'Stay near the player and guard them; good when the player is hurt',
        hold: 'Hold position; useful to anchor a room',
        regroup: 'Move to the player; good when buddy is hurt or far',
        retreat: 'Back off; buddy is critically hurt',
        goto: 'Move to a point; used for flanking support',
        grab: 'Collect nearby items; good when safe and supplies matter',
      },
    },
    buddy_in_danger: {
      type: 'noul',
      instructions: 'Is the buddy in immediate danger of dying?',
      criteria: { true: 'Low health with a threat close and line of sight', false: 'Safe or threats are far' },
    },
  };
  const bytes = assertBudget(state, questions, cfg, 'buddy');
  return { state, questions, bytes };
}

// ---------------------------------------------------------------------------
// 4. Player-model adaptation digest (0.5 Hz + event-triggered)
// ---------------------------------------------------------------------------

export interface AdaptQuestions extends Record<string, JevQuestion> {
  skill_is_outpacing_difficulty: { type: 'noul'; instructions: string; criteria: { true: string; false: string } };
  strongest_axis: { type: 'choice'; instructions: string; criteria: Record<string, string | null> };
  boredom_risk: { type: 'noul'; instructions: string; criteria: { true: string; false: string } };
  frustration_risk: { type: 'noul'; instructions: string; criteria: { true: string; false: string } };
}

export function compileAdapt(obs: Observation, learning: LearningState, cfg: BridgeConfig): Compiled<AdaptQuestions> {
  const s = learning.skill;
  const ev = learning.evidence;

  const state = {
    narrative: `Skill trend: ${learning.trendLine()}. Deaths last 5 min: ${learning.deathsRecent5m}. ` +
      `Difficulty ${Math.round(learning.difficulty * 10) / 10}.`,
    skill: s,
    skill_evidence: ev,
    difficulty: { current: Math.round(learning.difficulty * 10) / 10, floor: cfg.learning.difficultyFloor, ceiling: cfg.learning.difficultyCeiling },
    fun: { stall_seconds: learning.stallSeconds, deaths_last_5min: learning.deathsRecent5m },
  };

  const axisCriteria = (): Record<string, string> => ({
    aim: 'Hits accurately and kills fast',
    movement: 'Dodges projectiles, takes little damage while mobile',
    resource: 'Uses ammo and health items wisely, little waste',
    aggression: 'Initiates fights at good ranges, controls engagement',
    routing: 'Progresses through the level efficiently, little backtracking',
    adaptability: 'Recovers quickly after setbacks and deaths',
  });

  const questions: AdaptQuestions = {
    skill_is_outpacing_difficulty: {
      type: 'noul',
      instructions: 'Is this player\'s demonstrated skill clearly above what the current difficulty tier demands?',
      criteria: {
        true: 'Sustained high aim/movement, few deaths, encounters cleared well under expected time',
        false: 'Skill at or below tier expectations, or evidence is thin',
      },
    },
    strongest_axis: {
      type: 'choice',
      instructions: 'Which skill axis is this player\'s clearest strength right now?',
      criteria: axisCriteria(),
    },
    boredom_risk: {
      type: 'noul',
      instructions: 'Is the player showing boredom/stall patterns?',
      criteria: {
        true: 'Long no-combat stretches, low kill and damage rates, purposeless backtracking',
        false: 'Regular engagement cadence',
      },
    },
    frustration_risk: {
      type: 'noul',
      instructions: 'Is the player showing frustration patterns?',
      criteria: {
        true: 'Multiple deaths at the same spot or to the same monster type, health rarely above 30',
        false: 'Deaths varied and recovery stable',
      },
    },
  };

  const bytes = assertBudget(state, questions, cfg, 'adapt');
  return { state, questions, bytes };
}

// ---------------------------------------------------------------------------
// 5. Nemesis death-judgment digest (event-triggered on monster death)
// ---------------------------------------------------------------------------

export interface NemesisQuestions extends Record<string, JevQuestion> {
  weight_shift: { type: 'score'; instructions: string; criteria: string[] };
  bias_direction: { type: 'choice'; instructions: string; criteria: Record<string, string | null> };
  pattern_confident: { type: 'noul'; instructions: string; criteria: { true: string; false: string } };
}

export function compileNemesis(
  type: string, killingWeapon: string, nem: NemesisState | undefined,
  learning: LearningState, cfg: BridgeConfig,
): Compiled<NemesisQuestions> | null {
  const row = nem?.rows.find(r => r.type === type);
  if (!row) return null;

  const state = {
    narrative: `A ${type} was killed by the player's ${killingWeapon} ` +
      `while executing "${row.recent.at(-1) ?? 'chase'}". Deaths to this type so far: ${row.deaths}.`,
    row: { type: row.type, tactics: row.tactics, weapon_bias: row.weapon_bias, deaths: row.deaths },
    deaths_by_weapon: row.deaths_by_weapon,
    difficulty: Math.round(learning.difficulty * 10) / 10,
    skill_aim: learning.skill.aim,
  };

  const questions: NemesisQuestions = {
    weight_shift: {
      type: 'score',
      instructions: `How much should this monster type's tactic weights shift away from the tactic that just failed?`,
      criteria: [
        'Hold — not enough evidence, weights stay',
        'Nudge — small shift (-0.1) away from the failed tactic',
        'Clear shift (-0.25) — repeated pattern of dying this way',
        'Strong shift (-0.5) — the player reliably exploits this tactic',
      ],
    },
    bias_direction: {
      type: 'choice',
      instructions: 'Which counter-measure should this monster type bias toward against this player?',
      criteria: {
        none: 'No change yet; the current mix is fine',
        range: 'Keep more distance; the player punishes close combat',
        aggression: 'Commit harder and faster; the player is slow to punish aggression',
        unpredictability: 'Mix flanks and ambushes more; the player reads patterns too well',
        cover: 'Use cover and doorways; the player has strong hitscan',
      },
    },
    pattern_confident: {
      type: 'noul',
      instructions: 'Is there enough evidence that this is a real player pattern rather than luck?',
      criteria: {
        true: 'Several deaths in the same pattern across recent encounters',
        false: 'One-off or contradictory evidence',
      },
    },
  };

  const bytes = assertBudget(state, questions, cfg, 'nemesis');
  return { state, questions, bytes };
}
