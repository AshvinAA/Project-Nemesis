"""Tabular Q-learning: 576 states x 8 actions, with the brief's update rule
and decaying alpha/epsilon schedules.

    Q(s,a) += alpha * (r + gamma * max_a' Q(s',a') - Q(s,a))
    (terminal transitions omit the max term)

Persistence: JSON with a versioned header (map id pinned — see PLAN D6).
"""

from __future__ import annotations

import json
import os
import random
from typing import Optional

from . import config
from .state import STATE_COUNT

ACTIONS = ("advance", "retreat", "strafe_left", "strafe_right",
           "take_cover", "stand_and_shoot", "flank_left", "flank_right")
N_ACTIONS = len(ACTIONS)
ACTION_INDEX = {a: i for i, a in enumerate(ACTIONS)}


class QTable:
    def __init__(self, qtable_path: str = config.QTABLE_PATH,
                 map_id: str = "unknown") -> None:
        self.path = qtable_path
        self.map_id = map_id
        self.q = [[0.0] * N_ACTIONS for _ in range(STATE_COUNT)]
        self.episode = 0

    # -- schedules ------------------------------------------------------------

    def alpha(self) -> float:
        """0.3 -> 0.1 floor, linear over EPSILON_DECAY_EPISODES."""
        frac = min(1.0, self.episode / max(1, config.EPSILON_DECAY_EPISODES))
        return config.ALPHA_START + (config.ALPHA_FLOOR - config.ALPHA_START) * frac

    def epsilon(self) -> float:
        """0.2 -> 0.05 floor, linear over EPSILON_DECAY_EPISODES."""
        frac = min(1.0, self.episode / max(1, config.EPSILON_DECAY_EPISODES))
        return config.EPSILON_START + (config.EPSILON_FLOOR - config.EPSILON_START) * frac

    # -- action selection -------------------------------------------------------

    def select(self, s: int, rng: Optional[random.Random] = None,
               epsilon: Optional[float] = None) -> int:
        """epsilon-greedy over Q[s]."""
        rng = rng or random
        eps = self.epsilon() if epsilon is None else epsilon
        if rng.random() < eps:
            return rng.randrange(N_ACTIONS)
        row = self.q[s]
        best = max(row)
        besties = [i for i, v in enumerate(row) if v == best]
        return rng.choice(besties)   # tie-break randomly, not by index bias

    def greedy(self, s: int) -> int:
        row = self.q[s]
        return row.index(max(row))

    # -- update ------------------------------------------------------------------

    def update(self, s: int, a: int, r: float, s2: Optional[int],
               alpha: Optional[float] = None, gamma: Optional[float] = None) -> float:
        """One Q-step. s2=None marks a terminal transition (no bootstrap).
        Returns the td-error (for logging/curiosity)."""
        alpha = self.alpha() if alpha is None else alpha
        gamma = config.GAMMA if gamma is None else gamma
        target = r if s2 is None else r + gamma * max(self.q[s2])
        td = target - self.q[s][a]
        self.q[s][a] += alpha * td
        return td

    # -- persistence ---------------------------------------------------------------

    def save(self, path: Optional[str] = None) -> None:
        path = path or self.path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        doc = {"version": 1, "map": self.map_id, "episode": self.episode,
               "actions": list(ACTIONS),
               "q": self.q}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"))
        os.replace(tmp, path)

    def load(self, path: Optional[str] = None) -> bool:
        path = path or self.path
        if not os.path.exists(path):
            return False
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        if doc.get("version") != 1:
            return False
        q = doc.get("q")
        if not isinstance(q, list) or len(q) != STATE_COUNT:
            return False
        self.q = [[float(v) for v in row] for row in q]
        self.episode = int(doc.get("episode", 0))
        self.map_id = doc.get("map", self.map_id)
        return True
