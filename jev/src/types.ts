// types.ts — shared types for the JEV bridge.
// Field names mirror BuddyDoom's AI_Serialize() output (files/p_ai_llm.c)
// and the documented observation shapes in docs/MONSTER_AGENT_GUIDE.md.

export interface ObsPlayer {
  pos: number[];        // [x, y] or [x, y, z] map units
  angle?: number;       // degrees 0..359
  health?: number;
  armor?: number;
  weapon?: number;      // weapon index 0..8
  region?: number;
  dead?: boolean;
}

export interface ObsMonster {
  id: number;           // registry slot + 1 — refresh from EVERY observation
  type: string;         // AI_TypeName(): "zombie" | "imp" | "pinky" | ...
  pos: number[];        // [x, y]
  hp: number;
  region: number;
  see_player: boolean;
  see_buddy?: boolean;
  d_player: number;
  d_buddy?: number;
  order: string;        // current directive, AI_OrderName()
}

export interface ObsBuddy {
  pos: number[];
  health: number;
  armor?: number;
  weapon?: number;
  ammo?: number;
  state?: string;       // follow|fight|heal|hold|come|grab
  region?: number;
  d_player?: number;
  route?: number[][];
}

export interface ObsRegion { id: number; x: number; y: number; }
export type LinkKind = 'open' | 'door' | 'locked';
export interface ObsLink { a: number; b: number; kind: LinkKind; }

export interface ObsDirector {
  intensity: number;    // 0..100
  state: number;        // 0 buildup, 1 sustain, 2 fade
  recent_dmg: number;
  ammo_pct: number;
}

// Nemesis block — appended by our files/p_nemesis.c extension to AI_Serialize().
export interface NemesisRow {
  type: string;
  tactics: Record<string, number>;   // order name -> weight
  weapon_bias: Record<string, number>; // player weapon name -> bias
  deaths: number;
  deaths_by_weapon: Record<string, number>;
  recent: string[];                  // recent event labels
}

export interface NemesisState {
  version: number;
  rows: NemesisRow[];
  events: string[];                  // recent ground-truth event labels
}

export interface Observation {
  tic?: number;
  player: ObsPlayer | null;
  buddy?: ObsBuddy | null;
  monsters: ObsMonster[];
  count?: number;
  regions?: ObsRegion[];
  links?: ObsLink[];
  director?: ObsDirector;
  nemesis?: NemesisState;
  nolevel?: boolean;
}

// ---------------------------------------------------------------------------
// Jev API shapes (docs.typesafe.ai/api) — three question primitives.
// ---------------------------------------------------------------------------

export type NoulQuestion = {
  type: 'noul';
  instructions: string;
  criteria?: { true?: string; false?: string };
};
export type ChoiceQuestion = {
  type: 'choice';
  instructions: string;
  criteria: Record<string, string | null>;
};
export type ScoreQuestion = {
  type: 'score';
  instructions: string;
  criteria: string[];   // 2..10 level descriptions, ordered low -> high
};
export type JevQuestion = NoulQuestion | ChoiceQuestion | ScoreQuestion;

export interface JevRequest {
  state: unknown;
  model: string;
  questions: Record<string, JevQuestion>;
}

export interface NoulAnswer  { type: 'noul'; noul: number; }
export interface ChoiceAnswer {
  type: 'choice';
  choice: string;
  probabilities: Record<string, number>;
  confidence: number;
}
export interface ScoreAnswer {
  type: 'score';
  score: number;
  legend: Record<string, string>;
  probabilities: Record<string, number>;
  confidence: number;
}
export type JevAnswer = NoulAnswer | ChoiceAnswer | ScoreAnswer;

export interface JevResponse {
  model: string;
  answers: Record<string, JevAnswer>;
  usage: { input_tokens: number; output_tokens: number };
}

// Decision Composer outputs — exact wire vocabulary, type-checked at compile time.
export const MONSTER_ORDERS = [
  'chase', 'hold', 'fallback', 'flank_left', 'flank_right',
  'focus_fire', 'ambush', 'use_door',
] as const;
export type MonsterOrder = (typeof MONSTER_ORDERS)[number];

export const BUDDY_ORDERS = [
  'engage', 'defend', 'hold', 'regroup', 'retreat', 'goto', 'grab',
] as const;
export type BuddyOrder = (typeof BUDDY_ORDERS)[number];

// P_Director_TypeByName-accepted names (files/p_ai_director.c).
export const SPAWN_TYPES = [
  'zombie', 'shotgun', 'chaingun', 'imp', 'pinky', 'spectre', 'lost',
  'caco', 'pain', 'knight', 'baron', 'revenant', 'mancubus', 'arachnotron',
] as const;
export type SpawnType = (typeof SPAWN_TYPES)[number];

export interface PendingLine { line: string; kind: 'act' | 'buddy' | 'spawn' | 'item' | 'relax' | 'nemesis'; }

// Vanilla spawnhealth per AI_TypeName() name — lets the composer reason about
// wounded-ness from the raw hp the serializer reports.
export const SPAWNHEALTH: Record<string, number> = {
  zombie: 20, shotgun: 30, chaingun: 70, imp: 60, pinky: 150, spectre: 150,
  lost: 100, caco: 400, pain: 400, knight: 200, baron: 1000, revenant: 250,
  mancubus: 600, arachnotron: 250, monster: 60,
};
export function hpFraction(type: string, hp: number): number {
  const max = SPAWNHEALTH[type] ?? 60;
  return Math.max(0, Math.min(1, hp / max));
}
