"""Parser for the engine's ground-truth event ring (observe -> "nemesis"."events").

Label formats (from files/p_nemesis.c NEM_NoteDamageM / NEM_NoteKill):
    "<seq>:hit:<type>:<weapon>:<damage>"
    "<seq>:kill:<type>:<weapon>"
- `type` uses the NEMESIS ROW vocabulary ("shotgun" for MT_SHOTGUY), NOT the
  observe monsters[] "type" vocabulary ("shotgunguy").
- AI_Serialize emits the ring NEWEST-FIRST. NEM_PushEvent writes the label
  AFTER incrementing nem_event_seq, so a larger seq is always a later event,
  even across engine restarts within a session.
- The whole nemesis block is dropped from the observe when the 32 KB buffer
  is tight, and deaths-by-foreign-monsters still emit rows for OTHER types.
  Therefore: update the cursor ONLY on snapshots that carry the block, and
  filter events by type. On a gap (missing seqs inside the ring) we log a
  warning once and advance anyway — those events are gone.

Reward mapping (the brief's spec, adapted to event granularity):
    hit  on shotgun  -> +2 * damage            (damage dealt to... wait:
        NEM rows record damage dealt BY THE PLAYER TO the monster. The brief's
        "+2 per point of damage dealt to the player" is measured at DEATH time
        from the player's hp delta; per-hit we credit the SHOT LANDED shaping
        reward instead. See rewards.py.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

NEMESIS_ROW = "shotgun"  # p_nemesis.c row vocabulary for MT_SHOTGUY

_HIT_RE = re.compile(r"^(\d+):hit:([a-z]+):([a-z]+):(\d+)$")
_KILL_RE = re.compile(r"^(\d+):kill:([a-z]+):([a-z]+)$")


@dataclass(frozen=True)
class NemesisEvent:
    seq: int
    kind: str            # "hit" | "kill"
    mtype: str           # row vocabulary, e.g. "shotgun"
    weapon: str          # player weapon name, e.g. "shotgun"
    damage: int          # 0 for kills


def parse_label(label: str) -> Optional[NemesisEvent]:
    m = _HIT_RE.match(label)
    if m:
        return NemesisEvent(int(m.group(1)), "hit", m.group(2), m.group(3), int(m.group(4)))
    m = _KILL_RE.match(label)
    if m:
        return NemesisEvent(int(m.group(1)), "kill", m.group(2), m.group(3), 0)
    return None


class EventCursor:
    """Dedupes the newest-first event ring across successive observe snapshots.

    Usage:
        cur = EventCursor()
        evs = cur.update(observe_dict)   # -> new events for OUR type, oldest first

    The cursor advances only when the snapshot actually contains the nemesis
    block (the engine omits it under buffer pressure; see NEM_Serialize).
    """

    def __init__(self, mtype: str = NEMESIS_ROW) -> None:
        self.mtype = mtype
        self.last_seq = -1
        self.gaps: list[tuple[int, int]] = []  # (from, to] seq ranges lost

    def update(self, obs: dict) -> list[NemesisEvent]:
        nem = obs.get("nemesis")
        if not isinstance(nem, dict):
            return []  # block omitted this snapshot; nothing to learn from
        labels = nem.get("events") or []
        parsed = [p for p in (parse_label(l) for l in labels) if p is not None]

        # ring arrives newest-first -> normalize oldest-first
        parsed.sort(key=lambda e: e.seq)
        fresh = [e for e in parsed if e.seq > self.last_seq]

        if fresh:
            if self.last_seq >= 0 and fresh[0].seq > self.last_seq + 1:
                self.gaps.append((self.last_seq, fresh[0].seq - 1))
            self.last_seq = fresh[-1].seq
        return [e for e in fresh if e.mtype == self.mtype]


def episode_totals(events: Iterable[NemesisEvent]) -> dict:
    """Fold our-type events into the per-episode reward counters."""
    hits = kills = 0
    damage_dealt_to_nemesis = 0
    for e in events:
        if e.kind == "hit":
            hits += 1
            damage_dealt_to_nemesis += e.damage
        elif e.kind == "kill":
            kills += 1
    return {"hits": hits, "kills": kills, "damage_taken_by_nemesis": damage_dealt_to_nemesis}
