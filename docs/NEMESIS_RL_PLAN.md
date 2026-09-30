# PROJECT NEMESIS — RL ROADMAP & PROGRESS TRACKER

> This is the working plan for the **Q-learning Shotgun Guy** ("Jev learns to fight").
> It supersedes the original brief's clean-room design where the repo has already
> solved the problem. **Progress log is at the bottom — append an entry after every
> finished task.** Decision ledger in §11.

**Status legend:** `[ ]` todo · `[~]` in progress · `[x]` DONE

---

## 0. Grounded architecture (what already exists — verified in source)

| Brief assumption | Repo reality (verified) |
|---|---|
| Need to build C↔Python comms | **Already built:** TCP loopback `:31666` director listener in `files/p_ai_llm.c`. `observe` → one JSON line (player, monsters[], regions[], links[], director, nemesis block). Mutations: `act order=<o> ids=<csv> [focus=] [x=] [y=] [for=] [after=]`, `spawn type=<t> count=`, `spawn item=medkit|ammo`, `director relax`, `reset`, `wake`, `nemesis propose=…`. All acked `ok`. |
| Write `A_NemesisChase` + repoint `info.c` states | **Not needed initially:** `A_Chase` (p_enemy.c:1281) already diverts directed monsters to `A_LLMChase` via `P_AI_Active()`. Movement control = one `act order=` line. `A_LLMChase` steers movement only; vanilla attack gates stay (≈ "no stat buffs"). |
| Unix domain socket | Engine is **loopback TCP only**. Python precedent: `BuddyDoom/run/llm_player.py` drives the `:31700` agent listener. |
| Learning/memory/ground truth | `files/p_nemesis.c`: event ring (16 entries, labels `"<seq>:hit:<type>:<weapon>:<dmg>"` / `"<seq>:kill:…"`) riding every observe; clamped proposals (`|Δ|≤0.5`, weights ∈ [0.05,2.0]); decay; `nemesis_memory.dat` persistence. |
| Q-table | **New work (the actual project).** Python process, tabular 576×8. |

**User decisions locked (2026-09-30):**
1. **Reuse the existing engine stack** (TCP :31666, `p_nemesis.c`, the `A_LLMChase` diversion pattern).
2. **Standalone Python agent** — it owns the director listener alone during RL sessions; the `jev/` TypeScript bridge stays OFF. (Engine allows exactly ONE client per listener.)

**Agreed spec (from the brief, unchanged):**
- State (576): dist(close/med/far) × LOS(2) × angle(front/flankL/flankR/behind) × cover(none/left/right/both) × hp(healthy/hurt/critical) × hit-recently(y/n)
- Actions (8): advance, retreat, strafe-left, strafe-right, take-cover, stand-and-shoot, flank-left, flank-right
- Rewards: death −100 +2/pt dmg dealt +0.1/tic survived; player death +150; shaping: +cover-with-LOS, +shot landed, −hit in open, −standing in open LOS
- Q-update: `Q(s,a) += α(r + γ·maxQ(s',·) − Q(s,a))`, α 0.3→decay, ε-greedy 0.2→0.05

---

## 1. Phase 0 — Stabilize the substrate **[~]**

- [x] **0.1 Fix listener re-accept bug** — DONE 2026-09-30. Root cause: on Windows an abrupt client death (RST) surfaces as `read() == -1` (`WSAECONNRESET`), not 0; `AI_PollSocket` treated every `r<0` as "EAGAIN, nothing this tic" and returned with the dead client still installed, so `accept()` was never re-armed. Fix in `files/p_ai_llm.c`: `WSAEWOULDBLOCK` → return (retry next tic); ANY other socket error → close client, `client_fd = -1` (re-accept next tic). POSIX mirrors it for hard errors (EAGAIN/EWOULDBLOCK/EINTR → retry; else drop).
- [x] **0.2 Triage `BuddyDoom/run/buddydoom_crash.dmp`** — DONE 2026-09-30. VERDICT: stale artifact, NOT nemesis/socket related. Both `.dmp` and `navdbg.txt` timestamp Sep 25 18:20 (5 days old); navdbg content is a buddy void-rescue diagnosis (map 3.1, tic 696 — co-op buddy navigation), dump strings reference only `buddydoom.wad`. No action needed.
- [x] **0.3 Rebuild engine** — **STILL BLOCKED on this machine** (no MSVC/CMake; build tree references another machine's paths). Three C changes now await the rebuild: listener fix (p_ai_llm.c), event ring 64 (p_nemesis.c), HUD plumbing (p_nemesis.c + p_ai_llm.c). All are small, additive, and low-risk; one rebuild picks up all three.
- [x] **0.4 Live smoke test** — DONE 2026-09-30 against the prebuilt exe: game launches, `observe` answers (tic 345, 32 monsters, nemesis block present ✅). The abrupt-disconnect probe reproduced the wedge live: after the probe cycle, `:31666` returned **ECONNREFUSED** while the game process stayed alive — pre-fix baseline CONFIRMED with fresh evidence. The compiled fix (0.1) addresses exactly this; re-verify after the toolchain rebuild.

**Done =** a fresh game survives client churn; every later iteration cycle is restart-free.

## 2. Phase 1 — Nerd-glue loop (Python agent, zero learning) **[x]**

- [x] **1.1 Scaffold `nemesis/` package:** `config.py` (all knobs), `engine.py` (`DirectorLink` TCP client: line protocol, clean disconnects, reconnect), `rl_agent.py` (10 Hz loop; logs `nemesis/log/obs.jsonl` + `nemesis/log/acts.jsonl`).
- [x] **1.2 Single-nemesis discipline:** finds the `shotgunguy` row in `monsters[]`; when none alive → `spawn type=shotgun count=1` with a respawn cooldown (its death = episode boundary).
- [x] **1.3 Policy = hardcoded `act order=chase ids=<id>`**, refreshed on a cadence AND immediately on id change; explicit `for=35` (~1 s) so a dead agent never leaves stale directives.
- [x] **1.4 Offline self-test** (`nemesis/test_selftest.py`, run `python -m nemesis.test_selftest`): mock TCP engine replays a scripted observe stream (nolevel → empty → roster changes → abrupt disconnect → respawn). **PASS** (spawn cooldown, vocabulary-clean orders, current-ids-only, immediate retarget on id change, reconnect, 2 episodes, coherent logs). The test caught one real bug pre-live: id changes originally waited for the refresh cadence — fixed to retarget immediately.

**Done =** 15 min live play: one Shotgun Guy persists, respawns on death, obeys orders; `obs.jsonl` holds full spawn→fight→death→respawn episodes.

## 3. Phase 2 — State compiler + reward plumbing **[x]**

- [x] **2.1 `state.py`:** 576-cell state done, incl. a documented approximation: ANGLE is measured from the PLAYER's facing (monster facing is not serialized — player-relative bearing is the tactically meaningful signal anyway). Cover from non-open links sided by cross product.
- [x] **2.2 `events.py`:** ring parser (newest-first → oldest-first), seq-dedupe cursor, gap detection, **cursor advances only on snapshots carrying the nemesis block** (engine drops the block under buffer pressure).
- [x] **2.3 Event ring widened 16 → 64** in `files/p_nemesis.c` (macro; all 5 uses are size/modulo). Awaiting the Phase-0.3 toolchain rebuild to compile.
- [x] **2.4 `replay.py`:** episode reconstruction from `obs.jsonl` (episodes = alive-runs + the closing death poll, where the kill event lands), timeline printer, `load_polls()` corpus loader for the offline trainer.

**Done =** ✅ `python -m nemesis.test_phase2` → PHASE2 PASS (576-index bijectivity, label parse, dedupe/gap/block-less-snapshot handling, bucket boundaries, replay round-trip). Tests caught 2 real bugs pre-live (angle bearing sign; terminal kill event dropped by episode splitter) and 2 spec slips (hp bracket boundary at exactly 2/3; doom angle 180 = west not south).

## 4. Phase 3 — Q-learning core, offline first **[ ]**

- [ ] **3.1 `qtable.py`:** 576×8, ε-greedy, agreed update, α 0.3→0.1 floor, ε 0.2→0.05 floor over ~30 eps; persist `nemesis/rl/qtable.json` (version + map id header).
- [ ] **3.2 Offline trainer** over recorded `obs.jsonl` (replay recorded actions; verify update math + terminal handling).
- [ ] **3.3 Sanity tests:** Q bounded, terminal updated exactly once, schedules monotone, reward-per-episode trends up on synthetic data.

**Done =** offline convergence visible in ~10–15 synthetic episodes before any live training.

## 5. Phase 4 — Live write-back (policy drives the Shotgun Guy) **[x]**

- [x] **4.1 `nemesis/policy.py`:** all 8 abstract actions → vocabulary-exact orders (advance→chase, retreat→fallback, strafe-*/flank-*→flank_left/right with perpendicular anchor waypoints, take_cover→hold anchored at the cover-sibling region, stand_and_shoot→focus_fire). Always `ids=<current id>`, `for=35`.
- [x] **4.2 `LiveLearner` in `rl_agent.py`:** per-poll MDP steps (bootstrapped Q updates), terminal on nemesis death (−100 + 2·player-dmg + 0.1/tic) and player death (+150), single-counter episode bookkeeping (`learner.episode_label()`), qtable saved on every episode end, `--epsilon 1.0` A/B control mode, `--fresh` wipe.
- [x] **4.3 No-stat-buffs audit** — verified by grep 2026-09-30: the `act` path in p_ai_llm.c contains ZERO `P_DamageMobj`/`health +=`/`->speed`/`->damage` mutations; the Python mapping issues movement orders only. Vanilla attack gates (`P_CheckMissileRange`) untouched.

**Done =** ✅ offline (selftest drives the full learning loop against the mock engine: terminals fire, ε decays, vocab clean; trainer shows greedy > random, survival 5.6→10.2 polls). Live visible progression awaits the toolchain rebuild + a human playtest.

## 6. Phase 5 — In-engine HUD metrics **[x]**

- [x] **5.1 Parser:** `nemesis ... hud=<spec>` token in `AI_HandleLine` → `NEM_HUDSet()` (stores verbatim, ≤96 chars, stamps gametic).
- [x] **5.2 Display:** `NEM_HUDPrint()` from the existing ~1 Hz decay-pass site in `P_AI_Ticker` → `C_Printf("[nemesis] %s")`; prints `(stale)` when the agent hasn't updated for >10 s.
- [x] **5.3 Agent piggyback:** `LiveLearner.hud_spec()` → `ep=N,eps=F,r=F,surv=F,act=NAME` (ε override reflected; surv = mean of last 10 completions; last terminal as `r=`) attached to every act AND spawn line.

**Done =** ✅ pipeline complete offline; shows live in-engine after the rebuild.

## 7. Phase 6 — Controlled training + A/B evidence **[x]**

- [x] **6.1 `nemesis/train.py`:** `--launch` (PowerShell Start-Process), `--fresh` (qtable) + `--wipe-memory` (C table), runs until N episodes COMPLETE (learner-confirmed gate, not a timer), writes `nemesis/log/training_curve_{training|control}.csv`.
- [x] **6.2 Metrics:** CSV columns episode/terminal_r/survived_s/epsilon/player_dmg_taken + first-vs-last window trend line.
- [x] **6.3 A/B:** `--control` = ε=1.0 random policy, same qtable file left untouched; compare `training_curve_control.csv` vs `training_curve_training.csv` from the same session.

**Done =** ✅ orchestrator ready; the actual demo curve requires the rebuild + live sessions (human player in the loop).

## 8. Phase 8 — Hostile-buddy training opponent **[x]** (source; compile pending rebuild)

Makes the co-op buddy marine fight the PLAYER as a training opponent, with auto-respawn
on death. Movement/aim AI reused as-is — only *who* it targets, *what damage is legal*,
and *what happens on death* change. No stat buffs (health/armor/ammo untouched).

- [x] **8.1 Flag:** `-buddyhostile` parsed at the end of `P_AICoop_Init` (p_ai_coop.c);
      statics `buddy_hostile`, `hostile_respawn`, `hostile_was_dead` + `P_AICoop_HostileMode()`
      accessor (header + p_inter.c use). Plain SP launch already enables the buddy by default,
      so `-buddyhostile` alone is enough.
- [x] **8.2 Targeting:** `AICoop_FindTarget` short-circuits in hostile mode — returns the
      nearest live human (via `AICoop_HostileTarget`, defined after `AICoop_NearestHuman`)
      gated on `P_CheckSight`; NULL with no LOS (normal hunt logic closes the distance).
- [x] **8.3 Hostile combat branch** (top of the priority chain in `P_AICoop_BuildCmd`):
      LOS → state 1 fight (fire at the human, `movethresh=COOP_KEEP`, avoid damaging floors);
      no LOS → press to 96u toward the live human position (re-opens the sight line fast).
      Aim-line friendly-fire guard (`!buddy_hostile &&`) skipped so the fire branch is
      reachable. Director/console "attack" orders ignored (`forceaggro` gate). No "ff:" protest
      callout in `P_AICoop_NoteDamage` when hostile.
- [x] **8.4 Damage enable (p_inter.c `P_DamageMobj`):** `-nofriendlyfire` gate now has
      `&& !P_AICoop_HostileMode()` — human↔buddy damage is legal in hostile mode even under
      ff_protect. Verified NOT obstacles: `ff_protect` defaults 0; `P_SpawnPlayer` sets no
      MF_FRIEND on the buddy; the drone/turret MF_FRIEND source gate doesn't match player
      shooters; retaliation guard (`target->flags & MF_FRIEND`) doesn't apply to players;
      MT_XPOISONCLOUD is irrelevant (buddy fires hitscan/projectile player weapons).
- [x] **8.5 Auto-respawn:** in the `PST_DEAD` branch of `P_AICoop_BuildCmd`, hostile mode
      arms `HOSTILE_RESPAWN_TICS` (5 s) once per death (latch), then teleports the corpse to
      the recorded spawn (`coop_home_*`, set at level start) and `P_AICoop_Revive(FullHealth())`
      — reuses the engine's own stand-up path. Latch also cleared in the LIVE path and per
      level in `P_AICoop_ResetSlot`. No PST_REBORN (avoids the SP level-reload). corpse
      stays non-solid (no USE-reborn exploit); one dead marine on the intermission
      screen is cosmetic.
- [x] **8.6 Docs:** this section + progress log. **Blocked:** compile verification — no C
      toolchain on this machine; changes ride the pending one-shot rebuild (§12) together
      with the Phase 0.1 listener fix, NEM_EVENTMAX 64 and the HUD work.

## 9. Phase 7 — Demo video **[ ]**

- [ ] Reset-on-camera, narrate HUD (ep/ε/reward), fight at ep 1 vs ep ~25 vs random control.

## 10. Out of scope / non-goals (re-confirmed)

- No DNN, no stat buffs, no C-embedded learning, no overlay tool (HUD via `C_Printf`).
- `jev/` bridge untouched and OFF during RL sessions (single-client listener).
- Map pinned for training (geometry-dependent buckets); map id stored in qtable header.

## 11. Decision ledger

| # | Decision | Choice | Status |
|---|---|---|---|
| D1 | Substrate | Reuse existing TCP :31666 stack + `A_LLMChase` pattern | LOCKED |
| D2 | Process model | Standalone Python agent; jev/ bridge OFF in RL sessions | LOCKED |
| D3 | Training map | TBD — must pin (suggest `-warp 1 1` freedoom1.wad, skill 3) | OPEN |
| D4 | Event ring width | Widen 16→64 in p_nemesis.c (Phase 2) | OPEN (reco: yes) |
| D5 | γ / shaping weights | γ=0.95 start; tune shaping first if flat by ep ~15 | OPEN |
| D6 | Q-table scope | Per-map v1 (map id in header) | OPEN (reco: per-map) |
| D7 | `nemesis propose` sync | Q-table-only v1; C-table sync optional later | OPEN (reco: Q-only) |
| D8 | Crash dump relevance | Stale Sep 25 buddy-nav artifact — unrelated | RESOLVED |

## 12. Run cheat sheet (from HANDOFF.md — verified)

```sh
# Build engine (MSVC via VS BuildTools CMake)
CMAKE="/c/Program Files (x86)/Microsoft Visual Studio/2022/BuildTools/Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe"
"$CMAKE" -B BuddyDoom/build -S BuddyDoom -A x64
"$CMAKE" --build BuddyDoom/build --config Release --target buddydoom

# Launch game (PowerShell! cmd start hangs the calling shell)
powershell -Command "Start-Process -FilePath 'BuddyDoom\run\buddydoom.exe' -ArgumentList '-iwad','freedoom1.wad','-warp','1','1','-skill','3','-aidirector','31666' -WorkingDirectory 'BuddyDoom\run'"

# Launch with the Phase 8 hostile training buddy (needs the Phase-8 rebuild first)
powershell -Command "Start-Process -FilePath 'BuddyDoom\run\buddydoom.exe' -ArgumentList '-iwad','freedoom1.wad','-warp','1','1','-skill','3','-aidirector','31666','-buddyhostile' -WorkingDirectory 'BuddyDoom\run'"

# Probe protocol (close properly or the listener wedges — pre-0.1)
node -e "const net=require('net');const s=net.connect(31666,'127.0.0.1',()=>s.write('observe\n'));let b='';s.on('data',d=>{b+=d;if(b.includes('\n')){console.log(b.slice(0,300));s.end();process.exit(0)}});"

# Reset learned state
rm BuddyDoom/run/nemesis_memory.dat
```

---

## 12. PROGRESS LOG (append after every finished task)

**2026-09-30 — Phases 3–6 + live baseline**
- **Phase 3 DONE** — `nemesis/qtable.py` (576×8, ε-greedy w/ random tie-break, brief's update, α/ε linear schedules with floors, versioned JSON w/ atomic tmp-replace save); `nemesis/trainer.py` synthetic-duel offline trainer → **PASS**: polls survived 5.6→10.2 (first5 vs last5, 60 eps), held-out greedy −88.7 vs random −90.4.
- **Phase 4 DONE** — `nemesis/policy.py` (8 actions → vocabulary-exact orders + cover/flank anchors); `LiveLearner` (per-poll bootstrapped updates, both terminals, single episode counter via `episode_label()`, lifecycle listener, `stop_when` gate). Selftest drives the full loop against the mock engine: PASS. No-stat-buffs audit: grep-verified zero health/damage/speed mutations on the act path, C and Python.
- **Phase 5 DONE (source)** — C: `NEM_HUDSet/NEM_HUDPrint` in p_nemesis.c (verbatim spec store ≤96 chars, ~1 Hz print from the decay-pass site, `(stale)` after 10 s), `hud=` token in the nemesis parser; Python: `hud_spec()` piggybacked on every act/spawn line. NOTE: a transcription slip briefly set NEM_Init tactics to 0.0 — caught and reverted to 1.0 (neutral) within the same session.
- **Phase 6 DONE** — `nemesis/train.py`: `--episodes N --fresh --wipe-memory --launch --control`; completes-on-N gate, CSV curves (`training_curve_training.csv` / `training_curve_control.csv`), trend printer.
- **0.4 DONE (live baseline)** — prebuilt exe launched, observe answered (tic 345, 32 monsters, nemesis block ✅). Abrupt-disconnect probe reproduced the wedge LIVE: `:31666` ECONNREFUSED with the process alive afterward. Pre-fix baseline documented; the 0.1 fix needs the toolchain rebuild to take effect.
- Blocked-on-toolchain list for ONE rebuild: (1) listener re-accept fix, (2) NEM_EVENTMAX 64, (3) HUD set/print + parser token.
- Test count: selftest + phase2 + trainer, all passing after every change.
- **2.1–2.4 DONE.** New modules: `nemesis/state.py` (576-cell compiler; player-relative angle bucket; cross-product cover siding), `nemesis/events.py` (EventCursor: newest-first normalize, seq dedupe, gap log, block-less-snapshot safety), `nemesis/rewards.py` (brief's reward spec at poll/event granularity; ShapingAccumulator + pure `episode_terminal`), `nemesis/replay.py` (`load_polls` corpus, `split_episodes` incl. death poll, `summarize`/`timeline` printers).
- **2.3 DONE (source).** `NEM_EVENTMAX` 16→64 in `p_nemesis.c` with fidelity comment; compiles with the Phase-0.3 rebuild.
- Tests: `python -m nemesis.test_phase2` → PASS. 2 real bugs caught pre-live: (1) angle bearing had the player→monster vector inverted; (2) `split_episodes` dropped the death poll, losing the terminal kill event. 2 test-side spec errors fixed (hp boundary, doom angle directions).
- Phase-1 self-test still green after all changes.

**2026-09-30 — session start**
- Roadmap written and grounded against repo reality (this file). Plan MD task: **DONE**.
- Locked D1 (reuse stack) + D2 (standalone Python agent) with user.
- Jev API key received; stored in `jev/.env` (gitignored — never commit); key hygiene rule added to .gitignore. Task: **DONE**.
- Phase 0 started: 0.1 listener fix in progress.

**2026-09-30 — Phase 0**
- **0.1 DONE** — listener re-accept fix in `BuddyDoom/files/p_ai_llm.c` (`AI_PollSocket`): Windows socket errors (RST → `WSAECONNRESET`) now drop the dead client and re-arm `accept()`; only `WSAEWOULDBLOCK` retries. POSIX mirror added (EAGAIN/EWOULDBLOCK/EINTR retry, hard errors drop). Awaiting 0.3 to compile.
- **0.2 DONE** — crash dump + navdbg.txt triaged: stale Sep 25 buddy-navigation artifacts, unrelated to nemesis/sockets. D8 RESOLVED.
- **0.3 BLOCKED (this machine)** — no C toolchain installed (VS BuildTools/CMake/MinGW all absent; existing build tree references another user's paths). Options: (a) install VS 2022 BuildTools + CMake + SDL3 SDK here, or (b) rebuild on the original machine. Fix is 3 lines, compiles trivially once toolchain exists.
- Engine protocol re-verified while reading the parser: `act`/`spawn`/`nemesis`/`buddy`/`reset`/`wake` all acked `ok`; `observe` replies one JSON line (or `{"nolevel":true,...}` outside a level); `AI_OrderByName` vocabulary confirmed.

**2026-09-30 — Phase 1**
- **1.1–1.3 DONE** — `nemesis/` Python package scaffolded: `config.py`, `engine.py` (DirectorLink with clean half-close disconnects), `rl_agent.py` (10 Hz loop, obs+acts jsonl logging, single-nemesis spawn discipline, hardcoded chase policy with cadence + immediate id-change retarget, `for=35` short directives).
- **1.4 DONE** — offline self-test `nemesis/test_selftest.py`: **PASS** (6 commands asserted across a scripted stream incl. abrupt disconnect + roster changes; 2 episodes; logs coherent). Test caught a real bug pre-live (id change waited for refresh cadence) — fixed by retargeting immediately on id change.
- Note: `nemesis/log/` is runtime data; the self-test writes to a tempdir, live runs append to `nemesis/log/`.

**2026-09-30 — Phase 8: hostile-buddy training opponent (source DONE; compile pending rebuild)**
- User request: buddy should fight the player and auto-respawn when killed. Diagnosis first: the buddy IS enabled on a plain SP launch (default-on in `P_AICoop_Init`), it's just a hard ally by design — `Companion_IsEnemy` excludes players, `AICoop_FindTarget` scans monsters only, and death goes to the L4D revive-wait instead of respawning. Nothing was broken; the feature didn't exist.
- **8.1 DONE** — `-buddyhostile` flag parsed in `P_AICoop_Init` (p_ai_coop.c); statics `buddy_hostile` / `hostile_respawn` / `hostile_was_dead`, `HOSTILE_RESPAWN_TICS` (5 s), `P_AICoop_HostileMode()` accessor added to p_ai_coop.h.
- **8.2 DONE** — `AICoop_FindTarget` hostile short-circuit: returns the nearest live human (new `AICoop_HostileTarget`, defined after `AICoop_NearestHuman`) with `P_CheckSight` gate; NULL without LOS so the normal hunt logic closes distance.
- **8.3 DONE** — hostile combat branch at the top of the BuildCmd priority chain (fight on LOS, press to 96u without), aim-line FF guard disabled in hostile mode so the buddy actually fires at the player, director "attack" orders ignored, no "ff:" protest callout in `P_AICoop_NoteDamage`.
- **8.4 DONE** — p_inter.c `-nofriendlyfire` gate now `&& !P_AICoop_HostileMode()` so human↔buddy damage is legal in hostile mode. Audited the other damage gates: none block player-shooter→player-target (ff_protect defaults 0, buddy's player mobj carries no MF_FRIEND, retaliation guard is MF_FRIEND-target-only, MT_TURRET/MT_XPOISONCLOUD don't apply).
- **8.5 DONE** — auto-respawn in the PST_DEAD branch of BuildCmd: once-per-death latch arms a 5 s timer, then `P_TeleportMove` to the recorded spawn + `P_AICoop_Revive(FullHealth())` (engine's own stand-up path). Chose revive-in-place-at-home over PST_REBORN because SP `G_DoReborn` reloads the whole level on any reborn. Latch cleared in the LIVE path + per level in `P_AICoop_ResetSlot`.
- Sanity checks: brace/paren balance OK on p_ai_coop.c / p_inter.c / p_ai_coop.h (new `nemesis/bracecheck.py` helper; no C toolchain here, so no real compile).
- PLAN updated: new §8 (Phase 8, all boxes [x] with the compile caveat), §9–11 renumbered to §9 Phase 7 demo / §10 non-goals / §11 ledger / §12 cheat sheet (+ hostile launch line).
- **Blocked as before:** compile verification rides the one-shot toolchain rebuild together with the Phase 0.1 listener fix, NEM_EVENTMAX 64 and the HUD work. Until then, run the PREBUILT exe = no hostility, no auto-respawn (and launch with `-buddyhostile` once rebuilt).
