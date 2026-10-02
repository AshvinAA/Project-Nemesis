#!/usr/bin/env python3
"""Phase 9.11/9.12 live ladder test: drive the hostile-buddy rank ladder over
the director protocol and verify the player-weapon grant, in a REAL engine.

Run the game first (hostile mode, e.g. Nemesis.bat gameonly), then:
    python tools/ladder_test.py

Checks:
  A. the player spawns/respawns holding the shotgun (observe player.weapon==2)
  B. `buddy xp=N` feeds the ladder: two 60-damage injections -> rank 1
  C. rankup/eased events land in the observe event ring
  D. `buddy skill=N` still works (trainer override path)
  E. XP overflow into multiple promotions + legend clamp
  F. (passive) when the buddy kills the idle player, the rank eases off and
     the player respawns STILL HOLDING the shotgun

Exits 0 when every active check passes.  ONE client rule: stop the dashboard
--live / agent before running this.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nemesis import config                      # noqa: E402
from nemesis.engine import DirectorLink         # noqa: E402

failures: list[str] = []


def check(cond, msg):
    print(("PASS: " if cond else "FAIL: ") + msg)
    if not cond:
        failures.append(msg)


def nem(link) -> dict:
    obs = link.observe()
    return (obs or {}).get("nemesis", {}) or {}


def wait_for_skill(link, want: int, timeout: float = 3.0) -> dict:
    end = time.monotonic() + timeout
    n = {}
    while time.monotonic() < end:
        n = nem(link)
        if n.get("buddy_skill") == want:
            return n
        time.sleep(0.1)
    return n


def main() -> int:
    link = DirectorLink(port=config.DIRECTOR_PORT)
    if not link.ensure_connected():
        print("FAIL: no engine on :%d — boot the game first (Nemesis.bat gameonly)" % config.DIRECTOR_PORT)
        return 2

    obs = link.observe() or {}
    player = obs.get("player", {})
    n = obs.get("nemesis", {})
    check(player.get("weapon") == 2, "player holds the shotgun at spawn (weapon==2, got %r)" % player.get("weapon"))
    check("buddy_xp" in n, "observe carries buddy_xp (got keys %s)" % sorted(n.keys()))
    skill0 = n.get("buddy_skill")
    check(isinstance(skill0, int) and 0 <= skill0 <= 4, "buddy_skill present and sane: %r" % skill0)

    # B. XP injections climb the ladder (2 x 60 -> exactly one promotion).
    link.request("buddy skill=0")
    time.sleep(0.1)
    link.request("buddy xp=60")
    time.sleep(0.1)
    n = nem(link)
    check(n.get("buddy_xp") == 60 and n.get("buddy_skill") == 0,
          "60 xp banked, still rookie (xp=%r skill=%r)" % (n.get("buddy_xp"), n.get("buddy_skill")))
    link.request("buddy xp=60")
    n = wait_for_skill(link, 1)
    check(n.get("buddy_skill") == 1, "promotion fired at 100 xp (skill=%r)" % n.get("buddy_skill"))
    check(n.get("buddy_xp") == 20, "xp pot reset to the remainder (xp=%r)" % n.get("buddy_xp"))
    evs = " ".join(n.get("events", []))
    check("rankup:buddy:1" in evs, "rankup event in the ring (events=%r)" % n.get("events", [])[-3:])

    # D. trainer override still works.
    link.request("buddy skill=3")
    n = wait_for_skill(link, 3)
    check(n.get("buddy_skill") == 3, "buddy skill=3 override acked and observed")

    # E. overflow: 250 xp from skill 3 -> legend (4) clamp; the pot stops
    # counting at the cap (99) so a demote re-climbs on FRESH xp only.
    link.request("buddy xp=250")
    n = wait_for_skill(link, 4)
    check(n.get("buddy_skill") == 4, "overflow promotions clamp at legend (skill=%r)" % n.get("buddy_skill"))
    xp = n.get("buddy_xp")
    check(isinstance(xp, int) and 0 <= xp < 100, "overflow pot capped below one rank (xp=%r)" % xp)

    # F. passive: if the idle player dies to the buddy, rank eases off and the
    # respawn keeps the shotgun.  Only meaningful in -buddyhostile mode.
    # Watch from rank 1 so a single demote lands at 0.
    link.request("buddy skill=1")
    time.sleep(0.2)
    print("... watching 75 s for the buddy to kill the idle player (passive check)")
    end = time.monotonic() + 75.0
    saw_low_hp = saw_skill1 = saw_skill_drop = saw_hp100 = False
    while time.monotonic() < end and not (saw_skill_drop and saw_hp100):
        obs = link.observe() or {}
        player = obs.get("player") or {}
        hp = player.get("health", 100)
        sk = (obs.get("nemesis") or {}).get("buddy_skill", 0)
        if hp < 70:
            saw_low_hp = True
        if sk == 1:
            saw_skill1 = True
        if saw_skill1 and sk == 0:
            saw_skill_drop = True
        if saw_low_hp and hp >= 100 and player.get("weapon") == 2:
            saw_hp100 = True		# respawned full health, still armed
        time.sleep(0.25)
    check(saw_skill_drop, "rank eased off after the buddy killed the player")
    check(saw_hp100, "player respawned at full health still holding the shotgun")

    # Leave a clean ladder for the next session.
    link.request("buddy skill=0")
    link.close()
    if failures:
        print("LADDER TEST: %d FAILURES" % len(failures))
        return 1
    print("LADDER TEST PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
