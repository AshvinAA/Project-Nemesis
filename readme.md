# Project Nemesis

**A real-time reinforcement-learning enemy for DOOM.**

**Classic DOOM enemies are lobotomized.** The shotgun guy's `A_Chase` rolls random chances to walk toward you and occasionally stop and fire — same animation, same decision, every encounter for thirty years. Project Nemesis rips that function out and replaces it with a learning agent people call *jev*. He starts **almost useless** — barely aims, dies in one opening blast — and every time you kill him, an external RL process reads what killed him and he comes back **tactically smarter**: better cover, better flanking, better retreat timing. The point isn't to make him harder — it's to prove **real-time tactical learning against a human is possible**, with **no stat buffs** anywhere in the loop.

<!-- TODO: drop a banner screenshot here -->
<!-- TODO: drop a demo video link/badge here when the 16–20 s take is ready -->

---

## The problem

DOOM's shotgun guy is a clockwork idiot. `A_Chase` (p_enemy.c) picks a random chance each tic to walk toward you and a separate chance to stop and shoot. He never reacts to how you're killing him. He never changes his mind. He is, in every meaningful sense, the same enemy on your hundredth fight as on your first.

That's fine for a game from 1993. It's boring for anything trying to teach itself to win.

**Project Nemesis replaces one enemy's brain with a learning one.** The shotgun guy becomes je — a persistent nemesis memory of per-monster-type tactic weights and weapon-bias, ground-truth hit/kill events, and a runtime profile that watches how you kill him and feeds that back into the table. He **does not** get more health, more damage, or more speed. His numbers are identical at rookie and at legend. What changes is the only thing that was ever going to matter: **the decisions**.

---

## What actually learns (and how you watch it)

The engine keeps a small per-type table in C (`files/p_nemesis.c`): one weight per tactic order (chase, hold, fallback, flank_left, flank_right, ambush, focus_fire, use_door) plus per-weapon bias, plus death counters and a 64-event ground-truth ring. It's persisted to `nemesis_memory.dat` so learning survives a restart. A Python side process (`nemesis/`) owns the director listener on TCP `:31666`, compiles a 576-state observation every poll, runs a Q-table (576 × 8 actions, ε-greedy), and votes its current tactic into the engine's weighting table at about 2 Hz via `nemesis propose=<type> tactic_weight=<order>:<delta>`.

The things that move the weights are the same things that kill monsters in the game:

- **Tactic weights** — a squad member that dies *executing* a tactic sheds weight from it; the others get a tiny spread. What kills them teaches.
- **Weapon bias** — every player hit biases the source weapon upward; the shotgun that kills you earns fear. Hits drip, kills punch.
- **The buddy's own ladder** — a hostile training buddy absorbs post-armor damage you deal him; every 60 of that is one rank; if he kills you, he eases off one. That's the visible "he's getting harder" arc, and it is **only** rank, not health/damage/speed.

Three dashboards watch this happen, all of them real and running now:

- **The console** on `:8787`: phosphor-green query documents with animated bar meters for `[FIRING]` (hold_fire, press_chase, hold_ground, fear_shotgun), `[GOAL]` (xp_to_rank, rank, epsilon, squad_deaths, rank_ups), `[DODGE]` (dodge_left, dodge_right, back_off, door_run), and `[MOVEMENT]` (hunt, ambush) plus a per-type weight heatmap. A standing-order line and a DIRECTOR status row ride the top.
- **The parameter feed** — every learned variable jev changes, itemized, newest first, in plain English ("you hit the shotgun guy for 18 (shotgun)", "▲ JEV RANKED UP → amateur — he trained on the beating you gave him").
- **The in-game HUD** — `[nemesis] ep=N,eps=F,r=F,surv=F,sk=N,act=NAME` every second, plus a `[buddy] rank N/4 (name)` line in hostile mode. No overlay tool, no extra binaries — just the console `C_Printf` path the engine already has.

<!-- TODO: insert a screenshot of the DOCUMENTS console here -->
<!-- TODO: insert a screen of the in-game HUD here -->

---

## Tech stack

- **Engine** — DOOM (BuddyDoom fork, SDL3), C, built with GCC 16.2 + CMake 3.31.6 via the no-admin portable toolchain in `tools/`.
- **Learning / agent** — Python (stdlib + numpy for the Q-table), 576-state tabular Q-learning, ε-greedy with α/ε decay, persist to `nemesis/rl/qtable.json`.
- **Dashboard** — Python stdlib `http.server`, zero dependencies, embedded dark console with vanilla JS. Runs standalone from files or live against the engine.
- **Protocol** — newline-delimited TCP loopback `:31666`, single client, human-readable engine commands (`observe`, `act order=<o> ids=<csv> for=<n>`, `spawn type=<t> count=<n>`, `nemesis propose=...`). All acked `ok`.

---

## Getting started

**Build** (Windows, no admin — portable toolchain on `F:`):

```sh
cmd /c "F:\Project-Nemesis\tools\build_buddydoom.bat"
```

That configures `BuddyDoom/build` with MinGW Makefiles on the first run and builds the `buddydoom` target. If you're on a machine with actual MSVC + CMake you can use the CMake toolchain instead — the repo's `BuddyDoom/` tree is a normal CMake project — but on this machine the portable MinGW path is the one that works (the `C:` drive is 99% full and VS BuildTools won't install).

**Run the game with a hostile nemesis buddy** (one-shot, after the build):

```sh
powershell -Command "Start-Process -FilePath 'BuddyDoom\run\buddydoom.exe' -ArgumentList '-iwad','freedoom1.wad','-warp','1','1','-skill','3','-aidirector','31666','-buddyhostile' -WorkingDirectory 'BuddyDoom\run'"
```

The buddy auto-respawns ~5 s after death and you get a shotgun + full shells every (re)spawn in hostile mode. `-nomonsters` keeps the lobby quiet — in that mode the nemesis squad is the only thing to hit.

**Launch everything at once** — just double-click `Nemesis.bat`. Default mode starts the hostile game + a minimized live dashboard + opens the browser at `http://127.0.0.1:8787`. Mode switches are one word:

```
Nemesis.bat          game + live dashboard + browser (hostile buddy)
Nemesis.bat gameonly just the game
Nemesis.bat normal   ally buddy + monsters + dashboard
Nemesis.bat agent    game + RL training agent + its dashboard
Nemesis.bat dashboard dashboard only (replays last session's files)
Nemesis.bat kill     close everything
Nemesis.bat reset    wipe nemesis_memory.dat (reset all = + qtable)
```

**Or run the dashboard alone** (no game, no agent) to inspect a past session:

```sh
python -m nemesis.dashboard
# or side-by-side beside the 640×400 game window:
#   open http://127.0.0.1:8787/?compact=1
```

**Run the RL agent live** (game must be running with `-aidirector 31666` — the agent owns that slot, so don't also run `--live`):

```sh
python -m nemesis.rl_agent
# A/B control: pure random policy
python -m nemesis.rl_agent --epsilon 1.0
# Start clean
python -m nemesis.rl_agent --fresh
```

<!-- TODO: after the demo take is cut, drop the actual URL here and a one-line how-to-capture -->

---

## What you'll see on a good run

Early episodes: jev doesn't react to much. The shotgun guy runs at you, stops, fires from the doorway. You take him in one opening blast and he respawns exactly as clueless.

A few episodes in: the dashboard's `[MOVEMENT]` hunt bar drifts, `[DODGE]` dodge_left/right start moving off neutral 1.00, and `fear_shotgun` ticks up after you land a couple shots. On the next spawn the shotgun guy doesn't just charge — he keeps distance, flanks once, retreats when you're prone. The `[nemesis]` HUD line shows `sk=1`, then `sk=2`.

Late: he's punishing your mistakes — holding position, pressing when you reload, retreating when you angle him — and the `[GOAL]` xp_to_rank bar is climbing on every shotgun hit. You are training him in real time, and the only thing that changed was his decisions.

---

## Testing

Three suites live right now, all runnable from the repo root:

```sh
python -m nemesis.test_dashboard   # dashboard UI + replay contract
python -m nemesis.test_selftest    # full agent loop against a mock TCP engine
python -m nemesis.test_phase2      # state compiler, event cursor, replay
```

`test_selftest` is the real one for the agent — it boots a mock engine that replays a scripted observe stream (nolevel → empty → rostered shotgun guy → abrupt disconnect → reconnect → roster change → death → respawn) and asserts the whole loop: spawn cooldown, vocabulary-clean orders, current-ids-only, immediate retarget on id change, reconnect survival, and (now) that the agent actually votes tactic weights into the engine via `nemesis propose=`. It ran in ~2 s last time. The mock engine is a real TCP server in Python — this is the loop, not a unit-test mock of the loop.

---

## Design notes / non-negotiables

- **No stat buffs.** Health, damage, speed, ammo are untouched by the learning path. The only thing that changes is decision-making. If a PR touches `health`, `damage`, `speed`, or weapon damage on the act path, it is wrong — there's a grep audit in the plan for that.
- **External Python process.** Learning runs in `nemesis/`, not in C. C owns the table and the events; Python owns the Q-table, the policy, and the propose stream. They talk over the existing engine protocol.
- **One client per listener.** The engine's `:31666` dir accepts exactly one TCP client. The agent, the `--live` dashboard, and any manual probe are mutually exclusive for that slot. Running two of them wedges the engine (the listener re-accept fix in p_ai_llm.c prevents the wedge, but you still only get one).
- **Hostile buddy persistence.** On restart the buddy remembers his skill level from `nemesis_memory.dat`; killing him and backing out doesn't erase him. `Nemesis.bat reset` wipes it.
- **Map-pinned training state.** The 576-state grid depends on map geometry (cover links, region distances); the Q-table stores the map id in its header. Training state for one map is not portable to another without a reset.

---

## Documentation

- `docs/NEMESIS_RL_PLAN.md` — the working roadmap. Full phase-by-phase progress log at the bottom; decision ledger in §12 with every locked choice and its revert knobs. **This is the file to read before changing anything** — it has the verified protocol facts, the state that actually exists, and what broke and why.

---

## Contributing

Start by reading `docs/NEMESIS_RL_PLAN.md`. If you're touching the learning loop, run the three test suites first and leave them green. If you're touching the engine, build and don't ship a wedge — the listener re-accept path is the one to keep clean.

The repo is small enough that the right place to look before changing anything is the source itself. The plan is the second place.

<!-- TODO: license — pick a real one once there's a release worth licensing -->
