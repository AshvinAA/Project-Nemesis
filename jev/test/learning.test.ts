// learning.test.ts — the difficulty controller's boundedness and gating.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadConfig } from '../src/config.js';
import { LearningTracker } from '../src/learning.js';

test('difficulty rises only with evidence and dominance', () => {
  const cfg = loadConfig('default');
  const l = new LearningTracker(cfg);
  // No evidence -> adaptation is refused even with confident answers.
  const r1 = l.applyAdaptation({ outpacing: 0.99, boredom: 0.0, frustration: 0.0 });
  assert.equal(r1.stepped, false);
  assert.equal(r1.reason, 'low-evidence');

  // Give evidence.
  for (let i = 0; i < 30; i++) l.tick({ player: { pos: [0, 0] }, monsters: [], director: { intensity: 50, state: 1, recent_dmg: 5, ammo_pct: 80 } });
  const r2 = l.applyAdaptation({ outpacing: 0.9, boredom: 0.1, frustration: 0.1 });
  assert.equal(r2.stepped, true);
  assert.equal(r2.reason, 'dominance');
  assert.equal(r2.to, 2.25); // 2 + 0.25
});

test('frustration always wins and steps down', () => {
  const cfg = loadConfig('default');
  const l = new LearningTracker(cfg);
  for (let i = 0; i < 30; i++) l.tick({ player: { pos: [0, 0] }, monsters: [], director: { intensity: 50, state: 1, recent_dmg: 5, ammo_pct: 80 } });
  const r = l.applyAdaptation({ outpacing: 0.9, boredom: 0.9, frustration: 0.8 });
  assert.equal(r.reason, 'frustration');
  assert.ok(r.to < r.from);
});

test('difficulty respects the ceiling and floor', () => {
  const cfg = loadConfig('default');
  const l = new LearningTracker(cfg);
  for (let i = 0; i < 30; i++) l.tick({ player: { pos: [0, 0] }, monsters: [], director: { intensity: 50, state: 1, recent_dmg: 5, ammo_pct: 80 } });
  for (let i = 0; i < 40; i++) {
    l.applyAdaptation({ outpacing: 0.95, boredom: 0, frustration: 0 });
  }
  assert.ok(l.difficulty <= cfg.learning.difficultyCeiling);

  const l2 = new LearningTracker(cfg);
  for (let i = 0; i < 30; i++) l2.tick({ player: { pos: [0, 0] }, monsters: [], director: { intensity: 50, state: 1, recent_dmg: 5, ammo_pct: 80 } });
  for (let i = 0; i < 40; i++) {
    l2.applyAdaptation({ outpacing: 0.1, boredom: 0, frustration: 0.95 });
  }
  assert.ok(l2.difficulty >= cfg.learning.difficultyFloor);
});

test('EWMA skill values stay within [0,1]', () => {
  const cfg = loadConfig('default');
  const l = new LearningTracker(cfg);
  for (let i = 0; i < 500; i++) {
    l.ingestNemesis({ version: 1, rows: [], events: [`${i}:hit:imp:shotgun:200`] });
    l.tick({ player: { pos: [0, 0] }, monsters: [], director: { intensity: 50, state: 1, recent_dmg: 60, ammo_pct: 10 } });
  }
  for (const v of Object.values(l.skill)) assert.ok(v >= 0 && v <= 1, `out of range: ${v}`);
});
