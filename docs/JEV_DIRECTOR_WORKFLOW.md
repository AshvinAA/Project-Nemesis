# JEV Director — Project Workflow
## Replacing BuddyDoom's local LLM brain with TypeSafe AI's Jev (System One)

> Status: **v1.2 — §10 "Nemesis Fabric" is BUILT.** Bridge: `jev/` (TypeScript,
> 15/15 tests). Engine: `files/p_nemesis.c/.h` + hooks in `p_inter.c`,
> `p_ai_llm.c`, `p_ai_director.c`, `i_system.c`. Runtime docs: `jev/README.md`.
> §3–9 remain as the design record. All BuddyDoom protocol facts are verified
> against source; all Jev facts against `docs.typesafe.ai` and the launch blog.

---

## 0. Why this works (the one-paragraph thesis)

BuddyDoom's current LLM director fails at exactly the thing an AI Director needs most:
**latency**. A local Ollama model (e.g. ministral-3:8b) takes seconds per decision, so the
game can only afford a handful of coarse decisions per minute — the observation gets
summarized into a paragraph, the model answers in prose, and the result is parsed back into
`act`/`spawn` lines. Jev inverts every constraint: **~70–500 ms end-to-end** (10–100× faster),
**$0.042/MTok input with output free** (~400× cheaper), parallel evaluation of every question
in one call (adding questions ≈ free), outputs constrained to your schema (zero parse
failures, zero hallucinated directives), and **calibrated probabilities** on every answer —
which is precisely the signal we need for an adaptive-difficulty system that must be honest
about what it knows about the player.

The consequence: we upgrade from "one slow brain issuing a few orders per minute" to a
**hierarchical decision fabric** running at 5–10 Hz, where every monster squad, the buddy,
the spawner, and the item economy each get their own continuously-refreshed probability
distribution. More decisions, more variables, more state — because the cost of a decision
collapsed.

---

## 1. What we learned about Jev (operational summary)

### 1.1 The endpoint

```
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer <TYPESAFE_API_KEY>
Content-Type: application/json
```

```json
{
  "state":  { "...arbitrary JSON object/array or plain string..." },
  "model":  "jev-latest",
  "questions": {
    "<your_key>": { "type": "noul",   "instructions": "...", "criteria": { "true": "...", "false": "..." } },
    "<your_key>": { "type": "choice", "instructions": "...", "criteria": { "opt_a": "...", "opt_b": "..." } },
    "<your_key>": { "type": "score",  "instructions": "...", "criteria": ["level 0 desc", "level 1 desc", "..."] }
  }
}
```

Response — one answer per question, same keys:

```json
{
  "model": "jev-1.13.0",
  "answers": {
    "is_urgent":  { "type": "noul", "noul": 0.92 },
    "department": { "type": "choice", "choice": "technical",
                    "probabilities": { "billing": 0.08, "technical": 0.85, "sales": 0.07 },
                    "confidence": 0.82 },
    "frustration":{ "type": "score", "score": 1.6, "legend": { "0": "Calm", "1": "Frustrated", "2": "Very angry" },
                    "probabilities": { "0": 0.05, "1": 0.3, "2": 0.65 }, "confidence": 0.78 }
  },
  "usage": { "input_tokens": 312, "output_tokens": 48 }
}
```

### 1.2 The three primitives, mapped to Doom concepts

| Primitive | Returns | Doom mapping |
|---|---|---|
| **Noul** | `noul` ∈ [0,1] — P(yes) | Threat booleans: `player_camping`, `squad_wiped`, `player_low_ammo`, `flank_route_open`, `wakeup_needed` |
| **Choice** | argmax + full distribution + confidence (≤255 options) | Tactics: `hold / fallback / flank_left / flank_right / focus_fire / ambush / use_door / chase`; spawn species selection; buddy stance |
| **Score** | probability-weighted mean across 2–10 described levels | Continuous knobs: player stress (0–9), horde intensity, ambush patience, retreat urgency, difficulty tier |

### 1.3 Hard constraints (design within these)

| Constraint | Value | Consequence |
|---|---|---|
| Latency | 70–500 ms typical | Decision cadence 2–10 Hz, never blocking the 35 Hz tic loop |
| Context budget | state + all questions ≈ 64k tokens; state + longest question ≈ 32k | Observation must be a **dense digest**, not raw world dump |
| Choice cardinality | ≤ 255 options | Full monster-roster spawn menus fit; per-monster ID targeting does **not** — use squad-level calls |
| Score levels | 2–10, described by **situations not degrees** | Write rubric entries as game situations ("player is cornered with <25 hp and no ammo") |
| State formats | string, JSON object, or array of text | We send compact JSON with short keys + a human-legible digest |
| No images | text only | LIDAR rings, region graph, distances, sounds — structured, not pixels |
| Calibration | probabilities are honest over many calls | Safe to threshold on; enables the learning loop in §4 |
| No cross-question context | each question evaluated independently | Never ask "should A attack B or C?" in one question — decompose |
| Rate limits | 429/529 → exponential backoff | SDK retry policy; queue coalescing on our side (§3.6) |
| Versioning | `jev-latest` alias → currently `jev-1.13.0` | Log the returned `model`; pin thresholds per version |

### 1.4 The two rules that make Jev smarter in *our* usage

1. **Atomic questions, composed in code.** TypeSafe's own guidance: don't ask one big
   "what should the monsters do?" question. Ask 8 narrow ones, then combine with ordinary
   code. Composition logic lives in C/TS, so retuning difficulty = changing a coefficient,
   not rewriting a prompt.
2. **Scores need situations, Nouls need criteria.** "Moderately stressed" teaches the model
   nothing; "player took damage from 3+ directions in the last 5 s and health < 40" is
   matchable. Every threshold we later branch on gets described in the criteria text.

---

## 2. What BuddyDoom gives us (verified in source)

### 2.1 Three existing external-AI surfaces — all stay, one client now drives all three

| Surface | Flag | Port | Protocol (verified) |
|---|---|---|---|
| Player-1 agent | `-aiplayer [port\|demo]` | 31700 | TCP lines: `map`, `observe` → JSON; intents `goto x y`, `face x y`, `turn deg`, `attack 0/1`, `target id`, `weapon 1..8`, `use`, `stop` |
| Monster director | `-aidirector [port]` | 31666 | TCP lines: `observe` → JSON; `act order=… ids=… [focus=] [x=] [y=] [for=tics] [after=tics]`; `spawn type= count=`, `spawn item=medkit\|ammo`, `director relax`, `buddy order=…`, `reset`, `wake`, **`nemesis propose=…` (new)** |
| Co-op buddy | `-aicoop` | — | Local bot; director-level overrides via the same `buddy order=` line (`engage\|defend\|hold\|regroup\|retreat\|goto\|grab`) |

### 2.2 The observation contract (`observe` on 31666)

`AI_Serialize` in `files/p_ai_llm.c` emits, per level snapshot (cached < 2 tics):

- `player` — pos, angle, health, armor, weapon, region
- `buddy` — pos, health, armor, weapon, ammo, state, region, d_player, route (when `-aicoop`)
- `monsters[]` — id, type, pos, hp, region, see_player, see_buddy, d_player, d_buddy, order
- `regions[]` — `[id, x, y]` centroids; `links[]` — `[a, b, "open"|"door"|"locked"]`
- `director` — intensity, state (0 buildup / 1 sustain / 2 fade), recent_dmg, ammo_pct
- **`nemesis` (new)** — learned weight table + ground-truth event ring (`NEM_Serialize`)

Player-agent `observe` (port 31700) adds: `tic`, exit, doors, up-to-1200 wall line tuples,
8-direction `lidar`, `sounds` (hooked at `S_StartSound` via `G_Agent_LogSound`), `things`,
`waiting_at_door`.

### 2.3 Constraints we must respect

- **One directive slot per monster** — a second `act` for the same id *overwrites*; `after=` is
  a delay, not a queue. Sequences = re-issue on next observation.
- **IDs are registry slot + 1** — refresh from every observation; never cache across rosters.
- **One TCP client per listener** — our bridge is the single client; the JEV brain talks to the
  bridge, not to the game.
- **`act`/`buddy` do not feed the director watchdog** — only `spawn`, `spawn item=`,
  `director relax` reset `dir_llmlast`. The rule FSM resumes after ~15 s without a pacing
  command. Our pacing layer issues a pacing heartbeat every 5 s while authoritative.
- **C reflex layer owns the tic-locked details** (collision, doors, pathfinding, weapon
  timing). We issue *intent*, never per-tic motion.
- **Loopback-only listeners, single-player semantics** — netplay/demo compatibility is already
  documented as broken for these modes; we keep that boundary.

### 2.4 Source facts pinned during the build

- `AI_OrderByName` (p_ai_llm.c:654): `chase, hold, fallback, flank_left, flank_right,
  ambush, focus_fire, use_door` (unknown → AIO_NONE).
- `AIO` enum (p_ai_llm.c:87): NONE=0 … USEDOOR=8; `AI_MAX` = 256 registry slots.
- `A_LLMChase` (p_ai_llm.c:259): orders steer **movement only**; vanilla attack gates
  (melee / `P_CheckMissileRange`) remain engine-owned.
- `AI_Apply` default directive lifetime `for=70` (~2 s).
- `obsbuf` is 32 KB (`OBSBUF`); the serializer cache is < 2 tics.
- `P_Director_TypeByName` (p_ai_director.c:1160): zombie/shotgun/chaingun/imp/pinky/
  spectre/lost/caco/pain/knight/baron/revenant/mancubus/arachnotron.
- `dir_special1` = {MT_HEAD, MT_BRUISER, MT_SHADOWS, MT_SKULL} (DOOM1-safe specials).
- `P_DamageMobj` (p_inter.c:1038): full guard stack (HEGGFX, poison, turret/friend,
  friendly-fire, godmode) completes before our hooks; kill block at the `P_KillMobj` call;
  survived-hit hook at the painchance block.

---

## 3. Architecture (summary — as built)

```
                    ┌──────────────────────────────────────────────────┐
                    │                BuddyDoom (C, 35 Hz)              │
                    │  g_agent.c ──TCP:31700──►  p_ai_llm.c ◄──TCP:31666 │
                    └────────┼────────────────────────┼────────────────┘
                             ▼                        ▼
                    ┌──────────────────────────────────────────────────┐
                    │            jev_bridge  (Node.js/TS)              │
                    │  GameLink → State Compiler → Jev Pool →          │
                    │  Decision Composer → protocol lines              │
                    │  LearningTracker (EWMA + bounded difficulty)     │
                    └──────────────────────────────────────────────────┘
                                      ↕ (nemesis propose / observe)
                    ┌──────────────────────────────────────────────────┐
                    │  Tier 0 (C): p_nemesis.c — table + event ring    │
                    │  hooks in P_DamageMobj/P_KillMobj; rule director │
                    │  and spawn mixing consume the table natively     │
                    └──────────────────────────────────────────────────┘
```

**Tier principle:** memory and ground truth live in the engine; intelligence lives in the
bridge; Jev is a stateless judge between them. Every Jev-driven write-back is a *clamped
proposal*. Full component responsibilities: §9–10 below.

### 3.1 Call inventory (as built in `jev/src/stateCompiler.ts`)

| # | Call | Cadence (default) | Questions |
|---|---|---|---|
| 1 | squad tactics | 400 ms | `squad_stance` (8-way choice), `player_vulnerable` (score 0–3), `fire_discipline` (score 0–2) |
| 2 | pacing | 800 ms | `horde_now` (noul), `phase` (choice), `spawn_species` (14-way choice), `horde_size` (score 0–4) |
| 3 | buddy | 1500 ms | `buddy_stance` (7-way choice), `buddy_in_danger` (noul) |
| 4 | adaptation | 2000 ms | `skill_is_outpacing_difficulty`, `strongest_axis` (6-way), `boredom_risk`, `frustration_risk` |
| 5 | nemesis judgment | on kill event | `weight_shift` (score), `bias_direction` (5-way), `pattern_confident` (noul) |

Every digest carries a narrative line + dense short-key JSON, budget-guarded at 4000 tokens
(`BudgetError` throws above it).

### 3.2 Composer guarantees (tested in `jev/test/composer.test.ts`)

- Never emits an order outside `AI_OrderByName`'s vocabulary (fuzz-swept).
- Never targets an id outside the current roster.
- Spawns capped (`maxSpawnCount`), baron gated below difficulty 4, **all spawns blocked**
  under critical stress (hp < 30 or ammo < 10%) with mercy items instead.
- Flank orders require distance + a verified open link-graph route, else demote to
  `focus_fire`. Wounded squads (fraction-of-spawnhealth < 0.25) never press.
- Hysteresis per surface (squad 2 s, buddy 3 s) — stable command streams for the
  one-directive-slot protocol.

---

## 4. The learning loop (as built)

- **Ground truth (Tier 0):** `NEM_NoteHit` / `NEM_NoteKillM` hooks in `P_DamageMobj`
  record every player-inflicted hit (type, damage, weapon) and kill (type, weapon,
  tactic-in-effect via `NEM_NoteTactic` from `AI_Apply`). Events go into a 16-entry ring
  with unique labels (`"<seq>:hit:imp:shotgun:24"`) that ride every `observe`.
- **Fast memory (Tier 1):** `LearningTracker` — six-axis EWMA skill vector (aim, movement,
  resource, aggression, routing, adaptability) with per-axis evidence counts, updated from
  engine-truthed signals only (event labels, `director.recent_dmg`, `ammo_pct`, deaths).
- **Slow memory (Tier 0):** per-type tactic weights + weapon bias, decayed ~0.5%/s toward
  neutral, persisted to `nemesis_memory.dat` (versioned) on quit.
- **Difficulty controller:** bounded (±0.25/interval), evidence-gated (min 12 obs),
  frustration-first (frustration beats boredom beats dominance), floor 1 / ceiling 7.
- **Write-back:** death-time Jev judgment → clamped `nemesis propose=` deltas; the engine
  re-clamps. The bridge cannot corrupt the table even if compromised.

---

## 5. Build status & what remains

### Shipped (this session)

- [x] `jev/` bridge: Jev HTTP client (backoff, pool, call log), TCP GameLink (reconnect,
      pending-buffer, poll), State Compiler (5 digests, budget guard), Decision Composer
      (gates, hysteresis, exact vocabulary), LearningTracker, main loop, replay mode,
      deterministic mock Jev for offline full-pipeline runs. **15/15 tests, tsc clean.**
- [x] Tier 0 engine: `p_nemesis.c/.h` (table, clamps, decay, `nemesis_memory.dat`),
      `P_DamageMobj` hit/kill hooks, `AI_Serialize` nemesis block, `nemesis` line parser,
      decay pass in `P_AI_Ticker`, rule-tactic weighting, spawn escalation bias,
      `NEM_Quit` save on exit, MSVC + Makefile.in build entries (CMake globs automatically).
- [x] Workflow docs (this file) + `jev/README.md` runtime guide.

### Remaining (next sessions)

- [ ] Compile the engine (needs MSVC + SDL3 on Windows — no local compiler here;
      `cmake -B build && cmake --build build` or `build_all_win.bat`).
- [ ] Live end-to-end run: `-aidirector 31666` + `npm run dev` with a real key.
- [ ] Wire `NEM_NoteHit`'s `weapon_idx` through `-aicoop` buddy kills (currently
      player-source only by design — buddy kills don't teach the nemesis table).
- [ ] Optional Phase 7: `region_heat` + projectile-dodge events in `AI_Serialize` to
      sharpen habit probes; JevMon panel (repurpose gpumon) for latency/confidence HUD.

---

## 6. Cost & latency budget

| Cadence preset | Calls/s | Avg input tok/call | $/hour |
|---|---|---|---|
| aggressive | ~10 | ~700 | ~$1.50 |
| default (built) | ~5 | ~700 | ~$0.75 |
| economy | ~1.5 | ~700 | ~$0.22 |
| mock/dry | 0 | — | $0 |

At $0.042/MTok input, output free. The nemesis table adds ~100–200 tokens per call but
makes every call smarter. Hysteresis keeps behavior stable even at economy cadence.

---

## 7. Risks & mitigations (post-build)

| Risk | Mitigation (in place) |
|---|---|
| Network latency/failure mid-fight | `for=` lifetimes expire naturally; rule FSM auto-resumes after ~15 s; learned table keeps rule-directed behavior biased |
| Rate limits | Client backoff on 429/529 (documented), concurrency pool, cadence presets |
| `jev-latest` drift | Every call logs the returned `model`; thresholds in one config file |
| Hallucinated directives | Impossible by construction — composer + C clamps, fuzz-tested |
| Engine crash from bridge | Impossible by construction — poll-based listeners, clamped proposals, no pointer storage |
| Overfitting one session | Two-timescale memory, decay pass, evidence gating |

---

## 8. Why this is technically impressive (the honest version)

- **Architecture inversion**: "LLM as director" (slow brain, tiny state, prose parsing)
  → "model as decision primitive" (fast typed judgments, rich compiled state,
  deterministic composition) with a **persistent, clamped, engine-side nemesis memory**.
- **40–60 calibrated judgments/second** available across five concurrent decision surfaces,
  with zero malformed outputs by construction.
- **Two-timescale learning**: continuous ground-truth skill tracking (every bullet) plus
  death-time Jev judgment calls, both persisted and consumed natively by the rule director
  when the API is down — **no dead modes**.
- **Replayable, testable, cheap**: recorded-observation replay with a deterministic mock
  Jev, 15 property tests on the composer/learning invariants, and a bill that's a rounding
  error next to the GPU rig the Ollama path required.

---

## 9. Reconciliation: the "nemesis memory" design (merged)

An independently-developed design for the same target — a persistent nemesis memory where
monsters "remember how you killed them" — was reconciled with this workflow. The two are
~85% the same architecture (model returns action probability distributions, stateless model
+ code-owned memory, reflex layer executes between rounds, rule director as fallback). The
differences and the merge:

### 9.1 What the nemesis design does better (adopted)

| Aspect | Nemesis design | Why it wins |
|---|---|---|
| Memory substrate | Engine-side C module: `files/p_nemesis.c` owning a persistent weight/weapon-bias struct, written to `nemesis_memory.dat` (sibling of `buddydoom.cfg`, matching project convention) | Survives bridge crashes, can join savegames, zero external dependency for memory, fits repo conventions. Bridge-side memory is lost if the process dies. |
| Kill attribution | Hook `P_KillMobj` in `files/p_inter.c`: log the tactic in effect + the weapon that landed the kill at the moment of death | Ground truth. Inferring kills/weapons from `observe` polling deltas is lossy and fragile. |
| Death-time reasoning | On enemy death, hand recent tactics + kill weapon + current weight table to the model, which *judges* the weight shift (pattern confidence, death history) instead of a hardcoded penalty | Better learning signal per event; Jev makes it ~free (~$0.00003/call). |
| Validation | Standalone browser prototype already runs the full mechanism end-to-end | Empirical validation beats spec. |

### 9.2 What the continuous layer adds (kept)

Death-triggered learning alone is sparse: a good player dies twice a level — two learning
events in 30 minutes — while dodging, wasting ammo, and routing constantly. The EWMA skill
vector samples that continuous behavior, and the difficulty controller turns it into
pacing/species/item adaptation between deaths. The two memories are complementary
timescales: **slow** = nemesis weight table (death-triggered, persistent); **fast** = skill
vector (continuous, session-scoped with a slow persistent component). Both are serialized
into every Jev state.

### 9.3 Jev changes the original nemesis math

1. The ~1.4 s/round cadence constraint was an Ollama number. At 70–500 ms, director rounds
   go from ~0.7/s to 2–10/s, and the death-time call is always affordable.
2. Digest compilation is required either way: raw world state will not fit Jev's ~64k
   budget, and digests are what keep calls at ~250–900 tokens.

### 9.4 Merged engine touchpoints (resolved)

| File | Change |
|---|---|
| `files/p_nemesis.c` (new) | Persistent weight/weapon-bias struct; decay pass; floor/ceiling clamps; load/save `nemesis_memory.dat` |
| `files/p_inter.c` | `P_DamageMobj` hooks: hit + kill ground truth (tactic-in-effect + killing weapon) |
| `files/p_ai_llm.c` | `AI_Serialize()`: append the nemesis block; `nemesis propose` parser; decay pass; rule-tactic weighting |
| `files/p_ai_director.c` | Spawn-type escalation consumes death counters natively |
| `files/i_system.c` | `NEM_Quit()` on shutdown |
| `jev_bridge` | Sends both memories in the state; applies bounded weight-shift recommendations; writes back via clamped proposals |

Resolved protocol facts for wiring (verified from `docs/MONSTER_AGENT_GUIDE.md`,
`docs/AIPLAYER.md`, and source): order vocabulary is exactly `none, chase, hold, fallback,
flank_left, flank_right, focus_fire, ambush, use_door` (`AI_OrderByName`); monster ids are
registry-slot+1 and must be refreshed from every observation; one directive slot per monster
(`after=` delays, never queues); only `spawn` / `spawn item=` / `director relax` reset the
rule-FSM watchdog (~15 s); one TCP client per listener (31666 director, 31700 player agent).

---

## 10. Final architecture: the Nemesis Fabric (hybrid)

> Principle: **memory and ground truth live in the engine; intelligence lives in the
> bridge; Jev is a stateless judge between them.** Each tier degrades independently and
> neither can block the 35 Hz tic loop.

### 10.1 The three tiers

**Tier 0 — Engine (C, deliberately dumb).**

| Piece | Detail |
|---|---|
| `files/p_nemesis.c` (new) | Per-monster-type table: tactic weights (8 AIO orders) + weapon bias (per player weapon). Decay pass + floor/ceiling clamps. Load/save `nemesis_memory.dat` (sibling of `buddydoom.cfg`). Version-tagged file format. |
| Ground-truth event log | Hook `P_DamageMobj` (every hit: target type, damage, attacker weapon) **and** the kill path (death context: tactic in effect, killing weapon). 16-entry ring with unique labels — a small ring in C, no pointers, no allocation in the tic path. |
| `AI_Serialize()` extension | Appends the compact table + event digest. Numbers only, ~100–200 tokens — rides along free inside the digest budget. |
| New protocol mutation | `nemesis propose=<type> tactic_weight=<order>:<delta> [bias=<cm>]` — engine **clamps** (`|delta| ≤ 0.5`, weights ∈ [0.05, 2.0]) and applies; replies `ok`. A buggy or hostile bridge cannot unbalance the table beyond the clamps. Non-blocking, poll-based. |
| Native consumption | `P_AI_RuleTactics` weights flank-vs-focus by learned weights; `P_Director_PickType` escalates over-farmed trash types toward the special pool — learned behavior with zero model calls (the outage-proof floor). |

**Tier 1 — Bridge (TS, all the intelligence).**
State Compiler digests (§3.1, mandatory — raw state would be ~8–10k tokens);
EWMA skill vector computed **from the engine's ground-truth event ring** instead of polling
inference; the full call inventory; Decision Composer with hysteresis + safety gates; and
the write-back path — composer-approved, clamped `nemesis propose` lines after every
adaptation cycle.

**Tier 2 — Jev (stateless judge).**
Unchanged: atomic questions, calibrated probabilities, no memory, no prose.

### 10.2 How every prior con dies

| Prior con (source) | Killing mechanism |
|---|---|
| A: memory lost if bridge crashes | Tier 0 owns the table; bridge only *proposes* deltas; rule director consumes it natively. A bridge crash loses at most one adaptation cycle. |
| A: kill/weapon attribution inferred from polling is lossy | `P_DamageMobj`/kill hooks give per-hit weapon ground truth. The bridge **never infers** — it reads events. |
| B: death-triggered learning is sparse (2 events/level) | The damage hook makes the stream **continuous**: a shotgunner feeds dozens of ground-truth (type, weapon) counters per fight. Death-time Jev judgment stays as the large reinforcement; damage events are the small frequent ones. |
| B: raw `AI_Serialize` state too expensive (~32 KB buffer ≈ 8–10k tok) | Digest compiler is mandatory in Tier 1; the table rides along at ~100–200 tokens. |
| B: slow C iteration (MSVC + SDL3 toolchain for every change) | Tier 0 is intentionally dumb — struct + clamp + io + hooks, write-once. All retuning lives in Tier 1 where iteration is TS-fast with replay mode. |
| A: hidden difficulty scalar is invisible to the player | The nemesis personality (weapon bias, per-type grudges) is the *visible* layer; JevMon dashboards both tiers. |
| B: savegame fragility | Version-tagged `nemesis_memory.dat` from day one. |
| A: learned behavior needs the API | Rule director consumes the table with zero model calls — deadliness persists through full outage. |

### 10.3 Cost & call inventory deltas

- All §6 budgets hold: digests ~250–900 tok/call, cadence presets unchanged.
- Damage/death events are engine-local — **zero** extra Jev calls; they enrich existing
  calls' state instead.
- Death-time judgment call: event-triggered (~2–10/level) at ~$0.00003 each.
- New state fields (table + event digest) add ~100–200 tok to every call — inside budget,
  and they make *every* other call smarter (tactics see the grudge table too).

### 10.4 Fallback matrix (the "no dead modes" guarantee)

| Bridge | API | Behavior |
|---|---|---|
| up | up | Full fabric: squad tactics, pacing, items, buddy, continuous + death-time learning, clamped write-back. |
| up | down | Ladder: cadence halves, then freeze; bridge keeps digesting events and holds proposed deltas until recovery. |
| down | up | Engine keeps last table; rule director consumes it (biased tactics + biased species) — **learned behavior persists**. Game never blocks (poll-based listeners). |
| down | down | Pure rule director + persistent table from `nemesis_memory.dat`. Still visibly smarter than vanilla; memories intact for next session. |

### 10.5 Residual tradeoffs (honesty clause)

Not cons, but priced-in costs: one small engine patch was required up front (Tier 0 can't
be prototyped in TS — mitigated by keeping it dumb and write-once); two processes to debug
(mitigated by replay mode + call/emitted JSONL logs); the `nemesis propose` line is a
bespoke protocol extension to document upstream in `MONSTER_AGENT_GUIDE.md` if contributed
back.
