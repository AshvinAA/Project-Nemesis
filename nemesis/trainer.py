#!/usr/bin/env python3
"""Offline trainer (Phase 3): proves the Q-learning machinery converges
BEFORE any live training.

A synthetic duel world mimics the real loop's granularity (10 Hz polls,
engine-shaped rewards, terminal on death). It is deliberately crude physics —
the point is to validate update math, schedules, terminal handling and
reward accounting, not to predict DOOM.

    python -m nemesis.trainer [--episodes 60]

Exit 1 unless: last-5 mean episode reward > first-5 mean, and the trained
greedy policy beats the epsilon=1.0 random policy on held-out episodes.
"""

from __future__ import annotations

import argparse
import random

from . import config
from .qtable import QTable, N_ACTIONS
from .state import StateVector
from .rewards import (R_COVER_WITH_LOS, R_HIT_NO_COVER, R_IDLE_OPEN_LOS,
                      R_SHOT_LANDED, episode_terminal)

MAX_POLLS = 150          # ~15 s duel cap (10 Hz polls)
SHOT_DMG = (5, 20)       # player damage to nemesis per landed shot
NEM_DPS_CHANCE = 0.10    # per-poll chance the nemesis's shotgun lands


def _rand_state(rng: random.Random) -> StateVector:
    return StateVector(rng.randrange(3), rng.randrange(2), rng.randrange(4),
                       rng.randrange(4), 2, 0)


def _apply_action(s: StateVector, a: int, rng: random.Random) -> StateVector:
    dist, los, angle, cover, hp, hit = (s.dist, s.los, s.angle, s.cover, s.hp, s.hit_recent)
    if a == 0:      # advance
        dist = max(0, dist - 1)
        los = 1 if rng.random() < 0.8 else los
    elif a == 1:    # retreat
        dist = min(2, dist + 1)
        los = 1 if rng.random() < 0.5 else 0
    elif a in (2, 3):   # strafe
        angle = rng.choice((1, 2))
        if rng.random() < 0.3:
            cover = rng.choice((1, 2))
    elif a == 4:    # take_cover: works better at range
        if rng.random() < (0.75 if dist > 0 else 0.4):
            cover = rng.choice((1, 2))
    elif a == 5:    # stand_and_shoot: out in the open, trading
        cover = 0
        los = 1
    else:           # flank: close in from the side, exposed
        angle = 1 if a == 6 else 2
        dist = max(0, dist - 1)
        los = 1
        cover = 0
    return StateVector(dist, los, angle, cover, hp, hit)


def _player_shot(s: StateVector, rng: random.Random) -> int:
    """Damage the player deals the nemesis this poll (0 = miss)."""
    if s.los == 0:
        return 0
    p = 0.5 if s.dist == 0 else (0.3 if s.dist == 1 else 0.15)
    if s.cover:
        p *= 0.35
    return rng.randint(*SHOT_DMG) if rng.random() < p else 0


def _shaping(s: StateVector, a: int, took_hit: bool, rng: random.Random) -> float:
    r = 0.0
    dealt = NEM_DPS_CHANCE * (1.5 if a == 5 else 1.0)
    if s.los and rng.random() < dealt:
        r += R_SHOT_LANDED * rng.randint(3, 8)
    if s.cover and s.los:
        r += R_COVER_WITH_LOS
    if took_hit and s.los and not s.cover:
        r += R_HIT_NO_COVER
    if a == 5 and s.los and not s.cover and rng.random() < 0.5:
        r += R_IDLE_OPEN_LOS
    return r


def run_episode(q: QTable, rng: random.Random, train: bool,
                epsilon: float) -> tuple[float, int]:
    """One synthetic episode. Returns (total reward, polls survived)."""
    s = _rand_state(rng)
    hp_real = 30
    total_r = 0.0
    polls = 0
    s_idx = s.index()
    a = q.select(s_idx, rng, epsilon) if (train or True) else q.greedy(s_idx)
    for _ in range(MAX_POLLS):
        polls += 1
        s2 = _apply_action(s, a, rng)
        dmg = _player_shot(s2, rng)
        hp_real -= dmg
        dead = hp_real <= 0
        hp_bracket = 2 if hp_real > 20 else (1 if hp_real > 10 else 0)
        s2 = StateVector(s2.dist, s2.los, s2.angle, s2.cover, hp_bracket,
                         1 if dmg else 0)
        r = _shaping(s2, a, bool(dmg), rng)
        s2_idx = s2.index()
        if dead:
            r += episode_terminal(nemesis_died=True, player_died=False,
                                  player_dmg_taken=0.0,
                                  survival_seconds=polls / 10.0)
            q.update(s_idx, a, r, None) if train else None
            total_r += r
            return total_r, polls
        q.update(s_idx, a, r, s2_idx) if train else None
        total_r += r
        s, s_idx = s2, s2_idx
        a = q.select(s_idx, rng, epsilon)
    return total_r, polls


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", type=int, default=60)
    args = ap.parse_args()
    rng = random.Random(1993)   # deterministic runs

    q = QTable(":memory:", map_id="synthetic")
    curve = []
    n = max(10, args.episodes)
    for ep in range(n):
        q.episode = ep  # drives the alpha/epsilon decay schedules
        r, polls = run_episode(q, rng, train=True, epsilon=q.epsilon())
        curve.append((r, polls))

    def mean(xs):
        return sum(xs) / len(xs)

    head = mean([c[0] for c in curve[:5]])
    tail = mean([c[0] for c in curve[-5:]])
    surv_head = mean([c[1] for c in curve[:5]])
    surv_tail = mean([c[1] for c in curve[-5:]])

    # held-out evaluation: greedy trained vs pure random
    q.episode = 10**9  # epsilon floor
    eval_rng = random.Random(7)
    greedy_r = mean([run_episode(q, eval_rng, train=False, epsilon=0.0)[0] for _ in range(30)])
    random_r = mean([run_episode(QTable(":memory:"), eval_rng, train=True, epsilon=1.0)[0]
                     for _ in range(30)])

    print("offline trainer — synthetic duels, %d episodes" % n)
    print("  reward/episode : first5=%.1f  last5=%.1f  (delta %+.1f)" % (head, tail, tail - head))
    print("  polls survived : first5=%.1f  last5=%.1f" % (surv_head, surv_tail))
    print("  held-out       : greedy=%.1f  random=%.1f  (delta %+.1f)"
          % (greedy_r, random_r, greedy_r - random_r))
    print("  schedules      : alpha %.2f->%.2f  epsilon %.2f->%.2f"
          % (q.alpha(), config.ALPHA_FLOOR, q.epsilon(), config.EPSILON_FLOOR))

    ok = tail > head and greedy_r > random_r and surv_tail >= surv_head
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
