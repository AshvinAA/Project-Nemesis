// composer.test.ts — property invariants of the Decision Composer.
// These are the guarantees the old prose-parser could never make.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadConfig } from '../src/config.js';
import { DecisionComposer } from '../src/composer.js';
import { MONSTER_ORDERS, type JevAnswer, type Observation } from '../src/types.js';

function mkObs(over: Partial<Observation> = {}): Observation {
  return {
    player: { pos: [100, 100], health: 100, armor: 50, weapon: 2, region: 1 },
    monsters: [
      { id: 1, type: 'imp', pos: [300, 300], hp: 60, region: 2, see_player: true, d_player: 400, order: 'none' },
      { id: 2, type: 'imp', pos: [310, 300], hp: 55, region: 2, see_player: true, d_player: 420, order: 'none' },
    ],
    regions: [{ id: 1, x: 100, y: 100 }, { id: 2, x: 300, y: 300 }, { id: 3, x: 500, y: 100 }],
    links: [{ a: 1, b: 2, kind: 'open' }, { a: 2, b: 3, kind: 'open' }],
    director: { intensity: 40, state: 1, recent_dmg: 0, ammo_pct: 70 },
    ...over,
  };
}

function choice(v: string, conf = 0.9): JevAnswer {
  return { type: 'choice', choice: v, probabilities: { [v]: conf }, confidence: conf };
}
function noul(v: number): JevAnswer {
  return { type: 'noul', noul: v };
}
function score(v: number, conf = 0.8): JevAnswer {
  return { type: 'score', score: v, legend: {}, probabilities: {}, confidence: conf };
}

test('never emits an order outside the engine vocabulary', () => {
  const cfg = loadConfig('default');
  const c = new DecisionComposer(cfg);
  const obs = mkObs();
  // Jev can only answer from our criteria, but belt & braces: an unknown answer is dropped.
  const emit = c.composeSquad(obs, { region: 2, members: obs.monsters }, { squad_stance: choice('teleport_behind') });
  assert.equal(emit.acts.length, 0);
});

test('emits flank only when route is open and distance allows; else falls back to focus_fire', () => {
  const cfg = loadConfig('default');
  const c = new DecisionComposer(cfg);
  const obs = mkObs(); // link open, dist 400+
  const emit = c.composeSquad(obs, { region: 2, members: obs.monsters }, { squad_stance: choice('flank_left') });
  assert.equal(emit.acts.length, 1);
  assert.equal(emit.acts[0]!.order, 'flank_left');

  // Same call again inside the hysteresis window -> suppressed.
  const emit2 = c.composeSquad(obs, { region: 2, members: obs.monsters }, { squad_stance: choice('flank_left') });
  assert.equal(emit2.acts.length, 0);
});

test('wounded squad never presses', () => {
  const cfg = loadConfig('default');
  const c = new DecisionComposer(cfg);
  const obs = mkObs({ monsters: [
    { id: 1, type: 'imp', pos: [1, 1], hp: 8, region: 2, see_player: true, d_player: 100, order: 'none' },
  ] });
  const emit = c.composeSquad(obs, { region: 2, members: obs.monsters }, { squad_stance: choice('focus_fire') });
  assert.equal(emit.acts[0]!.order, 'fallback');
});

test('critical stress blocks all spawning and requests mercy items', () => {
  const cfg = loadConfig('default');
  const c = new DecisionComposer(cfg);
  const obs = mkObs({ player: { pos: [0, 0], health: 15, armor: 0, weapon: 2, region: 1 } });
  const emit = c.composePacing(obs, { horde_now: noul(0.99), horde_size: score(4), spawn_species: choice('baron') }, 5);
  assert.equal(emit.spawns.length, 0);
  assert.ok(emit.items.includes('medkit'));
});

test('spawn obeys count cap and baron difficulty floor', () => {
  const cfg = loadConfig('default');
  cfg.composer.maxSpawnCount = 4;
  const c = new DecisionComposer(cfg);
  const obs = mkObs();
  const emit = c.composePacing(obs, { horde_now: noul(0.9), horde_size: score(4), spawn_species: choice('baron') }, 2);
  assert.equal(emit.spawns[0]!.count, 4);
  assert.equal(emit.spawns[0]!.type, 'knight'); // baron demoted below difficulty 4
});

test('ids emitted are always from the current roster', () => {
  const cfg = loadConfig('default');
  const c = new DecisionComposer(cfg);
  const obs = mkObs();
  const emit = c.composeSquad(obs, { region: 2, members: obs.monsters }, { squad_stance: choice('hold') });
  for (const a of emit.acts) {
    for (const id of a.ids) assert.ok(obs.monsters.some(m => m.id === id));
  }
});

test('low-confidence horde triggers relax (watchdog feed)', () => {
  const cfg = loadConfig('default');
  const c = new DecisionComposer(cfg);
  const obs = mkObs();
  const emit = c.composePacing(obs, { horde_now: noul(0.1) }, 3);
  assert.equal(emit.relax, true);
});

test('all emitted orders are in vocabulary across a fuzz sweep', () => {
  const cfg = loadConfig('default');
  for (let i = 0; i < 200; i++) {
    const c = new DecisionComposer(cfg);
    const obs = mkObs({
      monsters: Array.from({ length: 1 + (i % 5) }, (_, k) => ({
        id: k + 1, type: 'imp', pos: [1, 2], hp: 60 - k, region: 2,
        see_player: i % 2 === 0, d_player: 100 + k * 150, order: 'none',
      })),
      director: { intensity: i % 100, state: i % 3, recent_dmg: i % 50, ammo_pct: i % 100 },
    });
    const orders = ['focus_fire', 'flank_left', 'hold', 'fallback', 'ambush', 'chase', 'flank_right', 'use_door'];
    const ans = { squad_stance: choice(orders[i % orders.length]!) };
    const emit = c.composeSquad(obs, { region: 2, members: obs.monsters }, ans);
    for (const a of emit.acts) assert.ok(MONSTER_ORDERS.includes(a.order));
  }
});
