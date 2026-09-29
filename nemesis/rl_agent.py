#!/usr/bin/env python3
"""Project Nemesis — Phase 1 RL agent (learning-free glue loop).

Connects to the engine's :31666 director listener and:
  1. polls `observe` at 10 Hz, logging every snapshot to nemesis/log/obs.jsonl
     (and every directive we send to nemesis/log/acts.jsonl — the offline
     trainer in Phase 3 needs both),
  2. keeps exactly ONE Shotgun Guy alive: when none is alive it spawns one
     (its death = the episode boundary the Q-learner will key on),
  3. issues a hardcoded `act order=chase` directive to it — no learning yet.

Run:  python -m nemesis.rl_agent            (game must be up with -aidirector 31666)
Stop: Ctrl+C (prints an episode summary; the engine reverts the monster to
      vanilla A_Chase within ~2 s of our last directive expiring).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from . import config
from .engine import DirectorLink

LOG_DIR = os.path.join(os.path.dirname(__file__), "log")


def _find_nemesis(obs: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The live Shotgun Guy entry from monsters[], or None."""
    for m in obs.get("monsters", []):
        if str(m.get("type", "")).lower() == config.NEMESIS_TYPE and m.get("hp", 0) > 0:
            return m
    return None


class EpisodeTracker:
    """Episode = spawn of the Shotgun Guy until it is no longer alive."""

    def __init__(self) -> None:
        self.episode = 0
        self.start: Optional[float] = None
        self.alive_seen = False

    def note_alive(self, now: float) -> None:
        if not self.alive_seen:
            self.episode += 1
            self.start = now
            self.alive_seen = True
            print(f"[ep {self.episode}] Shotgun Guy alive — episode started")

    def note_dead(self, now: float) -> Optional[float]:
        if self.alive_seen:
            dur = now - (self.start or now)
            self.alive_seen = False
            print(f"[ep {self.episode}] Shotgun Guy died — episode ended ({dur:.1f} s)")
            return dur
        return None


def run(max_seconds: Optional[float] = None, port: Optional[int] = None,
        log_dir: str = LOG_DIR) -> EpisodeTracker:
    """The agent loop. max_seconds/port/log_dir exist for the offline self-test;
    live use is just run()."""
    os.makedirs(log_dir, exist_ok=True)
    obs_log = open(os.path.join(log_dir, "obs.jsonl"), "a", encoding="utf-8")
    act_log = open(os.path.join(log_dir, "acts.jsonl"), "a", encoding="utf-8")

    link = DirectorLink(port=port or config.DIRECTOR_PORT)
    tracker = EpisodeTracker()
    last_order_poll = -10**9
    last_ordered_id: Optional[int] = None
    last_spawn_attempt = 0.0
    nem_id: Optional[int] = None
    polls = 0
    deadline = time.monotonic() + max_seconds if max_seconds else None

    print(f"[nemesis] connecting to {link.host}:{link.port} ...")
    print("[nemesis] Ctrl+C to stop. All output logs to nemesis/log/*.jsonl")

    try:
        while deadline is None or time.monotonic() < deadline:
            loop_t0 = time.monotonic()

            if not link.ensure_connected():
                time.sleep(config.RECONNECT_DELAY)
                continue

            obs = link.observe()
            polls += 1
            if obs is None:
                time.sleep(config.RECONNECT_DELAY)
                continue
            if obs.get("nolevel"):
                time.sleep(config.POLL_PERIOD)  # title/intermission — wait for a map
                continue

            now = time.monotonic()

            # 1) Log the raw observation (offline training corpus).
            obs_log.write(json.dumps({"t": round(time.time(), 3), "poll": polls, "obs": obs},
                                     separators=(",", ":")) + "\n")

            # 2) Episode boundaries from the Shotgun Guy's presence.
            nem = _find_nemesis(obs)
            if nem is not None:
                fresh_id = int(nem.get("id", 0))
                tracker.note_alive(now)
                if fresh_id != nem_id:
                    nem_id = fresh_id  # ids are registry-slot+1: refresh EVERY observe
                # 3) Hardcoded Phase-1 policy: chase, refreshed on a cadence,
                #    short for= so a dead agent never leaves stale directives.
                #    An id CHANGE (respawn/roster shift) re-targets IMMEDIATELY
                #    instead of waiting for the cadence.
                if (now - last_order_poll >= config.ORDER_REFRESH_POLLS * config.POLL_PERIOD
                        or nem_id != last_ordered_id):
                    reply = link.act("chase", [nem_id])
                    last_order_poll = now
                    last_ordered_id = nem_id
                    act_log.write(json.dumps({"t": round(time.time(), 3), "poll": polls,
                                              "ep": tracker.episode, "id": nem_id,
                                              "order": "chase", "reply": reply},
                                             separators=(",", ":")) + "\n")
            else:
                nem_id = None
                ended = tracker.note_dead(now)
                # 4) Single-nemesis discipline: spawn when none alive (cooldown).
                if (ended is not None or not tracker.alive_seen) and now - last_spawn_attempt > config.RESPAWN_COOLDOWN:
                    reply = link.spawn(config.NEMESIS_SPAWN_ARG, 1)
                    last_spawn_attempt = now
                    act_log.write(json.dumps({"t": round(time.time(), 3), "poll": polls,
                                              "ep": tracker.episode + 1,
                                              "order": f"spawn {config.NEMESIS_SPAWN_ARG}",
                                              "reply": reply},
                                             separators=(",", ":")) + "\n")

            # pace the loop at POLL_HZ
            elapsed = time.monotonic() - loop_t0
            time.sleep(max(0.0, config.POLL_PERIOD - elapsed))

    except KeyboardInterrupt:
        pass
    finally:
        link.close()
        obs_log.close()
        act_log.close()
        n = tracker.episode
        print(f"\n[nemesis] stopped. episodes observed: {n}. logs: nemesis/log/obs.jsonl, acts.jsonl")
    return tracker


if __name__ == "__main__":
    run()
