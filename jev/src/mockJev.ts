// mockJev.ts — deterministic offline Jev stand-in (--mock). Answers from the
// same state digests the real API would see, so the FULL pipeline (digest ->
// answers -> composer -> protocol lines) runs without a key or network.
// Used for tests, demos, and CI.

import type { JevRequest, JevResponse } from './types.js';

export function mockAnswer(purpose: string, req: JevRequest): JevResponse {
  const state = (req.state ?? {}) as Record<string, unknown>;
  const answers: JevResponse['answers'] = {};
  const conf = 0.85;

  for (const [key, q] of Object.entries(req.questions)) {
    if (q.type === 'noul') {
      // horde_now: yes when player healthy; boredom/frustration from stall/deaths.
      let v = 0.5;
      if (key === 'horde_now') v = (state as { player?: { hp?: number } })?.player?.hp !== undefined && (state as { player?: { hp?: number } }).player!.hp! > 60 ? 0.8 : 0.2;
      else if (key === 'boredom_risk') v = (state as { fun?: { stall_seconds?: number } })?.fun?.stall_seconds !== undefined && (state as { fun?: { stall_seconds?: number } }).fun!.stall_seconds! > 15 ? 0.8 : 0.2;
      else if (key === 'frustration_risk') v = (state as { fun?: { deaths_last_5min?: number } })?.fun?.deaths_last_5min !== undefined && (state as { fun?: { deaths_last_5min?: number } }).fun!.deaths_last_5min! >= 2 ? 0.8 : 0.2;
      else if (key === 'skill_is_outpacing_difficulty') v = 0.7;
      else if (key === 'buddy_in_danger') v = 0.2;
      else if (key === 'pattern_confident') v = 0.75;
      answers[key] = { type: 'noul', noul: v };
    } else if (q.type === 'choice') {
      let pick = Object.keys(q.criteria)[0] ?? 'none';
      if (key === 'squad_stance') pick = 'flank_left';
      else if (key === 'phase') pick = 'buildup';
      else if (key === 'spawn_species') pick = 'imp';
      else if (key === 'buddy_stance') pick = 'engage';
      else if (key === 'strongest_axis') pick = 'aim';
      else if (key === 'bias_direction') pick = 'unpredictability';
      answers[key] = { type: 'choice', choice: pick, probabilities: { [pick]: conf }, confidence: conf };
    } else {
      let v = 1;
      if (key === 'horde_size') v = (state as { player?: { hp?: number } })?.player?.hp !== undefined && (state as { player?: { hp?: number } }).player!.hp! > 60 ? 2.5 : 1;
      else if (key === 'weight_shift') v = 2.2;
      answers[key] = { type: 'score', score: v, legend: {}, probabilities: {}, confidence: conf };
    }
  }

  return { model: 'mock-1.0', answers, usage: { input_tokens: 0, output_tokens: 0 } };
}
