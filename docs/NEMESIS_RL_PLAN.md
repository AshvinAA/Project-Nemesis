# PROJECT NEMESIS — RL ROADMAP & PROGRESS TRACKER

> This is the working plan for the **Q-learning Shotgun Guy** ("Jev learns to fight").
> It supersedes the original brief's clean-room design where the repo has already
> solved the problem. **Progress log is at the bottom — append an entry after every
> finished task.** Decision ledger in §10.

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
- [ ] **0.3 Rebuild engine** — **BLOCKED on this machine**: no MSVC/CMake/MinGW installed (the existing `build/` tree was generated on a different machine — paths reference `C:\Users\SHAHRIAR`). Fix is compiled-in as soon as the engine is rebuilt on a machine with the toolchain (VS 2022 BuildTools + CMake + SDL3 SDK, per the cheat sheet in §11). Untracked-listener fix is 3 lines; low compile risk.
- [ ] **0.4 Live smoke test:** launch → connect → observe → rude disconnect → reconnect → observe answers. (Can run against the CURRENT exe to baseline the wedge, but the fix only takes effect after 0.3.)

**Done =** a fresh game survives client churn; every later iteration cycle is restart-free.

## 2. Phase 1 — Nerd-glue loop (Python agent, zero learning) **[x]**

- [x] **1.1 Scaffold `nemesis/` package:** `config.py` (all knobs), `engine.py` (`DirectorLink` TCP client: line protocol, clean disconnects, reconnect), `rl_agent.py` (10 Hz loop; logs `nemesis/log/obs.jsonl` + `nemesis/log/acts.jsonl`).
- [x] **1.2 Single-nemesis discipline:** finds the `shotgunguy` row in `monsters[]`; when none alive → `spawn type=shotgun count=1` with a respawn cooldown (its death = episode boundary).
- [x] **1.3 Policy = hardcoded `act order=chase ids=<id>`**, refreshed on a cadence AND immediately on id change; explicit `for=35` (~1 s) so a dead agent never leaves stale directives.
- [x] **1.4 Offline self-test** (`nemesis/test_selftest.py`, run `python -m nemesis.test_selftest`): mock TCP engine replays a scripted observe stream (nolevel → empty → roster changes → abrupt disconnect → respawn). **PASS** (spawn cooldown, vocabulary-clean orders, current-ids-only, immediate retarget on id change, reconnect, 2 episodes, coherent logs). The test caught one real bug pre-live: id changes originally waited for the refresh cadence — fixed to retarget immediately.

**Done =** 15 min live play: one Shotgun Guy persists, respawns on death, obeys orders; `obs.jsonl` holds full spawn→fight→death→respawn episodes.

## 3. Phase 2 — State compiler + reward plumbing **[ ]**

- [ ] **2.1 `state.py`:** the 576-cell state from observe fields (dist on `d_player`, `see_player`, angle bucket vs player angle, cover from regions/links, hp bracket vs spawnhealth 30, hp-delta → hit-recently). Deterministic, unit-testable.
- [ ] **2.2 `events.py`:** parse the engine event ring from observe (dedupe by seq, handle ring wrap + overflow).
- [ ] **2.3 Engine tweak: widen NEM event ring 16 → 64** (prevents dropped events under heavy fire; cheap, preserves reward semantics).
- [ ] **2.4 Episode bookkeeping** (tics survived, dmg dealt/taken per episode) + replay tool that prints an episode timeline.

**Done =** every state hash + event list reconstructible offline; replay prints a readable episode timeline.

## 4. Phase 3 — Q-learning core, offline first **[ ]**

- [ ] **3.1 `qtable.py`:** 576×8, ε-greedy, agreed update, α 0.3→0.1 floor, ε 0.2→0.05 floor over ~30 eps; persist `nemesis/rl/qtable.json` (version + map id header).
- [ ] **3.2 Offline trainer** over recorded `obs.jsonl` (replay recorded actions; verify update math + terminal handling).
- [ ] **3.3 Sanity tests:** Q bounded, terminal updated exactly once, schedules monotone, reward-per-episode trends up on synthetic data.

**Done =** offline convergence visible in ~10–15 synthetic episodes before any live training.

## 5. Phase 4 — Live write-back (policy drives the Shotgun Guy) **[ ]**

- [ ] **4.1 Action→protocol mapping:** advance/retreat/strafe-* → `act order=chase|fallback` + `x=/y=` offsets; take-cover → `hold` at cover region; flank-* → `flank_left|flank_right`; stand-and-shoot → `focus_fire`. Always `ids=<current id>`.
- [ ] **4.2 Episode end handling:** on Shotgun Guy death → Q-terminal update (−100 + 2·dmg + 0.1·tics) → respawn; on player death → +150.
- [ ] **4.3 No-stat-buffs audit:** confirm attack gates untouched; only positioning changes.

**Done =** visible dumb→smart progression across deaths; observe `order` field matches agent's sends.

## 6. Phase 5 — In-engine HUD metrics **[ ]**

- [ ] **5.1 Extend `nemesis` line parser (p_ai_llm.c:841) with `hud=ep=<n>,eps=<f>,last_r=<f>,surv_avg=<s>,act=<name>`.**
- [ ] **5.2 Draw via `C_Printf` (c_console.c:119) at ~1 Hz.**
- [ ] **5.3 Agent piggybacks hud block on every `act` line** (no extra round-trip).

**Done =** video shows episode count, ε, last reward, survival trend, current action — live in-engine.

## 7. Phase 6 — Controlled training + A/B evidence **[ ]**

- [ ] **6.1 `nemesis/train.py`:** fresh launch (PowerShell Start-Process — `cmd start` hangs!), wipe qtable + `nemesis_memory.dat`, N episodes, save logs.
- [ ] **6.2 Metrics + plots:** dmg-per-death, survival tics, action distribution per episode.
- [ ] **6.3 A/B:** ε=1.0 random policy vs trained table, same map/day/loadout.

**Done =** the demo curve: survival + damage rising over 15–30 episodes.

## 8. Phase 7 — Demo video **[ ]**

- [ ] Reset-on-camera, narrate HUD (ep/ε/reward), fight at ep 1 vs ep ~25 vs random control.

## 9. Out of scope / non-goals (re-confirmed)

- No DNN, no stat buffs, no C-embedded learning, no overlay tool (HUD via `C_Printf`).
- `jev/` bridge untouched and OFF during RL sessions (single-client listener).
- Map pinned for training (geometry-dependent buckets); map id stored in qtable header.

## 10. Decision ledger

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

## 11. Run cheat sheet (from HANDOFF.md — verified)

```sh
# Build engine (MSVC via VS BuildTools CMake)
CMAKE="/c/Program Files (x86)/Microsoft Visual Studio/2022/BuildTools/Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe"
"$CMAKE" -B BuddyDoom/build -S BuddyDoom -A x64
"$CMAKE" --build BuddyDoom/build --config Release --target buddydoom

# Launch game (PowerShell! cmd start hangs the calling shell)
powershell -Command "Start-Process -FilePath 'BuddyDoom\run\buddydoom.exe' -ArgumentList '-iwad','freedoom1.wad','-warp','1','1','-skill','3','-aidirector','31666' -WorkingDirectory 'BuddyDoom\run'"

# Probe protocol (close properly or the listener wedges — pre-0.1)
node -e "const net=require('net');const s=net.connect(31666,'127.0.0.1',()=>s.write('observe\n'));let b='';s.on('data',d=>{b+=d;if(b.includes('\n')){console.log(b.slice(0,300));s.end();process.exit(0)}});"

# Reset learned state
rm BuddyDoom/run/nemesis_memory.dat
```

---

## 12. PROGRESS LOG (append after every finished task)

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
