#!/usr/bin/env python3
"""Phase 2 unit tests: state compiler, event cursor, replay reconstruction.

Run:  python -m nemesis.test_phase2
"""

from __future__ import annotations

from nemesis.state import (StateVector, compile_state, STATE_COUNT,
                           _angle_bucket)
from nemesis.events import EventCursor, parse_label, episode_totals

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def test_state_count():
    check(STATE_COUNT == 576, "state space is %d, expected 576" % STATE_COUNT)
    seen = set()
    for d in range(3):
        for l in range(2):
            for a in range(4):
                for c in range(4):
                    for h in range(3):
                        for r in range(2):
                            sv = StateVector(d, l, a, c, h, r)
                            i = sv.index()
                            check(0 <= i < STATE_COUNT, "index %d out of range" % i)
                            seen.add(i)
    check(len(seen) == 576, "index() not bijective: %d unique" % len(seen))


def test_parse_label():
    e = parse_label("41:hit:shotgun:shotgun:18")
    check(e is not None and e.seq == 41 and e.kind == "hit"
          and e.mtype == "shotgun" and e.weapon == "shotgun" and e.damage == 18,
          "hit label parse wrong: %r" % (e,))
    k = parse_label("42:kill:shotgun:chaingun")
    check(k is not None and k.kind == "kill" and k.damage == 0,
          "kill label parse wrong: %r" % (k,))
    check(parse_label("garbage") is None, "garbage should not parse")
    check(parse_label("7:hit:imp:plasma:30") is not None, "other-type hits parse")


def test_event_cursor():
    cursor = EventCursor()
    obs1 = {"nemesis": {"version": 1, "rows": [], "events": [
        "5:kill:shotgun:shotgun",       # newest-first ring order
        "4:hit:shotgun:shotgun:18",
        "3:hit:imp:chaingun:12",        # other type -> filtered out
    ]}}
    evs = cursor.update(obs1)
    check([e.seq for e in evs] == [4, 5], "sort+filter wrong: %r" % (evs,))
    check(all(e.mtype == "shotgun" for e in evs), "vocab filter failed")
    check(cursor.last_seq == 5, "cursor at %d, expected 5" % cursor.last_seq)

    # dedupe: resending the same ring returns nothing new
    evs2 = cursor.update(obs1)
    check(evs2 == [], "dedupe failed")

    # gap detection (ring overflow): next ring holds only seq 9
    obs2 = {"nemesis": {"events": ["9:hit:shotgun:pistol:9"]}}
    evs3 = cursor.update(obs2)
    check([e.seq for e in evs3] == [9], "gap handling wrong: %r" % (evs3,))
    check(cursor.gaps == [(5, 8)], "gap not recorded: %r" % (cursor.gaps,))

    # missing nemesis block -> no advance (buffer-pressure path)
    evs4 = cursor.update({"monsters": []})
    check(evs4 == [], "events returned without a nemesis block")
    check(cursor.last_seq == 9, "cursor advanced on a block-less snapshot")


def _nem(hp=30):
    return {"id": 3, "type": "shotgunguy", "pos": [100.0, 100.0, 0], "hp": hp,
            "region": 7, "see_player": True, "d_player": 300, "order": "chase"}


_PLAYER = {"pos": [400.0, 100.0, 0], "angle": 0, "health": 100, "region": 8}


def test_state_compiler():
    regions = {7: (100.0, 100.0), 8: (200.0, 100.0)}
    links = [(7, 8, "door")]
    sv = compile_state(_nem(), _PLAYER, prev_hp=30, regions=regions, links=links)
    check(sv.dist == 1, "dist 300 -> medium, got %r" % sv.dist)
    check(sv.los == 1, "see_player -> visible")
    check(sv.cover != 0, "door link adjacent -> some cover, got %r" % sv.cover)
    check(sv.hp == 2, "hp 30/30 -> healthy, got %r" % sv.hp)
    check(sv.hit_recent == 0, "no hp drop -> no recent hit")

    sv2 = compile_state(_nem(hp=22), _PLAYER, prev_hp=30, regions=regions, links=links)
    check(sv2.hit_recent == 1, "hp 30->22 flags hit_recent, got %r" % sv2.hit_recent)
    check(sv2.hp == 2, "hp 22/30 (frac %.2f) still > 2/3 -> healthy, got %r" % (22 / 30.0, sv2.hp))
    sv2b = compile_state(_nem(hp=15), _PLAYER, prev_hp=30, regions=regions, links=links)
    check(sv2b.hp == 1, "hp 15/30 (frac %.2f) -> hurt, got %r" % (15 / 30.0, sv2b.hp))

    sv3 = compile_state(_nem(hp=5), _PLAYER, prev_hp=22, regions=regions, links=links)
    check(sv3.hp == 0, "hp 5/30 -> critical, got %r" % sv3.hp)
    check(sv3.hit_recent == 1, "second drop still hit_recent")


def test_angle_buckets():
    # player at origin facing +x (angle 0). monster due north (doom y-up).
    check(_angle_bucket(0.0, 100.0, 0.0, 0.0, 0.0) == 1, "north of facing +x -> left")
    check(_angle_bucket(0.0, -100.0, 0.0, 0.0, 0.0) == 2, "south of facing +x -> right")
    check(_angle_bucket(50.0, 0.0, 0.0, 0.0, 0.0) == 0, "dead ahead -> front")
    check(_angle_bucket(-50.0, 0.0, 0.0, 0.0, 0.0) == 3, "directly behind -> behind")
    # doom angles: 0=+x east, 90=north, 180=west, 270=south (CCW)
    check(_angle_bucket(-50.0, 0.0, 0.0, 0.0, 180.0) == 0, "facing 180(west), monster west -> front")
    check(_angle_bucket(0.0, -50.0, 0.0, 0.0, 270.0) == 0, "facing 270(south), monster south -> front")
    check(_angle_bucket(0.0, 50.0, 0.0, 0.0, 180.0) == 2, "facing west, monster south-behind -> right")


def test_replay_roundtrip():
    import tempfile
    import json
    import os
    from nemesis.replay import load_polls, split_episodes, summarize

    tmp = tempfile.mkdtemp(prefix="nemesis_p2_")

    def obs_line(monsters, evs, t):
        rec = {"t": t, "poll": 1,
               "obs": {"player": {"pos": [400.0, 100.0, 0], "health": 100, "angle": 0},
                       "monsters": monsters,
                       "regions": [[7, 100.0, 100.0], [8, 200.0, 100.0]],
                       "links": [],
                       "nemesis": {"events": evs}}}
        return json.dumps(rec)

    lines = [
        obs_line([], [], 1.0),                                          # prologue
        obs_line([_nem()], ["1:hit:shotgun:pistol:15"], 2.0),           # ep 1 starts
        obs_line([_nem(hp=15)], [], 3.0),
        obs_line([], ["2:kill:shotgun:shotgun"], 4.0),                  # ep 1 ends
        obs_line([], [], 5.0),
    ]
    with open(os.path.join(tmp, "obs.jsonl"), "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln + "\n")

    polls = load_polls(tmp)
    check(len(polls) == 5, "poll count %d, expected 5" % len(polls))
    eps = split_episodes(polls)
    check(len(eps) == 1, "episode count %d, expected 1" % len(eps))
    check(len(eps[0]) == 3, "episode 1 length %d, expected 3 (2 alive + death poll)" % len(eps[0]))
    line = summarize(eps[0], 1)
    check("ep 1" in line and "15" in line, "summarize output odd: %s" % line)
    totals = episode_totals([e for p in eps[0] for e in p["events"]])
    check(totals["hits"] == 1 and totals["kills"] == 1, "totals wrong: %r" % totals)


def main():
    test_state_count()
    test_parse_label()
    test_event_cursor()
    test_state_compiler()
    test_angle_buckets()
    test_replay_roundtrip()
    if failures:
        print("PHASE2 FAIL:")
        for f_ in failures:
            print("  -", f_)
        return 1
    print("PHASE2 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
