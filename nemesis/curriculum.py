"""Phase 9: the hostile-buddy skill curriculum.

The trainer owns the level.  Policy (two signals, whichever arrives first):
  1. EPISODES: +1 skill level every SKILL_EPISODES_PER_LEVEL completed
     episodes (5 per level by default -> veteran at 20).
  2. LESSONS: the engine auto-promotes on the buddy's death and auto-demotes
     when it kills the player.  The trainer reconciles on every episode end:
     it never pushes a level BELOW what the engine reports (a lesson earned is
     a lesson kept until the trainer's own promotion wave passes it).

All pushes go over the existing director link as `buddy skill=N` (acked `ok`).
No stat buffs anywhere: the level only reshapes reaction/aim/trigger/keep-away
inside the engine (p_ai_coop.c Phase 9 tables).
"""

from __future__ import annotations

from typing import Optional

from . import config


def level_for_episode(episode: int) -> int:
    """Trainer policy: episodes completed -> curriculum level (clamped 0..4)."""
    if episode <= 0:
        return 0
    lvl = episode // config.SKILL_EPISODES_PER_LEVEL
    return min(config.SKILL_MAX, lvl)


class SkillCurriculum:
    """Tracks the hostile-buddy skill level and pushes changes to the engine."""

    def __init__(self, link) -> None:
        self.link = link                     # nemesis.engine.DirectorLink
        self.level: int = 0                  # last level we know the engine is at
        self.pushed: int = 0                 # number of successful pushes this session
        self.last_error: Optional[str] = None

    def reconcile(self, engine_level: Optional[int]) -> None:
        """Absorb engine-side auto-lessons (observe nemesis.buddy_skill)."""
        if isinstance(engine_level, int) and 0 <= engine_level <= config.SKILL_MAX:
            self.level = engine_level

    def update_for_episode(self, episode: int) -> bool:
        """Push the policy level for `episode` if it differs from the engine's.
        Returns True when a push was sent and acked."""
        want = level_for_episode(episode)
        # Never demote an engine-earned lesson unless the policy wave is the
        # thing raising the level (want >= level handles both directions of
        # the auto-lesson and keeps monotone progress within a session).
        target = max(want, self.level) if want > self.level else self.level
        if target == self.level:
            return False
        return self.push(target)

    def push(self, level: int) -> bool:
        """Force-push a level (clamped) to the engine. Returns True on 'ok'."""
        level = max(0, min(config.SKILL_MAX, level))
        reply = self.link.request(f"buddy skill={level}")
        if reply == "ok":
            self.level = level
            self.pushed += 1
            self.last_error = None
            return True
        self.last_error = reply or "no reply"
        return False
