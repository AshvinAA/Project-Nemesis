#!/usr/bin/env python3
"""Offline self-test for the Phase 1 glue agent.

A mock TCP engine replays a synthetic observe stream (including the "no level"
title state, roster changes, a mid-fight abrupt disconnect, and reconnects),
then we assert the whole Phase-1 contract:
  - spawns a shotgun when none is alive, respects the respawn cooldown,
  - refreshes ids from every observe (never carries stale ids),
  - emits only vocabulary orders, only for CURRENT ids, with short for=,
  - survives an abrupt engine-side disconnect and reconnects,
  - writes coherent obs/acts logs with episode tags.

Run:  python -m nemesis.test_selftest
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time

from . import config
from . import rl_agent

ORDER_VOCAB = {"chase", "hold", "fallback", "flank_left", "flank_right",
               "ambush", "focus_fire", "use_door"}


def _shotgun(mid: int, hp: int = 30) -> dict:
    return {"id": mid, "type": "shotgunguy", "pos": [100, 100, 0], "hp": hp,
            "region": 7, "see_player": True, "d_player": 480}


class MockEngine:
    """Single-client loopback TCP server speaking the director protocol.

    `script` is a list consumed one item per `observe` request: a dict is
    serialized as the reply, a callable is called for the dict, and None
    means "drop the connection abruptly without replying" (tests the
    reconnect path against the listener-wedge bug we fixed in C).
    """

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.commands: list[str] = []   # every non-observe line, in order
        self.lock = threading.Lock()
        self.stop = False
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind((config.HOST, 0))
        srv.listen(1)
        srv.settimeout(0.2)
        self.srv = srv
        self.port: int = srv.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop = True
        self.thread.join(timeout=2)
        try:
            self.srv.close()
        except OSError:
            pass

    def _serve(self) -> None:
        while not self.stop:
            try:
                conn, _ = self.srv.accept()
            except (socket.timeout, OSError):
                continue
            conn.settimeout(0.05)
            buf = b""
            try:
                while not self.stop:
                    try:
                        data = conn.recv(4096)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not data:
                        break               # client gone; accept the next one
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        cmd = line.decode(errors="replace").strip()
                        if cmd == "observe":
                            with self.lock:
                                item = self.script.pop(0) if self.script else None
                            if item is None:
                                try:        # abrupt disconnect, no reply
                                    conn.close()
                                except OSError:
                                    pass
                                break
                            obj = item() if callable(item) else item
                            conn.sendall((json.dumps(obj, separators=(",", ":")) + "\n").encode())
                        else:
                            with self.lock:
                                self.commands.append(cmd)
                            conn.sendall(b"ok\n")
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass


def main() -> int:
    # shrink cadences so the test runs in ~2 s
    real = (config.POLL_PERIOD, config.ORDER_REFRESH_POLLS, config.RESPAWN_COOLDOWN)
    config.POLL_PERIOD = 0.01
    config.ORDER_REFRESH_POLLS = 2
    config.RESPAWN_COOLDOWN = 0.2

    nolevel = {"nolevel": True, "monsters": []}
    script = [
        nolevel,                       # title screen -> ignored
        {"monsters": []},              # level, no shotgun -> expect spawn #1
        {"monsters": []},              # still inside cooldown -> NO second spawn
        {"monsters": [_shotgun(3)]},   # spawned; id 3
        {"monsters": [_shotgun(3, hp=22)]},
        None,                          # abrupt engine-side disconnect
        {"monsters": [_shotgun(3)]},   # after reconnect, same roster
        {"monsters": [_shotgun(5)]},   # roster change -> id must refresh to 5
        {"monsters": []},              # died -> episode end, respawn (cooldown)
        {"monsters": []},
        {"monsters": [_shotgun(6)]},   # episode 2 begins
    ]

    mock = MockEngine(script)
    mock.start()

    tmpdir = tempfile.mkdtemp(prefix="nemesis_selftest_")
    t0 = time.monotonic()
    try:
        tracker = rl_agent.run(max_seconds=2.0, port=mock.port, log_dir=tmpdir)
    finally:
        config.POLL_PERIOD, config.ORDER_REFRESH_POLLS, config.RESPAWN_COOLDOWN = real
    dt = time.monotonic() - t0
    mock.close()

    with mock.lock:
        cmds = list(mock.commands)
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # spawns: at least one at start + one after the death, correct vocabulary
    spawns = [c for c in cmds if c.startswith("spawn")]
    check(len(spawns) >= 2, f"expected >=2 spawns, got {len(spawns)}: {cmds}")
    check("spawn type=shotgun count=1" in spawns, f"wrong spawn vocab: {spawns}")

    # acts: only vocabulary orders, current ids only, short for=
    acts = [c for c in cmds if c.startswith("act")]
    check(len(acts) >= 3, f"expected >=3 acts, got {len(acts)}")
    for c in acts:
        parts = c.split()
        order = next(p for p in parts if p.startswith("order=")).split("=", 1)[1]
        ids = next(p for p in parts if p.startswith("ids=")).split("=", 1)[1]
        fort = next(p for p in parts if p.startswith("for=")).split("=", 1)[1]
        check(order in ORDER_VOCAB, f"order out of vocabulary: {c}")
        check(int(fort) <= config.ORDER_FOR_TICS + 1, f"for= too long: {c}")
        check(int(ids) in (3, 5, 6), f"stale/foreign id in act: {c}")

    # id freshness: after the roster change the agent must target id 5
    check(any("ids=5" in c for c in acts), f"never re-targeted new id 5: {acts}")

    # episodes: alive(3) -> death -> alive(6) == 2 episodes
    check(tracker.episode == 2, f"expected 2 episodes, got {tracker.episode}")

    # logs coherent
    with open(os.path.join(tmpdir, "obs.jsonl"), encoding="utf-8") as f:
        obs_lines = [json.loads(l) for l in f if l.strip()]
    with open(os.path.join(tmpdir, "acts.jsonl"), encoding="utf-8") as f:
        act_lines = [json.loads(l) for l in f if l.strip()]
    check(len(obs_lines) >= 8, f"too few observations logged: {len(obs_lines)}")
    check(all("obs" in o and "poll" in o for o in obs_lines), "obs log malformed")
    check(all("order" in a and "reply" in a for a in act_lines), "act log malformed")
    check(all(a["ep"] in (1, 2) for a in act_lines), f"episode tags wrong: {act_lines}")

    if failures:
        print("SELFTEST FAIL:")
        for f_ in failures:
            print("  -", f_)
        return 1
    print(f"SELFTEST PASS: {len(cmds)} commands, {len(obs_lines)} observations, "
          f"{tracker.episode} episodes in {dt:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
