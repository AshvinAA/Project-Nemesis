// stateCompiler.test.ts — budget guard + squad grouping.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadConfig } from '../src/config.js';
import { squadsOf, compileSquad, compilePacing, BudgetError } from '../src/stateCompiler.js';
import { LearningTracker } from '../src/learning.js';
import type { Observation } from '../src/types.js';

const cfg = loadConfig('default');
const learning = new LearningTracker(cfg);

test('squads group monsters by region and sort by proximity', () => {
  const obs: Observation = {
    player: { pos: [0, 0], health: 100 },
    monsters: [
      { id: 1, type: 'imp', pos: [1, 1], hp: 60, region: 5, see_player: false, d_player: 800, order: 'none' },
      { id: 2, type: 'pinky', pos: [2, 2], hp: 40, region: 5, see_player: false, d_player: 900, order: 'none' },
      { id: 3, type: 'zombie', pos: [3, 3], hp: 20, region: 3, see_player: true, d_player: 100, order: 'none' },
      { id: 4, type: 'imp', pos: [4, 4], hp: 0, region: 9, see_player: false, d_player: 50, order: 'none' }, // dead
    ],
  };
  const squads = squadsOf(obs);
  assert.equal(squads.length, 2);
  assert.equal(squads[0]!.region, 3);   // nearest squad first
  assert.equal(squads[1]!.members.length, 2);
});

test('digests stay inside the token budget', () => {
  const obs: Observation = {
    player: { pos: [0, 0], health: 80, armor: 40, weapon: 2, angle: 90, region: 1 },
    monsters: Array.from({ length: 40 }, (_, i) => ({
      id: i + 1, type: 'imp', pos: [i, i], hp: 60, region: 2 + (i % 4),
      see_player: true, d_player: 200 + i * 10, order: 'none',
    })),
    nemesis: { version: 1, rows: [{ type: 'imp', tactics: { chase: 1 }, weapon_bias: { shotgun: 0.5 }, deaths: 3, deaths_by_weapon: { shotgun: 3 }, recent: [] }], events: [] },
  };
  const squads = squadsOf(obs);
  const s = compileSquad(obs, squads[0]!, learning, cfg);
  assert.ok(s.bytes <= cfg.budget.maxDigestTokens);
  const p = compilePacing(obs, learning, cfg);
  assert.ok(p.bytes <= cfg.budget.maxDigestTokens);
});

test('budget guard throws when a digest would be absurd', () => {
  const cfgTiny = loadConfig('default');
  cfgTiny.budget.maxDigestTokens = 10;
  const obs: Observation = {
    player: { pos: [0, 0], health: 100 },
    monsters: Array.from({ length: 40 }, (_, i) => ({
      id: i + 1, type: 'imp', pos: [i, i], hp: 60, region: 2, see_player: true, d_player: i, order: 'none',
    })),
  };
  assert.throws(() => compilePacing(obs, learning, cfgTiny), BudgetError);
});
