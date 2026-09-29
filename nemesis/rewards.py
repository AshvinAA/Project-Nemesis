"""Reward model: the brief's spec adapted to event/poll granularity.

Brief spec (kept verbatim where measurable):
    terminal (nemesis death):  -100 + 2*damage_dealt_to_player + 0.1*tics_survived
    terminal (player death):   +150
    shaping: + cover between it and LOS-on-it; + shot landed;
             - damage taken in the open; - standing in open LOS.

Granularity notes (documented deviation, see PLAN D-ledger):
- "damage dealt to the player" is measured as player-hp lost between two
  polls while the nemesis is alive with LOS (engine serializes player.health;
  per-hit attribution to the monster is not available in the observe).
- tics survived = leveltime delta x 35 at death time.
- "shot landed" by the nemesis = player hp drop while nemesis has LOS.
- "took damage" by the nemesis comes from the engine event ring (exact,
  per-hit, with weapon) rather than hp deltas.
- Shaping is emitted per POLL (10 Hz), not per engine tic; the chosen
  directive persists for ~1 s regardless, so the credit assignment is at
  the same granularity as the decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

TICRATE = 35

R_DEATH_BASE = -100.0
R_DMG_TO_PLAYER = 2.0        # per point of player hp the nemesis took off
R_TIC_SURVIVED = 0.1
R_PLAYER_DEATH = 150.0

R_SHOT_LANDED = +1.0         # per player-hp point dropped under nemesis LOS
R_COVER_WITH_LOS = +0.05     # per poll in cover while player still sees it
R_HIT_NO_COVER = -0.05       # per nemesis hp drop while in open LOS
R_IDLE_OPEN_LOS = -0.02      # per poll standing still, visible, no cover


@dataclass
class ShapingAccumulator:
    """Per-episode stateful shaping computer. Feed one poll at a time."""

    last_player_hp: Optional[int] = None
    last_leveltime: Optional[float] = None
    last_pos: Optional[tuple[float, float]] = None
    events_seen: int = 0
    totals: dict = field(default_factory=lambda: {
        "shot_landed_polls": 0, "cover_polls": 0, "hit_open_polls": 0,
        "idle_open_polls": 0, "player_dmg_taken": 0})

    def poll(self, nem: Optional[dict], player: dict, leveltime: Optional[float],
             nem_events_this_poll: int) -> float:
        """nem=None means the nemesis is dead this poll. Returns shaping r."""
        r = 0.0
        if nem is not None:
            # player hp drop since last poll while nemesis sees the player
            hp = player.get("health")
            if (self.last_player_hp is not None and hp is not None
                    and hp < self.last_player_hp and nem.get("see_player")):
                drop = self.last_player_hp - hp
                r += R_SHOT_LANDED * drop
                self.totals["player_dmg_taken"] += drop
                self.totals["shot_landed_polls"] += 1

            cover = nem.get("_cover", 0)  # injected by the agent after compile_state
            visible = bool(nem.get("see_player"))
            if cover in (1, 2, 3) and visible:
                r += R_COVER_WITH_LOS
                self.totals["cover_polls"] += 1

            if nem_events_this_poll > 0 and not cover and visible:
                r += R_HIT_NO_COVER * nem_events_this_poll
                self.totals["hit_open_polls"] += 1

            pos = tuple(nem.get("pos", [0, 0])[0:2])
            moved = (self.last_pos is not None
                     and (abs(pos[0] - self.last_pos[0]) + abs(pos[1] - self.last_pos[1])) > 4)
            if not moved and not cover and visible:
                r += R_IDLE_OPEN_LOS
                self.totals["idle_open_polls"] += 1
            self.last_pos = pos

        if player.get("health") is not None:
            self.last_player_hp = player["health"]
        if leveltime is not None:
            self.last_leveltime = leveltime
        return r

    def terminal_nemesis_death(self) -> float:
        """-100 + 2*player_dmg_taken + 0.1*tics_survived."""
        tics = 0.0
        if self.last_leveltime is not None:
            tics = self.last_leveltime * TICRATE  # leveltime delta tracked by caller
        return R_DEATH_BASE + R_DMG_TO_PLAYER * self.totals["player_dmg_taken"] + R_TIC_SURVIVED * tics


def episode_terminal(nemesis_died: bool, player_died: bool, player_dmg_taken: float,
                     survival_seconds: float) -> float:
    """Pure function version used by the offline trainer (no accumulator state)."""
    if player_died:
        return R_PLAYER_DEATH
    if nemesis_died:
        return (R_DEATH_BASE + R_DMG_TO_PLAYER * player_dmg_taken
                + R_TIC_SURVIVED * survival_seconds * TICRATE)
    return 0.0
