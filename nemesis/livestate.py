"""Phase 9: live telemetry for the dashboard.

Two outputs, both crash-safe:
  * live_state.json  — full snapshot, rewritten ~4 Hz (STATE_WRITE_EVERY).
                       Written via `.tmp` + os.replace so a reader never sees
                       a half file even when the agent dies mid-write.
  * state_history.jsonl — one compact row per second (HISTORY_APPEND_EVERY).
                       The dashboard keeps the tail in RAM for sparklines; the
                       file is the durable copy.

The dashboard never blocks the agent loop: update() just swaps an immutable
dict under the GIL and does throttled I/O.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from typing import Any, Optional

from . import config


def _atomic_write_json(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmp, path)


class LiveState:
    """Rolling snapshot + history for the weight-evolution dashboard."""

    HISTORY_MAX = 900  # ~15 min of 1 Hz rows kept in RAM

    def __init__(self) -> None:
        self.snapshot: dict[str, Any] = {}
        self.history: deque[dict] = deque(maxlen=self.HISTORY_MAX)
        self.events: deque[dict] = deque(maxlen=40)   # ticker feed
        self._last_write = 0.0
        self._last_hist = 0.0
        os.makedirs(os.path.dirname(config.STATE_HISTORY_PATH) or ".",
                    exist_ok=True)

    # -- event ticker -------------------------------------------------------

    def log_event(self, text: str) -> None:
        self.events.append({"ts": round(time.time(), 3), "text": str(text)[:120]})

    # -- per-poll update ------------------------------------------------------

    def update(self, learner, obs: Optional[dict], curriculum=None) -> None:
        now_m = time.monotonic()
        nem = obs.get("nemesis", {}) if obs else {}
        player = obs.get("player", {}) if obs else {}
        eps = (learner.epsilon_override if learner.epsilon_override is not None
               else learner.q.epsilon())
        skill = nem.get("buddy_skill")
        skill = skill if isinstance(skill, int) else curriculum.level if curriculum else 0
        levels = config.SKILL_LEVELS
        snap = {
            "ts": round(time.time(), 3),
            "tic": obs.get("tic") if obs else None,
            "episode": learner.q.episode,
            "episode_label": learner.episode_label(),
            "alive": learner.alive,
            "epsilon": round(eps, 4),
            "buddy_skill": skill,
            "buddy_skill_name": levels[skill] if isinstance(skill, int)
                                and 0 <= skill < len(levels) else "?",
            "last_terminal_r": learner.rewards_log[-1] if learner.rewards_log else 0.0,
            "surv_avg": round(learner._surv_avg(), 2),
            "player_hp": player.get("health"),
            "player_pos": player.get("pos"),
            "nemesis_hp": learner.prev_hp,
            "last_action": learner.last_action_name,
            "rows": nem.get("rows", []),
            "events": list(nem.get("events", [])),
            "agent_events": list(self.events),
            "pushes": getattr(curriculum, "pushed", 0) if curriculum else 0,
            "curriculum_level": getattr(curriculum, "level", None) if curriculum else None,
            "curriculum_error": getattr(curriculum, "last_error", None) if curriculum else None,
        }
        self.snapshot = snap

        if now_m - self._last_write >= config.STATE_WRITE_EVERY:
            self._last_write = now_m
            try:
                _atomic_write_json(config.LIVE_STATE_PATH, snap)
            except OSError as e:
                print(f"[livestate] snapshot write failed: {e}")

        if now_m - self._last_hist >= config.HISTORY_APPEND_EVERY:
            self._last_hist = now_m
            # Compact row: scalars + the learned weights (that's the point of
            # the dashboard: watch jev's weights drift episode by episode).
            wt = {}
            for row in nem.get("rows", []):
                if isinstance(row, dict) and row.get("type"):
                    wt[str(row["type"])] = {
                        "deaths": row.get("deaths", 0),
                        "tactics": row.get("tactics", {}),
                        "weapon_bias": row.get("weapon_bias", {}),
                    }
            row_h = {"ts": snap["ts"], "ep": snap["episode"],
                     "eps": snap["epsilon"], "skill": snap["buddy_skill"],
                     "r": snap["last_terminal_r"], "surv": snap["surv_avg"],
                     "hp": snap["player_hp"], "act": snap["last_action"],
                     "wt": wt}
            self.history.append(row_h)
            try:
                with open(config.STATE_HISTORY_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row_h, separators=(",", ":")) + "\n")
            except OSError as e:
                print(f"[livestate] history append failed: {e}")
