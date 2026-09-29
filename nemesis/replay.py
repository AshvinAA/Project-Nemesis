"""Replay: reconstruct episodes from the agent's jsonl logs.

    python -m nemesis.replay                 # summarize all episodes in nemesis/log
    python -m nemesis.replay --episode 2     # full timeline of one episode
    python -m nemesis.replay --dir path/to/logdir

Also provides load_polls() — the record loader the Phase-3 offline trainer
replays (identical data the live agent saw, including our sent orders).
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Optional

from . import config
from .state import compile_state, regions_links_from_obs
from .events import EventCursor, NemesisEvent, episode_totals
from .rewards import episode_terminal

DEFAULT_LOG_DIR = os.path.join(os.path.dirname(__file__), "log")


def _find_nemesis(obs: dict) -> Optional[dict]:
    for m in obs.get("monsters", []):
        if str(m.get("type", "")).lower() == config.NEMESIS_TYPE and m.get("hp", 0) > 0:
            return m
    return None


def load_polls(log_dir: str = DEFAULT_LOG_DIR) -> list[dict]:
    """Unified poll records: {t, obs, nem, state, events} in file order."""
    obs_path = os.path.join(log_dir, "obs.jsonl")
    polls: list[dict] = []
    if not os.path.exists(obs_path):
        return polls
    cursor = EventCursor()
    prev_hp: Optional[int] = None
    with open(obs_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            obs = rec.get("obs", {})
            if obs.get("nolevel"):
                continue
            regions, links = regions_links_from_obs(obs)
            nem = _find_nemesis(obs)
            events: list[NemesisEvent] = cursor.update(obs)
            state = None
            if nem is not None:
                state = compile_state(nem, obs.get("player", {}), prev_hp, regions, links)
                nem["_cover"] = state.cover  # shaping consumes this
                prev_hp = nem.get("hp")
            elif nem is None:
                prev_hp = None
            polls.append({"t": rec.get("t"), "obs": obs, "nem": nem,
                          "state": state, "events": events})
    return polls


def split_episodes(polls: list[dict]) -> list[list[dict]]:
    """Episode = maximal run of polls where the nemesis is alive, PLUS the
    single closing poll where it is gone (terminal events — the engine's kill
    event — land there). Polls before the first sighting are a prologue."""
    episodes: list[list[dict]] = []
    cur: list[dict] = []
    for p in polls:
        if p["nem"] is not None:
            cur.append(p)
        elif cur:
            cur.append(p)   # keep the death poll: terminal events land here
            episodes.append(cur)
            cur = []
    if cur:
        episodes.append(cur)
    return episodes


def summarize(episode: list[dict], idx: int) -> str:
    events = [e for p in episode for e in p["events"]]
    tot = episode_totals(events)
    first, last = episode[0], episode[-1]
    dur = (last["t"] or 0) - (first["t"] or 0)
    hp_start = first["obs"].get("player", {}).get("health")
    hp_end = last["obs"].get("player", {}).get("health")
    player_dmg = (hp_start - hp_end) if (hp_start is not None and hp_end is not None) else 0
    r_term = episode_terminal(nemesis_died=True, player_died=False,
                              player_dmg_taken=float(max(0, player_dmg)),
                              survival_seconds=max(0.0, dur))
    return (f"ep {idx}: {len(episode)} polls, {dur:.1f}s | nemesis hits taken: "
            f"{tot['hits']} ({tot['damage_taken_by_nemesis']} dmg) | player hp "
            f"{hp_start}->{hp_end} | terminal reward ~= {r_term:.1f}")


def timeline(episode: list[dict]) -> str:
    out: list[str] = []
    for p in episode:
        nem, st = p["nem"], p["state"]
        ev = ",".join(f"{e.kind}:{e.weapon}:{e.damage}" for e in p["events"]) or "-"
        if nem is None or st is None:
            out.append("  (nemesis absent)")
            continue
        order = nem.get("order", "?")
        out.append(f"  t={p['t']} {st.name():52s} hp={nem.get('hp'):3d} "
                   f"order={order:12s} ev={ev}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Project Nemesis log replay")
    ap.add_argument("--dir", default=DEFAULT_LOG_DIR)
    ap.add_argument("--episode", type=int, default=None)
    ap.add_argument("--full", action="store_true", help="print timelines for all episodes")
    args = ap.parse_args()

    polls = load_polls(args.dir)
    if not polls:
        print(f"no observations in {args.dir} (run the agent first)")
        return 1
    eps = split_episodes(polls)
    prologue = len(polls) - sum(len(e) for e in eps)
    print(f"{len(polls)} polls ({prologue} pre-fight), {len(eps)} episodes\n")
    for i, ep in enumerate(eps, 1):
        print(summarize(ep, i))
    if args.full:
        for i, ep in enumerate(eps, 1):
            print(f"\n=== episode {i} ===")
            print(timeline(ep))
    if args.episode is not None:
        if 1 <= args.episode <= len(eps):
            print(f"\n=== episode {args.episode} ===")
            print(timeline(eps[args.episode - 1]))
        else:
            print(f"no episode {args.episode} (have {len(eps)})")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
