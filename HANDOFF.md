# PROJECT NEMESIS — AI HANDOFF DOCUMENT

> **Read this first.** This document is written for a future AI agent (or human)
> picking up this project cold. It captures: what this project IS, exactly what
> was built, every protocol fact verified against source, what is tested vs
> untested, what broke and why, and what to do next. Everything below was
> learned by doing — much of it the hard way.

**Last updated:** 2026-09-19 (session 1: full build + engine compile + live smoke test)

---

## 1. What this project is

**Project Nemesis** = BuddyDoom (id's 1993 DOOM on SDL3, forked from
`doclulleNPC/BuddyDoom`) whose monster director is driven by **Jev**, TypeSafe AI's
"System One" model — instead of the upstream local-Ollama LLM director.

The product goal: **an enemy AI that gets genuinely harder against *you specifically***
by remembering how you kill it — a persistent nemesis memory — plus real-time
adaptive difficulty. Not a smarter monster per se: a *director* that learns the player.

### 1.1 The core insight (why Jev)

Jev is not a chat LLM. It is a ~100 ms decision function:
`POST https://api.typesafe.ai/v1/systemone` with `{state, model:"jev-latest", questions}`
→ typed answers with **calibrated probabilities**. Three question primitives:

| Type | Returns | Notes |
|---|---|---|
| `noul` | `{noul: 0..1}` | P(yes). No separate confidence field. |
| `choice` | `{choice, probabilities, confidence}` | ≤255 options; confidence = how peaked the distribution is |
| `score` | `{score, legend, probabilities, confidence}` | 2–10 levels, described by **situations not degrees**; score = probability-weighted mean, can land between levels |

Key properties that shaped the whole design:
- **All questions in one call are evaluated in parallel and independently** — adding
  questions ≈ free. But each question sees ONLY the shared state, never other answers.
  → Ask *atomic* questions, compose decisions in ordinary code.
- **~70–500 ms end-to-end**, $0.042/MTok input, output free. The old Ollama path took
  seconds per prose decision → this is a 10–100× latency inversion.
- **Cannot hallucinate malformed output** — answers are constrained to the schema you sent.
- Every answer is calibrated: 0.9 means right ~90% of the time *over many calls*.
  → Safe to threshold on; enables evidence-gated learning.
- Text-only state (string/JSON object/array). No images. ~64k token budget total,
  state + longest question ≤ ~32k. Errors: 401 bad key, 422 validation, 429/529 → backoff.
- `jev-latest` is an alias (currently jev-1.13.0); **the response always tells you the
  model that answered — log it**, pin thresholds per version.

The two rules that make Jev smart in this project:
1. **Atomic questions, composed in code.** Never "what should the monsters do?" —
   ask 5 narrow ones, combine with thresholds/hysteresis in TS/C.
2. **Scores need situations, nouls need criteria.** "Moderately stressed" is unmatchable;
   "player below 40 hp, hit from 3 directions in the last 5 s" is.

### 1.2 The architecture in one diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                BuddyDoom (C, 35 Hz tic loop)                    │
│                                                                 │
│  A_LLMChase executes orders per-tic (movement; vanilla attacks) │
│  P_AI_RuleTactics / P_Director_PickType consume the learned     │
│  table NATIVELY (works with zero model calls)                   │
│                                                                 │
│  p_nemesis.c ── weight table + event ring + clamped proposals   │
│  p_inter.c ──── P_DamageMobj hooks = ground truth per hit       │
│  p_ai_llm.c ─── TCP :31666 (observe / act / spawn / nemesis)    │
│  g_agent.c ───── TCP :31700 (player agent, optional)            │
└───────────────┬─────────────────────────────────────────────────┘
                │ one client per listener; poll-based; never blocks
┌───────────────▼─────────────────────────────────────────────────┐
│                jev/ bridge (Node 20 + TypeScript)               │
│                                                                 │
│  GameLink (TCP) → StateCompiler (digests ≤4k tok) → JevClient   │
│  → DecisionComposer (thresholds+hysteresis+safety gates)        │
│  → protocol lines back to the engine                            │
│  LearningTracker (6-axis EWMA skill + bounded difficulty)       │
│  mock Jev (--mock) for full-pipeline offline runs               │
└───────────────┬─────────────────────────────────────────────────┘
                │ HTTPS, ~5 calls/s, ~700 tok each, ≈$0.75/hour
┌───────────────▼─────────────────────────────────────────────────┐
│                     Jev API (stateless judge)                   │
└─────────────────────────────────────────────────────────────────┘
```

**The tier principle (memorize this):** memory and ground truth live in the
**engine**; intelligence lives in the **bridge**; Jev is a **stateless judge**
between them. The bridge can only *propose* table changes; the engine **clamps**
them. Every tier degrades independently; nothing ever blocks the 35 Hz loop.

---

## 2. Repository map

```
Project-Nemesis/
├── readme.md                  ← project overview + quick start
├── HANDOFF.md                 ← THIS FILE
├── .gitignore                 ← SDL3 SDK + build dir + logs excluded
├── SDL3/                      ← SDL3 devel SDK 3.4.16 (VC build), NOT in git
│
├── docs/
│   ├── JEV_DIRECTOR_WORKFLOW.md   ← the full design doc (v1.2). §1–2 = research
│   │                               facts; §3–8 = original design; §9 = merge with
│   │                               the "nemesis" design; §10 = final architecture.
│   └── (upstream BuddyDoom docs — AGENT_CONTROL, MONSTER_AGENT_GUIDE, AIPLAYER,
│         DIRECTOR_MODES are the load-bearing ones)
│
├── jev/                       ← THE BRIDGE (all new, TypeScript, ESM)
│   ├── package.json           npm run dev | replay | replay:dry | test | typecheck
│   ├── tsconfig.json          strict, noUncheckedIndexedAccess, Node16 resolution
│   ├── README.md              runtime guide
│   ├── src/
│   │   ├── types.ts           Obs* shapes mirroring AI_Serialize; Jev req/resp;
│   │   │                      MONSTER_ORDERS/BUDDY_ORDERS/SPAWN_TYPES as const
│   │   │                      tuples (compile-time wire vocabulary); SPAWNHEALTH
│   │   │                      table for wounded-ness math
│   │   ├── config.ts          ALL knobs: cadence presets (aggressive/default/
│   │   │                      economy), thresholds, hysteresis, clamps, budget.
│   │   │                      dryRun=true when no TYPESAFE_API_KEY
│   │   ├── jevClient.ts       HTTP client: concurrency pool (4), 429/529 exponential
│   │   │                      backoff, abort timeout, per-call JSONL log
│   │   ├── gameLink.ts        TCP clients (reconnect, pending buffer, 100 ms poll),
│   │   │                      emitters: act/buddy/spawn/item/relax/nemesisPropose
│   │   ├── stateCompiler.ts   5 digest builders + budget guard (BudgetError)
│   │   ├── composer.ts        DecisionComposer: probabilities → exact lines
│   │   ├── learning.ts        LearningTracker: EWMA skill vector, event ingest,
│   │   │                      bounded difficulty controller (frustration-first)
│   │   ├── mockJev.ts         deterministic offline Jev (full-pipeline testing)
│   │   └── main.ts            wiring, cadence loops, replay mode, status ticker
│   └── test/                  15 tests (all passing), see §5
│
├── BuddyDoom/                 ← the engine fork (upstream + our Tier-0 patch)
│   ├── CMakeLists.txt         +dbghelp patch (see §6.3)
│   ├── build/                 CMake build tree (gitignored)
│   ├── run/                   staged binaries; ID0/ holds IWADs & configs
│   │   ├── buddydoom.exe      ← BUILT, includes nemesis code
│   │   └── ID0/freedoom1.wad  ← freely-licensed IWAD, 28 MB
│   ├── files/
│   │   ├── p_nemesis.c/.h     ← NEW: the Tier-0 nemesis module
│   │   ├── p_inter.c          ← MODIFIED: 2 hooks in P_DamageMobj
│   │   ├── p_ai_llm.c         ← MODIFIED: 4 changes (include, AI_Apply note,
│   │   │                      AI_Serialize nemesis block, nemesis parser + decay)
│   │   ├── p_ai_director.c    ← MODIFIED: spawn escalation via death counters
│   │   ├── i_system.c         ← MODIFIED: NEM_Quit() in I_Quit
│   │   ├── Makefile.msvc      ← MODIFIED: +p_nemesis.obj
│   │   └── Makefile.in        ← MODIFIED: +p_nemesis.c/.h in sources
│   └── docs/ …
└── (root) readme.md was "a game bruv" — now real; git history is thin (1 commit)
```

---

## 3. What was DONE (and works)

### 3.1 Bridge (`jev/`) — complete, tested, runs offline
- **JevClient**: pool + backoff + call log. `dryRun` (no key) logs instead of sending.
- **GameLink**: both TCP listeners, auto-reconnect, buffered pending lines flushed
  on reconnect, `observe` poll at 10 Hz. Emitters use the EXACT engine vocabulary.
- **StateCompiler**: 5 digests (squad / pacing / buddy / adapt / nemesis-death), each
  narrative + dense JSON, hard budget guard throws `BudgetError` > 4000 tokens.
- **DecisionComposer**: probability → line with all safety logic as code:
  - flank requires avg dist ≥ 200 AND an open link-graph route ≠ player's region,
    else demotes to `focus_fire` (verified live in replay: correct demotion happened)
  - wounded squads (fraction-of-spawnhealth < 0.25) never press → `fallback`
  - critical player stress (hp<30 or ammo<10%) blocks ALL spawns, issues mercy items
  - baron spawn gated below difficulty 4 → demoted to knight
  - hysteresis: squad 2 s, buddy 3 s (fits the engine's one-directive-slot model)
- **LearningTracker**: 6-axis EWMA (aim/movement/resource/aggression/routing/
  adaptability) with per-axis evidence counts; ingests engine event labels
  (`"<seq>:hit:<type>:<weapon>:<dmg>"` / `"<seq>:kill:<type>:<weapon>"`) with a
  dedupe set; difficulty controller: bounded ±0.25 steps, evidence-gated (≥12 obs),
  **frustration > boredom > dominance** priority, floor 1 / ceiling 7.
- **Replay mode**: `npm run replay` pipes recorded observations through the IDENTICAL
  pipeline (mock answers) — this is how you develop without launching the game.
- **15/15 tests pass, tsc --noEmit clean.** Tests encode the composer invariants
  (vocabulary, roster ids, caps, stress gates, baron floor, hysteresis) and the
  difficulty controller's boundedness + evidence gating.

### 3.2 Engine (Tier 0) — complete, compiles, links, RUNS
- **`p_nemesis.c/.h`**: per-type table (14 monster types × 8 tactic weights + 9
  weapon biases + death counters), 16-entry event ring with unique labels,
  `NEM_Propose` clamping (|Δ| ≤ 0.5, weights ∈ [0.05, 2.0], bias ∈ [−1, 1]),
  0.5%/s decay toward neutral, versioned `nemesis_memory.dat` persistence
  (saved in `I_Quit` via `NEM_Quit`, loaded lazily in `NEM_Init`).
- **Ground-truth hooks** in `P_DamageMobj` (p_inter.c): `NEM_NoteHit` after
  `target->health -= damage` when the target SURVIVED; `NEM_NoteKillM` in the kill
  block before `P_KillMobj`. Both pass `source->player->readyweapon` (or −1).
  Buddy/monster kills deliberately do NOT teach the table (player-inflicted only).
- **Tactic attribution**: `AI_Apply` (p_ai_llm.c) calls `NEM_NoteTactic(type, order)`
  whenever a directive lands, so death-time "died while executing flank_left" is known.
- **Serializer**: `AI_Serialize` appends `"nemesis":{version, rows[], events[]}` —
  only ACTIVE rows are emitted (token economy). Verified appearing in live observe.
- **New protocol line**: `nemesis propose=<type> tactic_weight=<order>:<delta> [bias=<cm>]`
  parsed in `AI_HandleLine`, applied via clamped `NEM_Propose`, acked `ok`.
- **Native consumption (outage-proof learning)**:
  - `P_AI_RuleTactics`: wounded-fallback gated by `NEM_TacticWeight(tn,"fallback") ≥ 0.5`;
    flank-vs-focus choice weighted by learned flank vs focus_fire weights.
  - `P_Director_PickType`: a trash-type pick with `NEM_Deaths(tn) ≥ 5` escalates to the
    `dir_special1` pool (caco/baron/spectre/soul).
- **Decay pass**: `P_AI_Ticker` calls `NEM_DecayPass()` ~1×/s, in every mode.

### 3.3 Verified live (smoke test, before the listener hiccup)
Game launched with `-iwad freedoom1.wad -warp 1 1 -skill 3 -aidirector 31666`:
```
tic: 336 | monsters: 32 | regions: 64
player: {"pos":[-120,263,0],"angle":178,"health":100,"armor":0,"weapon":1,"region":146}
director: {"intensity":14,"state":0,"recent_dmg":0,"ammo_pct":4}
nemesis:  {"version":1,"rows":[],"events":[]}    ← our block, live
```

### 3.4 Docs
- `docs/JEV_DIRECTOR_WORKFLOW.md` v1.2 — full design + build record (§10 is final arch).
- `jev/README.md` — runtime guide. Root `readme.md` — overview + quick start.

---

## 4. Verified protocol facts (pin these; they cost blood to learn)

### 4.1 Engine TCP protocol (:31666 director, :31700 player agent)
- Plain newline-delimited text over TCP, **loopback-only, ONE client per listener**.
  The poll is non-blocking; the game NEVER waits on the socket. A slow/dead client
  costs the game nothing.
- `observe` → one JSON line (cached <2 tics). Outside a level: `{"nolevel":true,"monsters":[]}`.
- Mutations are acked `ok` (player-agent mutations are silent): `act order=<o>
  ids=<csv> [focus=] [x=] [y=] [for=] [after=]`, `spawn type=<t> count=<1..8>`,
  `spawn item=medkit|ammo`, `director relax`, `buddy order=…`, `reset`, `wake`,
  and (ours) `nemesis propose=…`.
- **Order vocabulary is EXACTLY** (`AI_OrderByName`): `chase, hold, fallback,
  flank_left, flank_right, ambush, focus_fire, use_door`. Unknown → AIO_NONE.
  Docs elsewhere say "focus-fire"/"none" — trust the parser.
- **Buddy vocabulary** (`AI_BuddyTactic`): `engage, defend, hold, regroup, retreat,
  goto, grab` (+auto).
- **Spawn names** (`P_Director_TypeByName`): zombie, shotgun, chaingun, imp, pinky,
  spectre, lost, caco, pain, knight, baron, revenant, mancubus, arachnotron.
- **Monster ids = registry slot + 1**, rebuilt during serialization. STILL-LIVE
  monsters usually keep their ids, but you MUST re-validate against every fresh
  observe; never carry ids across roster changes.
- **One directive slot per monster**: a second `act` for the same id OVERWRITES;
  `after=` delays that one directive (it is NOT a queue). Default `for=70` (~2 s).
- **Watchdog**: only `spawn` / `spawn item=` / `director relax` reset `dir_llmlast`.
  After ~15 s without a pacing command the rule FSM resumes (by design — this is
  the automatic fallback). `act`/`buddy` do NOT feed it.
- `A_LLMChase` steers **movement only**; melee/missile attack gates stay vanilla
  (`P_CheckMissileRange` etc.). Deadliness comes from positioning + targeting +
  what gets spawned — not from order choice alone.
- `AI_Serialize` builds over `regions[]` (occupied + 1-hop neighbors) and
  `links[]` (`open|door|locked`) — the flank-route check in the composer uses this.
- Observation buffer is 32 KB (`OBSBUF`); our nemesis block is written only if
  ≥2 KB headroom remains (guard added).
- Player agent (:31700, `-aiplayer [port|demo]`): `map`/`observe` → JSON with
  `lidar`, `sounds`, `things`, `doors`; intents `goto x y`, `face x y`,
  `turn <deg>`, `attack 0|1`, `target <id>`, `weapon <1..8>`, `use`, `stop`.

### 4.2 Engine internals that mattered
- `AIO` enum: NONE=0 … USEDOOR=8; registry `AI_MAX`=256.
- Vanilla spawnhealth table (needed for wounded-ness): zombie 20, shotgun 30,
  chaingun 70, imp 60, pinky/spectre 150, lost 100, caco/pain 400, knight 200,
  baron 1000, revenant/arachnotron 250, mancubus 600.
- `dir_special1` = {MT_HEAD, MT_BRUISER, MT_SHADOWS, MT_SKULL} (DOOM1-safe specials).
- `P_DamageMobj` guard stack order matters: weapon_power scale → buddy damage
  scale → HEGGFX morph → XPOISONCLOUD → turret/friendly bail → ff_protect bail →
  SKULLFLY momentum → baby-skill halving → thrust → **player block** (armor etc.)
  → `target->health -= damage` → kill block → painchance → retaliation.
  Our hooks sit: kill inside the `if (target->health <= 0)` block (before
  `P_KillMobj`), hit right before the painchance block (survivors only).
- Rule director FSM: BUILDUP → SUSTAIN → FADE with stress gates; `runfsm =
  !dir_llm || (gametic - dir_llmlast > DIR_LLM_FALLBACK)`.
- The engine is 1996 K&R-flavored C: build needs `-fno-strict-aliasing -fcommon`
  (CMake already sets this) and shows hundreds of harmless C4113 warnings in
  d_deh.c/state tables. Don't chase them.

### 4.3 Jev API
- Endpoint/auth/shapes as in §1.1. Client retry: 429/529 exponential backoff
  (documented), other errors: fail fast and let cadence retry.
- Cost model: state size dominates (questions ≈ free). Digest ~250–900 tok/call.
  Default cadence ≈ 5 calls/s ≈ $0.75/h. Output tokens free.

---

## 5. Testing status

| Layer | Status |
|---|---|
| Bridge unit tests (15) | ✅ pass — composer invariants (vocab fuzz, roster ids, caps, stress, baron floor, hysteresis), learning boundedness/evidence gating, digest budget guard, squad grouping |
| Bridge typecheck | ✅ `tsc --noEmit` clean (strict + noUncheckedIndexedAccess) |
| Bridge full-pipeline offline | ✅ replay with mock Jev produces correct emitted lines (incl. a verified flank→focus demotion by the gate) |
| Engine compile + link | ✅ MSVC 14.44 via VS-2022-BuildTools CMake, Release, x64 |
| Engine live observe | ✅ nemesis block present, real level data |
| Engine live proposals | ⚠️ sent but response not captured before the listener stopped answering (see §6.4) — **re-verify** |
| Bridge ↔ live game end-to-end | ❌ NOT yet run (bridge was never pointed at the live game) |
| Real Jev key end-to-end | ❌ never run (no key; everything used mock/dry) |
| Savegame round-trip of nemesis_memory.dat | ❌ untested (load path runs at NEM_Init; save at quit) |
| Buddy-directed gameplay (-aicoop) | ❌ untested live |

`nemesis_memory.dat` reset: delete the file (it's written next to buddydoom.cfg, i.e. `BuddyDoom/run/`).

---

## 6. What broke, and how it was fixed (read before debugging)

### 6.1 Bridge bugs found by tests (all fixed)
- **hp-fraction math**: first version divided raw hp by count — "wounded squad"
  was computed wrong (a test caught it). Fixed with the SPAWNHEALTH table and
  fraction-of-spawnhealth. Lesson: engine reports raw hp; you must know spawnhealth.
- **Flank test world**: initially asserted flank succeeds in a 2-region world with
  no third region — the gate correctly demoted it. The TEST was wrong, not the code.
- **Windows glob**: `tsx --test test/*.test.ts` doesn't expand on Windows —
  list test files explicitly in package.json.
- **ESM pitfalls**: `require` doesn't exist in ESM (use `import {createWriteStream}`);
  no `import` of .ts extensions — use `.js` suffixes in relative imports.

### 6.2 Engine bugs caught in self-review (fixed before compile)
- A dead first-draft function (`NEM_NoteDamage` with a nonsense line) was removed;
  the mobj→name mapping lives in `NEM_MTypeName` + wrappers `NEM_NoteHit/NoteKillM`.
- **`NEM_Propose` order-parse bug**: `NEM_OrderIndex` received `"chase:-0.25"`
  (never split at the colon). Fixed with explicit `memcpy(order, key+14, colon-len)`.
- **Serializer comma bug**: first version peeked at `buf[n-1] != '['` which breaks
  with multiple active rows. Fixed with an explicit `firstrow` flag.
- **Header order**: `p_nemesis.h` used `mobj_t` before including `p_mobj.h` — include
  moved to the top.
- **A bad edit I reverted immediately**: while inserting the kill hook I initially
  changed `P_KillMobj(source, target)` to a "fixed" argument — WRONG, reverted to
  vanilla the same minute. If you see weirdness there, the current code is correct:
  `P_KillMobj (source, target);` and the hook is a separate block ABOVE it.

### 6.3 Build system (the two upstream link gaps)
- **`dbghelp.lib` missing**: `i_main.c`'s crash handler (StackWalk64/Sym*/MiniDumpWriteDump)
  doesn't link under CMake. Fixed in `BuddyDoom/CMakeLists.txt`:
  `if(MSVC) target_link_libraries(buddydoom PRIVATE dbghelp) endif()`.
  (Upstream presumably builds via `build_all_win.bat`/nmake which adds it.)
- **`extractor` tool can't link** (unresolved `wadpng_from_patch`, `wc_*` — it needs
  more sources than the CMake single-file rule gives it). Pre-existing upstream
  gap; we don't need that tool, so it's left broken. Build only the game target:
  `cmake --build BuddyDoom/build --config Release --target buddydoom`.
- SDL3 SDK: downloaded `SDL3-devel-3.4.16-VC.zip` from GitHub releases, extracted
  to `Project-Nemesis/SDL3` (CMake searches `${CMAKE_SOURCE_DIR}/../SDL3` — since
  our CMAKE_SOURCE_DIR is `BuddyDoom/`, the sibling that works is `Project-Nemesis/SDL3`).
  gitignored.

### 6.4 Runtime quirks learned live (IMPORTANT for next session)
- **Launching the game from a tool shell**: `cmd /c start buddydoom …` HANGS the
  calling shell (console-subsystem app inherits the console). What works:
  `powershell -Command "Start-Process -FilePath 'BuddyDoom\run\buddydoom.exe'
  -ArgumentList '-iwad','freedoom1.wad','-warp','1','1','-skill','3','-aidirector','31666'
  -WorkingDirectory 'BuddyDoom\run'"`.
- **The one-client listener can wedge**: after two raw probe connections (the first
  never explicitly closed, the second maybe raced), the game was alive (process up,
  ~118 MB) but :31666 refused connections / stopped answering. Root cause hypothesis:
  the listener keeps the FIRST socket half-open and never re-accepts after an abrupt
  client disconnect. **The bridge's GameLink does clean reconnects, but if you
  experiment with raw probes, close them properly (`s.end()`), and prefer just
  running the bridge.** Candidate upstream fix: accept-loop that re-accepts on
  disconnect (see `AI_PollSocket`/`P_AI_NetService` in p_ai_llm.c). NOT yet fixed —
  it's the known-open runtime issue.
- **The bridge was never connected live.** Next session: kill stale processes
  (`taskkill //F //IM buddydoom.exe`), relaunch (PowerShell Start-Process), then
  `cd jev && npm run dev -- --mock` and watch `log/emitted.jsonl` + `log/calls.jsonl`.
- IWAD: shareware doom1.wad downloads on archive.org are installer-packed or 404;
  **Freedoom 0.13.0 release zip works** (freedoom1.wad → run/ID0/). The user also
  has their own WADs in ID0/ — do not delete anything there (we removed only the
  bogus 404 doom1.wad we created ourselves).

---

## 7. Design decisions worth remembering (the WHY)

1. **Why memory lives in C, not the bridge**: survives bridge/API failure, can join
   savegames, fits repo convention (`nemesis_memory.dat` beside `buddydoom.cfg`),
   and lets the RULE director consume learning with zero model calls. Bridge-side
   memory was the original design; the merge (§9 of workflow doc) moved it engine-side.
2. **Why hook P_DamageMobj (hits) and not just P_KillMobj (deaths)**: deaths are
   sparse (~2/level for a good player). Hits are continuous AND carry weapon ground
   truth. One hook kills two cons at once (sparse learning + lossy attribution).
3. **Why proposals are clamped in C**: a buggy/hostile bridge must not be able to
   unbalance the table. |Δ|≤0.5 per proposal, weights ∈ [0.05, 2.0]. Trust boundary
   = the engine, always.
4. **Why digests instead of raw observe**: raw `AI_Serialize` output can approach the
   32 KB buffer ≈ 8–10k tokens ≈ 10–25× the digest budget. Digest = narrative line +
   dense short-key JSON ≤ ~1.5k tokens. Narrative matters — Jev reads text.
5. **Why the composer owns all thresholds (not Jev)**: Jev is calibrated but
   stateless; policy must be deterministic, testable, auditable code. Difficulty
   "decisions" are bounded steps, never free variables.
6. **Why hysteresis everywhere**: the engine holds ONE directive per monster with
   `for=` lifetimes; a jittery 2–10 Hz classifier would thrash it. Orders persist
   2–3 s minimum per surface.
7. **Why the difficulty controller is frustration-first**: never stack difficulty on
   a struggling player. Frustration > boredom > dominance; low evidence ⇒ no change.
8. **Why `-fno-strict-aliasing` matters**: 1996 engine type-puns; CMake sets it;
   don't "optimize" the build flags.

---

## 8. How to run everything (cheat sheet)

```sh
# Bridge: offline full-pipeline demo (no key, no game)
cd jev && npm install && npm test && npm run replay

# Engine build (MSVC toolchain via VS BuildTools CMake)
CMAKE="/c/Program Files (x86)/Microsoft Visual Studio/2022/BuildTools/Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe"
"$CMAKE" -B BuddyDoom/build -S BuddyDoom -A x64
"$CMAKE" --build BuddyDoom/build --config Release --target buddydoom
# → BuddyDoom/run/buddydoom.exe (staged by CMake)

# Live game (PowerShell Start-Process; cmd start hangs!)
powershell -Command "Start-Process -FilePath 'BuddyDoom\run\buddydoom.exe' -ArgumentList '-iwad','freedoom1.wad','-warp','1','1','-skill','3','-aidirector','31666' -WorkingDirectory 'BuddyDoom\run'"

# Bridge against the live game (mock Jev)
cd jev && npm run dev -- --mock
# with a real key:  set TYPESAFE_API_KEY=... && npm run dev
# cadence presets:  --cadence aggressive|default|economy

# Raw protocol probe (close sockets properly or the listener wedges!)
node -e "const net=require('net');const s=net.connect(31666,'127.0.0.1',
  ()=>s.write('observe\n'));let b='';s.on('data',d=>{b+=d;
  if(b.includes('\n')){console.log(b.slice(0,300));s.end();process.exit(0)}});"

# Reset the learned table
rm BuddyDoom/run/nemesis_memory.dat
```

---

## 9. Next steps (ordered, with rationale)

1. **Fix the listener re-accept bug** (p_ai_llm.c socket loop) or at minimum verify
   the bridge's clean-reconnect path against a fresh game. Without this, every
   crashed client costs a game restart.
2. **Connect the bridge to the live game** (`npm run dev -- --mock`) and watch
   emitted lines while playing. Verify: act orders visibly change monster behavior,
   spawn/relax pacing feeds the watchdog, nemesis proposals get acked and survive
   re-observe, `nemesis_memory.dat` appears on quit and reloads on next start.
3. **Real-key end-to-end**: measure actual p50/p95 latency from this machine,
   re-tune cadence presets against reality, log model version drift.
4. **A/B harness**: `-aidemo` (scripted) vs JEV director on the same map; log
   time-to-peak, damage-per-death, item efficiency. This produces demo numbers.
5. **Buddy integration test** (`-aicoop`): buddy digest + `buddy order=` lines live.
6. **JevMon panel**: repurpose `gpumon` (already builds) into a live dashboard:
   calls/s, p50/p95, current difficulty tier, skill radar, grudge table view.
   This is the demo/showcase surface.
7. Optional engine enrichment: `region_heat` (region occupancy history) +
   projectile-dodge events in `AI_Serialize` to sharpen habit probes.
8. Savegame integration: decide whether nemesis state joins the save block or
   stays session-global (current: session-global via .dat; simpler, works).

---

## 10. Glossary (the vocabulary used everywhere)

- **Tier 0 / Tier 1 / Tier 2** — engine C memory+ground truth / TS bridge intelligence / Jev judge.
- **noul / choice / score** — Jev's three question primitives.
- **digest** — the compiled per-call state the bridge sends to Jev.
- **composer** — deterministic probability→protocol-line converter (all policy lives here).
- **hysteresis** — minimum interval before re-ordering a surface (anti-thrash).
- **ground truth** — engine-emitted events (hit/kill with weapon) vs inferred-from-polling.
- **watchdog** — `dir_llmlast`; rule FSM auto-resumes ~15 s after the last pacing command.
- **nemesis propose** — the clamped bridge→engine weight-write protocol line.
- **EWMA skill vector** — 6-axis exponentially-weighted player skill model with evidence counts.
- **the merge** — combining the continuous-bridge design with the death-triggered
  nemesis design (workflow doc §9); resolved by the damage-hook + engine-side memory.
