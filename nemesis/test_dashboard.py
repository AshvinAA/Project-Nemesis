#!/usr/bin/env python3
"""Phase 9.13: dashboard + rank-ladder tests.

Covers (all offline, no engine needed):
  1. dashboard endpoints (/state, /history, /) against a REAL LiveState fed a
     synthetic observe snapshot (rank + XP + rows + rankup event),
  2. the UI contract: the page must carry the plain-English explainer, the XP
     bar, the rookie->legend ladder labels and the panel anchors,
  3. standalone file-replay mode (DashboardState reading the json/jsonl files),
  4. the curriculum ladder: episode progression, pushes, engine-lesson
     absorption (never demote), error path,
  5. the RL agent tolerates the new engine event kinds (rankup/eased labels
     parse to None -> ignored, no crash),
  6. level_for_episode boundaries.

Run:  python -m nemesis.test_dashboard
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.request

from . import config
from . import dashboard as dash
from .curriculum import SkillCurriculum, level_for_episode
from .events import parse_label
from .livestate import LiveState, param_deltas

failures: list[str] = []


def check(cond, msg):
    if cond:
        return
    failures.append(msg)
    print("FAIL:", msg)


def _synthetic_obs() -> dict:
    return {
        "tic": 4321,
        "player": {"pos": [100, 200, 0], "health": 77, "weapon": 2},
        "monsters": [],
        "nemesis": {
            "version": 1,
            "rows": [
                {"type": "shotgun", "deaths": 3,
                 "tactics": {"chase": 1.42, "hold": 0.71},
                 "weapon_bias": {"pistol": 0.2, "shotgun": -0.35}},
            ],
            "buddy_skill": 2,
            "buddy_xp": 40,  # must stay < BUDDY_XP_PER_RANK (60)
            "events": ["907:rankup:buddy:2", "906:hit:shotgun:shotgun:21",
                       "905:kill:imp:pistol"],
        },
    }


def test_scoreboard_stats() -> None:
    """Phase 9.16: LiveState folds the engine event ring into session stats."""
    import tempfile
    old_lp, old_hp = config.LIVE_STATE_PATH, config.STATE_HISTORY_PATH
    with tempfile.TemporaryDirectory() as tmpdir:
        config.LIVE_STATE_PATH = os.path.join(tmpdir, "sb_live.json")
        config.STATE_HISTORY_PATH = os.path.join(tmpdir, "sb_hist.jsonl")
        live = LiveState()
        # Feed an obs; the ring is baselined on first sight (no backlog counting).
        _feed(live, _synthetic_obs(), times=1)
        st1 = dict(live.stats)
        # New events only: 2 kills, 2 hits (33 dmg), 1 rankup, 1 eased.
        obs2 = _synthetic_obs()
        obs2["nemesis"]["events"] = [
            "912:eased:buddy:1", "911:rankup:buddy:2",
            "910:hit:caco:plasma:13", "909:kill:shotgun:ssg",
            "908:hit:shotgun:shotgun:20", "907:kill:imp:pistol",
            *obs2["nemesis"]["events"],
        ]
        _feed(live, obs2, times=2)
        st = live.stats
        check(st["kills"] == st1["kills"] + 1, "kill count wrong: %r (st1=%r)" % (st, st1))
        check(st["hits"] == st1["hits"] + 2, "hit count wrong: %r" % st)
        check(st["dmg"] == st1["dmg"] + 33, "damage sum wrong: %r" % st)
        check(st["rankups"] == st1["rankups"] + 1, "rankup count wrong: %r" % st)
        check(st["eased"] == st1["eased"] + 1, "eased count wrong: %r" % st)
        # Ring repeats (same seqs) must not double-count.
        _feed(live, obs2, times=2)
        check(live.stats["kills"] == st["kills"], "ring replay double-counted")
        # Player death detection (hp 100 -> 0).
        obs3 = _synthetic_obs()
        obs3["player"]["health"] = 0
        _feed(live, obs3, times=1)
        obs4 = _synthetic_obs()
        obs4["player"]["health"] = 100
        _feed(live, obs4, times=1)
        check(live.stats["pdeaths"] == 1, "player death not counted: %r" % live.stats)
    config.LIVE_STATE_PATH, config.STATE_HISTORY_PATH = old_lp, old_hp


def _feed(live: LiveState, obs: dict, times: int = 2) -> None:
    shim = dash._LiveShim()
    shim.last_action_name = "chase"
    shim.prev_hp = 30
    for _ in range(times):
        live.update(shim, obs, None)


def _get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def test_endpoints_and_ui(tmpdir: str) -> None:
    config.LIVE_STATE_PATH = os.path.join(tmpdir, "live_state.json")
    config.STATE_HISTORY_PATH = os.path.join(tmpdir, "state_history.jsonl")
    config.HISTORY_APPEND_EVERY = 0.0          # append every update
    live = LiveState()
    _feed(live, _synthetic_obs())

    httpd, url = dash.start(live=live, port=0)  # ephemeral port
    try:
        code, body = _get(url + "/state")
        snap = json.loads(body)
        check(code == 200, "GET /state status %s" % code)
        check(snap.get("buddy_skill") == 2, "snapshot buddy_skill != 2: %r" % snap.get("buddy_skill"))
        check(snap.get("buddy_xp") == 40, "snapshot buddy_xp != 40: %r" % snap.get("buddy_xp"))
        check(snap.get("buddy_xp_next") == config.BUDDY_XP_PER_RANK, "buddy_xp_next wrong")
        check(snap.get("buddy_skill_name") == "semi-pro", "rank name wrong: %r" % snap.get("buddy_skill_name"))
        check(snap.get("player_weapon") == 2, "player_weapon not mirrored: %r" % snap.get("player_weapon"))
        check(snap.get("rows"), "rows empty in snapshot")
        check(any("rankup" in e for e in snap.get("events", [])),
              "rankup event missing from snapshot events")

        code, body = _get(url + "/history")
        rows = json.loads(body).get("rows", [])
        check(code == 200 and rows, "GET /history empty")
        wt = rows[-1].get("wt", {})
        check("shotgun" in wt, "history wt missing shotgun row: %r" % list(wt))
        check(rows[-1].get("skill") == 2, "history row skill != 2")

        code, body = _get(url + "/")
        page = body.decode("utf-8")
        check(code == 200, "GET / status %s" % code)
        # UI contract: the plain-English pieces a normal person needs.
        check('id="hero"' in page, "hero explainer panel missing")
        check("What is jev doing right now?" in page, "explainer headline missing")
        check('id="xpbar"' in page and 'id="xptext"' in page, "XP bar missing")
        check("RANK_UP_TOKEN" not in page, "placeholder leaked into page")
        for name in config.SKILL_LEVELS:
            check(name in page, "ladder label %r missing from page" % name)
        for anchor in ('id="ladder"', 'id="heat"', 'id="tick"', 'id="spk_w"', 'id="spk_sk"'):
            check(anchor in page, "panel anchor %s missing" % anchor)
        check("plainify" in page and "rankup" in page, "plain-English ticker missing")
        # Phase 9.16 contract: hero ladder stepper, scoreboard, rank-up flash.
        check('id="ladder"' in page and 'class="ladder"' in page, "hero ladder missing")
        check("ldot" in page and "lbar" in page, "ladder stepper markup missing")
        check('id="sb"' in page and "scoreboard" in page.lower(), "scoreboard missing")
        check("rankFlash" in page and "RANK UP" in page, "rank-up flash missing")
        check("drawScoreboard" in page, "scoreboard renderer missing")
        for sid in ("sb_kills", "sb_hits", "sb_dmg", "sb_rankups", "sb_eased", "sb_pdeaths"):
            check(sid in page, "scoreboard tile %s missing" % sid)
        # Phase 9.17 contract: the "jev pulling the strings" parameter feed.
        check('id="strings"' in page, "param-feed panel missing")
        check("jev pulling the strings" in page, "param-feed headline missing")
        check('id="dfeed"' in page and 'id="dcount"' in page, "param-feed list missing")
        check("showDelta" in page and "deltaLine" in page, "param-feed renderer missing")
        check("paramDeltasFromRows" in page, "replay param-diff fallback missing")
        for k in ("tactic", "bias", "deaths", "rank", "xp", "eps"):
            check('"%s"' % k in page, "param-feed kind %r missing from renderer" % k)
        # Phase 9.18 contract: side-by-side compact mode.
        check("compact=1" in page and "body.compact" in page, "compact mode missing")
        check("max-width:660px" in page, "compact 660px column missing")
        check("qstoggle" in page, "compact toggle link missing")
        # Phase 9.19 contract: query-document terminal + phosphor bar meters.
        for tag in ("[INTENT]", "[FIRING]", "[GOAL]", "[DODGE]", "[MOVEMENT]", "[STRINGS]"):
            check(tag in page, "doc tag %r missing" % tag)
        check("DOCUMENTS" in page, "DOCUMENTS section label missing")
        check('id="qsorder"' in page and "standing order" in page, "standing-order line missing")
        check('id="dodgeq"' in page, "dynamic dodge query missing")
        for did in ("doc_fire", "doc_goal", "doc_dodge", "doc_move", "doc_drift", "doc_record"):
            check('id="%s"' % did in page, "doc panel %s missing" % did)
        check("renderBars" in page and "setBar" in page, "bar-meter renderer missing")
        check("meanTactic" in page and "meanBias" in page, "weight aggregation missing")
        check("flashDocs" in page, "doc-flash hook missing")
        for bid in ("qb_focus_fire", "qb_xp", "qb_rank", "qb_eps", "qb_deaths",
                    "qb_fl", "qb_fr", "qb_fb", "qb_door", "qb_hunt", "qb_amb", "qb_bias_sg"):
            check('id="%s"' % bid in page, "bar %s missing" % bid)
        for tid in ("qv_focus_fire", "qv_xp", "qv_eps", "qv_fl"):
            check('id="%s"' % tid in page, "bar value %s missing" % tid)
        check('id="dir_tic"' in page and 'id="dir_hits"' in page
              and 'id="dir_kills"' in page and 'id="dir_rank"' in page,
              "DIRECTOR status row missing")
        check("docglow" in page and "blink" in page, "terminal animations missing")
        check("max-width:1180px" in page, "full-width wrap missing")
    finally:
        httpd.shutdown()


def test_file_replay(tmpdir: str) -> None:
    lpath = os.path.join(tmpdir, "replay_live.json")
    hpath = os.path.join(tmpdir, "replay_hist.jsonl")
    with open(lpath, "w", encoding="utf-8") as f:
        json.dump({"ts": 1.0, "tic": 5, "buddy_skill": 3, "buddy_skill_name": "professional",
                   "rows": [], "events": ["12:eased:buddy:2"]}, f)
    with open(hpath, "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 1.0, "ep": 0, "eps": 0.2, "skill": 3, "r": 0.0,
                            "surv": 0.0, "hp": 100, "act": None,
                            "wt": {"shotgun": {"deaths": 1, "tactics": {}, "weapon_bias": {}}}}) + "\n")

    state = dash.DashboardState(live=None)
    dash_config = config
    old_lp, old_hp = dash_config.LIVE_STATE_PATH, dash_config.STATE_HISTORY_PATH
    dash_config.LIVE_STATE_PATH, dash_config.STATE_HISTORY_PATH = lpath, hpath
    try:
        snap = state.state_json()
        check(snap.get("buddy_skill") == 3, "replay snapshot skill wrong: %r" % snap.get("buddy_skill"))
        rows = state.history_rows()
        check(len(rows) == 1 and rows[0]["skill"] == 3, "replay history wrong: %r" % rows)
        # Corrupt/missing file -> empty dicts, never a crash.
        dash_config.LIVE_STATE_PATH = os.path.join(tmpdir, "nope.json")
        dash_config.STATE_HISTORY_PATH = os.path.join(tmpdir, "nope.jsonl")
        check(dash.DashboardState(live=None).state_json() == {}, "missing snapshot file should yield {}")
        check(dash.DashboardState(live=None).history_rows() == [], "missing history file should yield []")
    finally:
        dash_config.LIVE_STATE_PATH, dash_config.STATE_HISTORY_PATH = old_lp, old_hp


class FakeLink:
    """Scripted stand-in for DirectorLink.request()."""

    def __init__(self, replies: dict | None = None):
        self.replies = replies or {}
        self.sent: list[str] = []

    def request(self, line: str) -> str:
        self.sent.append(line)
        if line in self.replies:
            return self.replies[line]
        return "ok"


def test_curriculum_ladder() -> None:
    link = FakeLink()
    cur = SkillCurriculum(link)
    check(cur.level == 0 and cur.pushed == 0, "curriculum starts at 0/0")

    # Episode progression: 5 episodes -> level 1 (rookie -> amateur).
    check(level_for_episode(0) == 0 and level_for_episode(4) == 0,
          "level_for_episode early boundaries wrong")
    check(level_for_episode(5) == 1 and level_for_episode(9) == 1,
          "level_for_episode mid boundaries wrong")
    check(level_for_episode(25) == 4 and level_for_episode(999) == config.SKILL_MAX,
          "level_for_episode clamp wrong")

    check(cur.update_for_episode(4) is False, "no push expected below threshold")
    check(cur.update_for_episode(5) is True, "push expected at episode 5")
    check(cur.level == 1 and cur.pushed == 1, "level/pushed after first push: %s/%s" % (cur.level, cur.pushed))
    check(link.sent == ["buddy skill=1"], "push line wrong: %r" % link.sent)

    # Engine auto-lesson lands ABOVE the policy: reconcile absorbs it and the
    # policy never demotes an engine-earned rank.
    cur.reconcile(3)
    check(cur.level == 3, "reconcile should absorb engine rank 3")
    check(cur.update_for_episode(10) is False, "policy must not demote engine-earned rank")

    # Manual push clamps and reports errors.
    check(cur.push(9) is True and cur.level == config.SKILL_MAX, "push clamp wrong")
    bad = SkillCurriculum(FakeLink({"buddy skill=1": "err"}))
    check(bad.push(1) is False and bad.last_error == "err", "push error path wrong")


def test_event_label_tolerance() -> None:
    # The RL agent's parser must IGNORE the new ladder events (return None)
    # without crashing, and must keep parsing the classic hit/kill labels.
    check(parse_label("907:rankup:buddy:2") is None, "rankup label should be ignored by the agent parser")
    check(parse_label("908:eased:buddy:1") is None, "eased label should be ignored by the agent parser")
    check(parse_label("905:kill:imp:pistol") is not None, "kill label broke")
    check(parse_label("906:hit:shotgun:shotgun:21") is not None, "hit label broke")
    # The mirror constant must match the engine's NEM_BUDDY_XP.
    check(config.BUDDY_XP_PER_RANK == 60, "BUDDY_XP_PER_RANK must mirror NEM_BUDDY_XP (60)")


def test_param_feed() -> None:
    """Phase 9.17: /state must carry the live parameter-change feed and the
    history rows must stay diffable (the file-replay fallback re-derives it)."""
    old_lp, old_hp = config.LIVE_STATE_PATH, config.STATE_HISTORY_PATH
    with tempfile.TemporaryDirectory() as tmpdir:
        config.LIVE_STATE_PATH = os.path.join(tmpdir, "pf_live.json")
        config.STATE_HISTORY_PATH = os.path.join(tmpdir, "pf_hist.jsonl")
        old_hist_every = config.HISTORY_APPEND_EVERY
        config.HISTORY_APPEND_EVERY = 0.0          # force a history row per feed
        try:
            live = LiveState()
            _feed(live, _synthetic_obs(), times=1)  # arms baselines
            check(live.snapshot.get("param_feed") == [], "fresh feed must be empty")
            # 1) tactic weights move (1 Hz diff) ...
            obs2 = _synthetic_obs()
            obs2["nemesis"]["rows"][0]["tactics"]["chase"] = 1.9
            obs2["nemesis"]["rows"][0]["weapon_bias"]["pistol"] = 0.5
            obs2["nemesis"]["rows"][0]["deaths"] = 4
            # 2) ... and scalars move (10 Hz poll): rank, xp, epsilon.
            obs2["nemesis"]["buddy_skill"] = 3
            obs2["nemesis"]["buddy_xp"] = 55		# still < 60
            shim = dash._LiveShim()
            shim.epsilon_override = live.snapshot["epsilon"]   # pin epsilon
            live.update(shim, obs2, None)
            feed = live.snapshot.get("param_feed", [])
            kinds = {(d["kind"], d.get("name")) for d in feed}
            for want in (("tactic", "chase"), ("bias", "pistol"),
                         ("deaths", None), ("rank", None), ("xp", None)):
                check(want in kinds, "param_feed missing %r: %r" % (want, kinds))
            check(("eps", None) not in kinds, "pinned epsilon must not look like a move")
            chase = next(d for d in feed if d["kind"] == "tactic" and d["name"] == "chase")
            check(chase["old"] == 1.42 and chase["new"] == 1.9, "tactic delta values wrong: %r" % chase)
            check(chase["type"] == "shotgun" and "ts" in chase, "tactic delta needs type+ts")
            rankd = next(d for d in feed if d["kind"] == "rank")
            check(rankd["old"] == 2 and rankd["new"] == 3, "rank delta wrong: %r" % rankd)
            # No change -> no new records (no churn).  Feed the SAME shim so
            # epsilon stays pinned (a fresh _LiveShim would look like a move).
            n_before = len(live.snapshot["param_feed"])
            for _ in range(3):
                live.update(shim, obs2, None)
            check(len(live.snapshot["param_feed"]) == n_before, "unchanged obs grew the feed")
            # Epsilon is also a string jev pulls — its own record when it moves.
            shim.epsilon_override = 0.15
            live.update(shim, obs2, None)
            epsd = next(d for d in live.snapshot["param_feed"] if d["kind"] == "eps")
            check(epsd["new"] == 0.15, "eps delta wrong: %r" % epsd)
        finally:
            config.LIVE_STATE_PATH, config.STATE_HISTORY_PATH = old_lp, old_hp
            config.HISTORY_APPEND_EVERY = old_hist_every


def test_param_deltas_pure() -> None:
    """param_deltas() is the shared diff: records exactly the moves."""
    prev = {"shotgun": {"deaths": 1, "tactics": {"chase": 1.0, "hold": 1.0},
                        "weapon_bias": {"pistol": 0.0}},
            "imp": {"deaths": 0, "tactics": {}, "weapon_bias": {}}}
    cur = {"shotgun": {"deaths": 2, "tactics": {"chase": 1.5, "hold": 1.0},
                       "weapon_bias": {"pistol": -0.25, "shotgun": 0.1}},
           "imp": {"deaths": 0, "tactics": {"ambush": 0.8}, "weapon_bias": {}}}
    out = param_deltas(prev, cur)
    got = {(d["kind"], d["type"], d.get("name")) for d in out}
    check(("tactic", "shotgun", "chase") in got, "chase move missing: %r" % got)
    check(("tactic", "imp", "ambush") in got, "new-type tactic move missing")
    check(("bias", "shotgun", "pistol") in got, "bias move missing")
    check(("bias", "shotgun", "shotgun") in got, "new bias key missing")
    check(("deaths", "shotgun", None) in got, "deaths diff missing")
    check(("tactic", "shotgun", "hold") not in got, "unchanged tactic must not appear")
    for d in out:
        check(d["old"] != d["new"], "no-op delta leaked: %r" % d)
    check(param_deltas(prev, prev) == [], "identical maps must diff to []")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        test_endpoints_and_ui(tmpdir)
        test_file_replay(tmpdir)
    test_scoreboard_stats()
    test_curriculum_ladder()
    test_event_label_tolerance()
    test_param_deltas_pure()
    test_param_feed()
    if failures:
        print("DASHBOARD FAIL:")
        for f_ in failures:
            print("  -", f_)
        return 1
    print("DASHBOARD PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
