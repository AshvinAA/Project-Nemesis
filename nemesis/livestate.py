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
import re
import time
from collections import deque
from typing import Any, Optional

from . import config

# Event labels from the engine ring: "<seq>:hit:<type>:<weapon>:<dmg>",
# "<seq>:kill:<type>:<weapon>", "<seq>:rankup:buddy:<lvl>", "<seq>:eased:buddy:<lvl>".
_EV_RE = re.compile(r"^(\d+):(hit|kill|rankup|eased):(.+)$")


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
        # Phase 9.16: session scoreboard (what a normal person reads first).
        self.stats = {"kills": 0, "hits": 0, "dmg": 0,
                      "rankups": 0, "eased": 0, "pdeaths": 0}
        self._ev_seq = -1          # last processed event ring seq (-1 = armed)
        self._attached = False     # seen the first nemesis block with events?
        self._last_php: Optional[int] = None
        self._t0: Optional[float] = None
        os.makedirs(os.path.dirname(config.STATE_HISTORY_PATH) or ".",
                    exist_ok=True)

    def _absorb_events(self, nem: dict) -> None:
        """Fold new engine events into the scoreboard. The ring arrives
        newest-first with unique seqs. The FIRST sight of the block arms the
        counter: if the ring already has events they are backlog from before
        we attached (skip them); an empty ring arms at -1 so everything that
        happens AFTER attachment counts."""
        if "events" not in nem:
            return                       # block omitted this snapshot
        parsed = []
        for lab in nem.get("events") or []:
            m = _EV_RE.match(str(lab))
            if m:
                parsed.append((int(m.group(1)), m.group(2), m.group(3)))
        if not self._attached:
            self._attached = True
            if not parsed:
                return                   # armed: everything from now counts
            self._ev_seq = max(parsed[-1][0], self._ev_seq)   # skip backlog
        parsed.sort(key=lambda e: e[0])                 # oldest first
        for seq, kind, rest in parsed:
            if seq <= self._ev_seq:
                continue
            self._ev_seq = max(self._ev_seq, seq)
            if kind == "kill":
                self.stats["kills"] += 1
            elif kind == "hit":
                self.stats["hits"] += 1
                try:
                    self.stats["dmg"] += int(rest.rsplit(":", 1)[1])
                except (IndexError, ValueError):
                    pass
            elif kind == "rankup":
                self.stats["rankups"] += 1
            elif kind == "eased":
                self.stats["eased"] += 1

    # -- event ticker -------------------------------------------------------

    def log_event(self, text: str) -> None:
        self.events.append({"ts": round(time.time(), 3), "text": str(text)[:120]})

    # -- per-poll update ------------------------------------------------------

    def update(self, learner, obs: Optional[dict], curriculum=None) -> None:
        now_m = time.monotonic()
        nem = obs.get("nemesis", {}) if obs else {}
        player = obs.get("player", {}) if obs else {}
        if self._t0 is None:
            self._t0 = now_m
        self._absorb_events(nem)
        php = player.get("health")
        if isinstance(php, int) and isinstance(self._last_php, int) \
                and self._last_php > 0 and php <= 0:
            self.stats["pdeaths"] += 1
        self._last_php = php if isinstance(php, int) else self._last_php
        eps = (learner.epsilon_override if learner.epsilon_override is not None
               else learner.q.epsilon())
        skill = nem.get("buddy_skill")
        skill = skill if isinstance(skill, int) else curriculum.level if curriculum else 0
        levels = config.SKILL_LEVELS
        xp = nem.get("buddy_xp")
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
            "buddy_xp": xp if isinstance(xp, int) and 0 <= xp < config.BUDDY_XP_PER_RANK else 0,
            "buddy_xp_next": config.BUDDY_XP_PER_RANK,
            "player_weapon": player.get("weapon"),
            "stats": dict(self.stats),
            "session_secs": round(now_m - self._t0, 1) if self._t0 else 0.0,
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
