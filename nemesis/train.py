#!/usr/bin/env python3
"""Phase 6 training orchestrator.

    python -m nemesis.train --episodes 25 --fresh        # full training run
    python -m nemesis.train --episodes 10 --control      # A/B baseline (random)
    python -m nemesis.train --episodes 25 --launch       # also launch the game

Behavior:
  - optionally launches BuddyDoom via PowerShell Start-Process (cmd start
    HANGS the calling shell — see HANDOFF.md §6.4),
  - --fresh wipes the qtable (and optionally the C-side nemesis memory),
  - runs the live agent until N episodes COMPLETE (learner-confirmed),
  - writes nemesis/log/training_curve.csv (episode, terminal_r, survived_s,
    epsilon, player_dmg) and prints the first5-vs-last5 trend.

The demo claim is the delta between a --fresh run and a --control run on the
same map/day/loadout: trained greedy must survive longer than random.
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import time

from . import config
from .rl_agent import LiveLearner, run as agent_run

DEFAULT_QTABLE = config.QTABLE_PATH
MEMORY_DAT = os.path.join("BuddyDoom", "run", "nemesis_memory.dat")

GAME_EXE = os.path.join("BuddyDoom", "run", "buddydoom.exe")
GAME_ARGS = ["-iwad", "freedoom1.wad", "-warp", "1", "1", "-skill", "3",
             "-aidirector", str(config.DIRECTOR_PORT)]


def launch_game() -> bool:
    """Start-Process (PowerShell) — the only launch path that doesn't hang."""
    if not os.path.exists(GAME_EXE):
        print(f"[train] {GAME_EXE} not found — launch the game manually with:")
        print("        buddydoom.exe -iwad freedoom1.wad -warp 1 1 -skill 3 -aidirector 31666")
        return False
    try:
        subprocess.run(["powershell", "-Command",
                        "Start-Process -FilePath 'BuddyDoom\\run\\buddydoom.exe' "
                        f"-ArgumentList '{','.join(GAME_ARGS)}' "
                        "-WorkingDirectory 'BuddyDoom\\run'"],
                       check=True, timeout=15)
        print("[train] game launched; waiting for the director listener ...")
        time.sleep(6)
        return True
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as e:
        print(f"[train] launch failed ({e}); start the game manually")
        return False


class CompletionGate:
    """Stops the agent once N episodes have completed."""

    def __init__(self, n: int) -> None:
        self.target = n
        self.completed = 0
        self.rows: list[dict] = []

    def __call__(self, event: str, info: dict) -> None:
        if event == "episode_end":
            self.completed += 1
            self.rows.append(info)
            print(f"[train] progress: {self.completed}/{self.target} episodes")
        elif event == "episode_start":
            print(f"[train] ep {info['episode']} in flight ...")


def write_curve(rows: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["episode", "terminal_r", "survived_s",
                                          "epsilon", "player_dmg_taken"])
        w.writeheader()
        for r in rows:
            w.writerow({k: (f"{r[k]:.3f}" if isinstance(r[k], float) else r[k])
                        for k in w.fieldnames})


def trend(rows: list[dict]) -> str:
    if len(rows) < 4:
        return "not enough episodes for a trend"
    k = max(1, len(rows) // 5)
    def mean(key, xs):
        return sum(x[key] for x in xs) / len(xs)
    head_r, tail_r = mean("terminal_r", rows[:k]), mean("terminal_r", rows[-k:])
    head_s, tail_s = mean("survived_s", rows[:k]), mean("survived_s", rows[-k:])
    return (f"survival {head_s:.1f}s -> {tail_s:.1f}s | "
            f"terminal {head_r:+.1f} -> {tail_r:+.1f} | "
            f"(+{k} ep window each end, {len(rows)} total)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Project Nemesis training orchestrator")
    ap.add_argument("--episodes", type=int, default=25)
    ap.add_argument("--fresh", action="store_true", help="wipe qtable (+ --wipe-memory)")
    ap.add_argument("--wipe-memory", action="store_true",
                    help="also delete BuddyDoom/run/nemesis_memory.dat")
    ap.add_argument("--control", action="store_true",
                    help="epsilon=1.0 random-policy baseline (A/B comparison)")
    ap.add_argument("--launch", action="store_true", help="launch the game first")
    ap.add_argument("--qtable", default=None)
    args = ap.parse_args()

    qtable = args.qtable or DEFAULT_QTABLE
    if args.fresh and os.path.exists(qtable):
        os.remove(qtable)
        print(f"[train] wiped {qtable}")
    if (args.fresh or args.wipe_memory) and os.path.exists(MEMORY_DAT):
        os.remove(MEMORY_DAT)
        print(f"[train] wiped {MEMORY_DAT}")

    if args.launch:
        launch_game()

    if args.control:
        print("[train] CONTROL RUN: epsilon=1.0 (random policy)")

    gate = CompletionGate(args.episodes)
    label = "control" if args.control else "training"
    agent_run(qtable_path=qtable,
              epsilon=1.0 if args.control else None,
              fresh=False,   # we wiped above, exactly once
              listener=gate,
              stop_when=lambda: gate.completed >= args.episodes)

    out = os.path.join(os.path.dirname(__file__), "log",
                       f"training_curve_{label}.csv")
    write_curve(gate.rows, out)
    print(f"\n[train] {label}: {gate.completed} episodes -> {out}")
    print(f"[train] trend: {trend(gate.rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
