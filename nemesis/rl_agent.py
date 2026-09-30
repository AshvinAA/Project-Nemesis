#!/usr/bin/env python3
"""Project Nemesis — live Q-learning agent (Phases 1-4 combined).

Loop (10 Hz, the same granularity the offline trainer validated):
  observe -> parse ground-truth events -> compile 576-state -> shape reward
          -> epsilon-greedy action -> engine order (movement only) -> Q update

Episode boundaries: the Shotgun Guy's death (terminal -100 + 2*player-dmg
+ 0.1/tic) and the player's death (terminal +150). Single-nemesis discipline:
when none is alive, spawn one.

  python -m nemesis.rl_agent                 # train live (game up on :31666)
  python -m nemesis.rl_agent --epsilon 1.0   # A/B control: pure random policy
  python -m nemesis.rl_agent --fresh         # wipe the qtable first
  python -m nemesis.rl_agent --qtable PATH   # explicit table file

No stat buffs, ever: actions map to movement orders only; attack gates stay
vanilla (see nemesis/policy.py and the PLAN's no-stat-buffs audit).
"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Optional

from . import config
from .engine import DirectorLink
from .state import compile_state, regions_links_from_obs
from .events import EventCursor
from .rewards import ShapingAccumulator, episode_terminal
from .qtable import QTable
from .policy import action_to_order
from .curriculum import SkillCurriculum
from .livestate import LiveState
from .dashboard import start as start_dashboard

LOG_DIR = os.path.join(os.path.dirname(__file__), "log")


def _find_nemesis(obs: dict[str, Any]) -> Optional[dict[str, Any]]:
    for m in obs.get("monsters", []):
        if str(m.get("type", "")).lower() == config.NEMESIS_TYPE and m.get("hp", 0) > 0:
            return m
    return None


class LiveLearner:
    """One MDP step per poll. Terminal updates on nemesis/player death.
    The SINGLE source of episode counting (q.episode = completed episodes).
    Optional `listener(event, info)` hook for printing/logging lifecycle."""

    def __init__(self, qtable_path: Optional[str] = None,
                 epsilon_override: Optional[float] = None,
                 listener=None) -> None:
        path = qtable_path or config.QTABLE_PATH
        self.q = QTable(path)
        self.loaded = self.q.load()
        self.epsilon_override = epsilon_override
        self.listener = listener
        self.cursor = EventCursor()
        self.prev_hp: Optional[int] = None
        self.pending: Optional[tuple[int, int]] = None   # (state_idx, action_idx)
        self.acc: Optional[ShapingAccumulator] = None
        self.ep_start: Optional[float] = None
        self.alive = False
        self.rewards_log: list[float] = []
        self._survivals: list[float] = []   # ring of last 10 survival times
        self.last_action_name: Optional[str] = None

    def _emit(self, event: str, info: dict) -> None:
        if self.listener is not None:
            self.listener(event, info)

    # -- HUD metrics (Phase 5: live in-engine overlay via C_Printf) ----------

    def hud_spec(self, last_action: Optional[str] = None) -> Optional[str]:
        """ep=N,eps=F,r=F,surv=F,act=NAME — the brief's live-learning proof.
        r = terminal reward of the last completed episode (0 before the first)."""
        if not self.alive and self.q.episode == 0:
            return None
        last_r = self.rewards_log[-1] if self.rewards_log else 0.0
        eps = (self.epsilon_override if self.epsilon_override is not None
               else self.q.epsilon())
        spec = (f"ep={self.episode_label()},eps={eps:.3f},r={last_r:+.1f},"
                f"surv={self._surv_avg():.1f}")
        if last_action:
            spec += f",act={last_action}"
        return spec[:80]  # C-side NEM_HUD_MAX is 96; leave margin

    def _surv_avg(self) -> float:
        survs = getattr(self, "_survivals", [])
        return (sum(survs) / len(survs)) if survs else 0.0

    def episode_label(self) -> int:
        """1-based number of the episode in flight (0 = pre-first-contact)."""
        return self.q.episode + (1 if self.alive else 0)

    # -- episode lifecycle --------------------------------------------------

    def _begin_episode(self, now: float) -> None:
        self.alive = True
        self.acc = ShapingAccumulator()
        self.prev_hp = None
        self.pending = None
        self.ep_start = now
        self._emit("episode_start", {"episode": self.episode_label()})

    def _end_episode(self, terminal_r: float, now: float) -> dict:
        """Fold the terminal reward into the last pending transition."""
        self.alive = False
        if self.pending is not None:
            s, a = self.pending
            self.q.update(s, a, terminal_r, None)
        self.pending = None
        self.q.episode += 1
        self.q.save()
        surv = now - (self.ep_start or now)
        self._survivals.append(surv)
        if len(self._survivals) > 10:
            self._survivals.pop(0)
        info = {"episode": self.q.episode, "terminal_r": terminal_r,
                "survived_s": surv, "epsilon": self.q.epsilon(),
                "player_dmg_taken": self.acc.totals["player_dmg_taken"] if self.acc else 0}
        self.rewards_log.append(terminal_r)
        self._emit("episode_end", info)
        return info

    # -- one poll -------------------------------------------------------------

    def step(self, obs: dict, now: float) -> Optional[tuple[str, list[int], int, Optional[tuple[int, int]]]]:
        """Returns (order_name, ids, for_tics, anchor) the caller should send,
        or ('spawn', ...) when a respawn is due, or None."""
        events = self.cursor.update(obs)
        player = obs.get("player", {})
        regions, links = regions_links_from_obs(obs)
        nem = _find_nemesis(obs)
        hits_this_poll = sum(1 for e in events if e.kind == "hit")

        # --- player dead: nemesis wins the episode ---------------------------
        if player.get("health", 100) <= 0:
            if self.alive:
                self._end_episode(episode_terminal(nemesis_died=False,
                                                   player_died=True,
                                                   player_dmg_taken=0.0,
                                                   survival_seconds=now - (self.ep_start or now)),
                                  now)
            return None  # never spawn into a dead player's world

        # --- nemesis dead (or never seen): terminal + respawn request --------
        if nem is None:
            if self.alive:
                dmg = float(self.acc.totals["player_dmg_taken"]) if self.acc else 0.0
                self._end_episode(episode_terminal(nemesis_died=True,
                                                   player_died=False,
                                                   player_dmg_taken=dmg,
                                                   survival_seconds=now - (self.ep_start or now)),
                                  now)
            self.prev_hp = None
            return ("spawn", [], 0, None)   # keep exactly one alive (rate-limited by caller)

        # --- nemesis alive: one MDP step --------------------------------------
        if not self.alive:
            self._begin_episode(now)
        assert self.acc is not None

        sv = compile_state(nem, player, self.prev_hp, regions, links)
        nem["_cover"] = sv.cover                      # consumed by shaping
        r = self.acc.poll(nem, player, obs.get("tic"), hits_this_poll)
        self.prev_hp = nem.get("hp")

        s_idx = sv.index()
        if self.pending is not None:
            ps, pa = self.pending
            self.q.update(ps, pa, r, s_idx)           # bootstrapped step
        a_idx = self.q.select(s_idx, epsilon=(self.epsilon_override
                                              if self.epsilon_override is not None
                                              else None))
        self.pending = (s_idx, a_idx)

        order, anchor = action_to_order(a_idx, nem, player, regions, links)
        self.last_action_name = order
        return (order, [int(nem.get("id", 0))], config.ORDER_FOR_TICS, anchor)


def run(max_seconds: Optional[float] = None, port: Optional[int] = None,
        log_dir: str = LOG_DIR, qtable_path: Optional[str] = None,
        epsilon: Optional[float] = None, fresh: bool = False,
        listener=None, stop_when=None, dashboard: bool = True) -> LiveLearner:
    os.makedirs(log_dir, exist_ok=True)
    obs_log = open(os.path.join(log_dir, "obs.jsonl"), "a", encoding="utf-8")
    act_log = open(os.path.join(log_dir, "acts.jsonl"), "a", encoding="utf-8")

    if fresh and qtable_path and os.path.exists(qtable_path):
        os.remove(qtable_path)
        print(f"[nemesis] wiped {qtable_path}")

    link = DirectorLink(port=port or config.DIRECTOR_PORT)
    curriculum = SkillCurriculum(link)
    live = LiveState()

    def _tap(event: str, info: dict) -> None:
        """Feed the dashboard ticker and run the curriculum promotion wave
        on episode end (engine-earned lessons are absorbed via reconcile)."""
        if event == "episode_start":
            live.log_event(f"ep {info.get('episode')} started — nemesis alive")
        elif event == "episode_end":
            live.log_event(f"ep {info.get('episode')} ended: terminal "
                           f"{info['terminal_r']:+.1f}, "
                           f"survived {info['survived_s']:.1f}s")
            if curriculum.update_for_episode(info.get("episode", 0)):
                from . import config as _c
                live.log_event(f"curriculum: buddy promoted to "
                               f"{_c.SKILL_LEVELS[curriculum.level]} "
                               f"(skill {curriculum.level})")
        if listener is not None:
            listener(event, info)

    learner = LiveLearner(qtable_path, epsilon_override=epsilon,
                          listener=_tap)
    print(f"[nemesis] qtable {'loaded' if learner.loaded else 'NEW'} "
          f"(episode {learner.q.episode}, eps={learner.q.epsilon():.3f}"
          f"{', CONTROL eps=' + str(epsilon) if epsilon is not None else ''})")

    dash = None
    if dashboard:
        try:
            dash, dash_url = start_dashboard(live=live)
            print(f"[nemesis] dashboard live at {dash_url}")
        except OSError as e:
            print(f"[nemesis] dashboard unavailable: {e}")

    last_spawn_attempt = 0.0
    last_sent: Optional[tuple] = None
    polls = 0
    deadline = time.monotonic() + max_seconds if max_seconds else None

    print(f"[nemesis] connecting to {link.host}:{link.port} ...")
    print("[nemesis] Ctrl+C to stop. Logs: nemesis/log/obs.jsonl, acts.jsonl")

    try:
        while ((deadline is None or time.monotonic() < deadline)
               and not (stop_when and stop_when())):
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
                time.sleep(config.POLL_PERIOD)
                continue
            now = time.monotonic()

            # Phase 9: absorb engine-side auto-lessons into the curriculum.
            curriculum.reconcile(obs.get("nemesis", {}).get("buddy_skill"))

            obs_log.write(json.dumps({"t": round(time.time(), 3), "poll": polls,
                                      "obs": obs}, separators=(",", ":")) + "\n")

            nem_alive = _find_nemesis(obs) is not None
            decision = learner.step(obs, now)

            sent_line = None
            if decision is not None:
                order, ids, fort, anchor = decision
                if order == "spawn":
                    if now - last_spawn_attempt > config.RESPAWN_COOLDOWN:
                        reply = link.spawn(config.NEMESIS_SPAWN_ARG, 1,
                                           hud=learner.hud_spec())
                        last_spawn_attempt = now
                        live.log_event("respawn requested — one nemesis stays alive")
                        sent_line = {"t": round(time.time(), 3), "poll": polls,
                                     "ep": learner.episode_label(), "order": f"spawn {config.NEMESIS_SPAWN_ARG}",
                                     "reply": reply}
                elif ids:
                    reply = link.act(order, ids, for_tics=fort, x=anchor[0] if anchor else None,
                                     y=anchor[1] if anchor else None,
                                     hud=learner.hud_spec(last_action=order))
                    last_sent = (order, tuple(ids))
                    sent_line = {"t": round(time.time(), 3), "poll": polls,
                                 "ep": learner.episode_label(), "id": ids[0] if ids else None,
                                 "order": order, "reply": reply}

            if sent_line:
                act_log.write(json.dumps(sent_line, separators=(",", ":")) + "\n")

            # Phase 9: snapshot (~4 Hz) + history row (~1 Hz), throttled inside.
            live.update(learner, obs, curriculum)

            elapsed = time.monotonic() - loop_t0
            time.sleep(max(0.0, config.POLL_PERIOD - elapsed))

    except KeyboardInterrupt:
        pass
    finally:
        link.close()
        if dash is not None:
            dash.shutdown()
        obs_log.close()
        act_log.close()
        learner.q.save()
        print(f"\n[nemesis] stopped. episodes completed: {learner.q.episode}. "
              f"qtable at {learner.q.path}.")
    return learner


def _default_listener(event: str, info: dict) -> None:
    if event == "episode_start":
        print(f"[ep {info['episode']}] Shotgun Guy alive — episode started")
    elif event == "episode_end":
        print(f"[ep {info['episode']}] ended: terminal={info['terminal_r']:+.1f} "
              f"survived={info['survived_s']:.1f}s eps={info['epsilon']:.3f} "
              f"player_dmg={info['player_dmg_taken']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Project Nemesis live RL agent")
    ap.add_argument("--qtable", default=None, help="qtable.json path")
    ap.add_argument("--epsilon", type=float, default=None,
                    help="override epsilon (A/B control: 1.0 = random policy)")
    ap.add_argument("--fresh", action="store_true", help="wipe the qtable first")
    ap.add_argument("--max-seconds", type=float, default=None)
    ap.add_argument("--no-dashboard", action="store_true",
                    help="skip the :8787 weight-evolution console")
    args = ap.parse_args()
    run(qtable_path=args.qtable, epsilon=args.epsilon, fresh=args.fresh,
        max_seconds=args.max_seconds, listener=_default_listener,
        dashboard=not args.no_dashboard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
