# JEV Director — Project Workflow
## Replacing BuddyDoom's local LLM brain with TypeSafe AI's Jev (System One)

> Status: **Design document** (v1.1, Sep 2026 — §10 "Nemesis Fabric" is the final
> architecture; §3–9 remain as the design record). All BuddyDoom protocol facts below are
> verified against `docs/AGENT_CONTROL.md`, `docs/MONSTER_AGENT_GUIDE.md`,
> `docs/AIPLAYER.md`, `docs/DIRECTOR_MODES.md` on `doclulleNPC/BuddyDoom@main`.
> All Jev facts are verified against `docs.typesafe.ai` and the TypeSafe launch blog.

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

## 2. What BuddyDoom gives us (verified surfaces)

### 2.1 Three existing external-AI surfaces — all stay, one client now drives all three

| Surface | Flag | Port | Protocol (verified) |
|---|---|---|---|
| Player-1 agent | `-aiplayer [port\|demo]` | 31700 | TCP lines: `map`, `observe` → JSON; intents `goto x y`, `face x y`, `turn deg`, `attack 0/1`, `target id`, `weapon 1..8`, `use`, `stop` |
| Monster director | `-aidirector [port]` | 31666 | TCP lines: `observe` → JSON; `act order=… ids=… [focus=] [x=] [y=] [for=tics] [after=tics]`; `spawn type= count=`, `spawn item=medkit\|ammo`, `director relax`, `buddy order=…`, `reset`, `wake` |
| Co-op buddy | `-aicoop` | — | Local bot; director-level overrides via the same `buddy order=` line (`engage\|defend\|hold\|regroup\|retreat\|goto\|grab`) |

### 2.2 The observation contract (`observe` on 31666)

`AI_Serialize` in `files/p_ai_llm.c` emits, per level snapshot (cached < 2 tics):

- `player` — pos, angle, health, armor, weapon, region
- `buddy` — pos, health, armor, weapon, ammo, state, region, d_player, route (when `-aicoop`)
- `monsters[]` — id, type, pos, hp, region, see_player, see_buddy, d_player, d_buddy, order
- `regions[]` — `[id, x, y]` centroids; `links[]` — `[a, b, "open"|"door"|"locked"]`
- `director` — intensity, state (0 buildup / 1 sustain / 2 fade), recent_dmg, ammo_pct

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
  command. Our pacing layer must therefore issue a pacing command at least every ~15 s of
  authoritative control (or deliberately let the rule FSM resume as a safety net — we do the
  latter during fallback mode, §5.4).
- **C reflex layer owns the tic-locked details** (collision, doors, pathfinding, weapon
  timing). We issue *intent*, never per-tic motion. The `-aiplayer` reflex controller +
  BSP Dijkstra (`PF_AStar`) stays exactly as is.
- **Loopback-only listeners, single-player semantics** — netplay/demo compatibility is already
  documented as broken for these modes; we keep that boundary.

### 2.4 What gets deleted

| Old component | Disposition |
|---|---|
| `tools/director.c` (native Ollama client) | **Replaced** by `jev_bridge` (§3). Kept as `--legacy-ollama` fallback mode for offline play. |
| Ollama host/model config, GPU monitor's LLM role | GPU monitor (`gpumon`) stays — it's a useful SDL3 dashboard; repurpose its status panel for JEV call latency/confidence stats. |
| Prompt/parse layer (prose → `act` lines) | Deleted. Jev returns typed fields; the mapping table is code, not parsing. |
| `-aidemo` scripted director | Stays — it's engine-side and now serves as the **A/B baseline** for demoing JEV's advantage. |

---

## 3. New architecture

```
                    ┌──────────────────────────────────────────────────┐
                    │                BuddyDoom (C, 35 Hz)              │
                    │                                                  │
                    │  g_agent.c ──TCP:31700──►  p_ai_llm.c ◄──TCP:31666──  │
                    │  (player agent)            (monster director)    │
                    │        ▲                        ▲                │
                    └────────┼────────────────────────┼────────────────┘
                             │ observe / intents      │ observe / act+spawn+buddy
                             ▼                        ▼
                    ┌──────────────────────────────────────────────────┐
                    │            jev_bridge  (Node.js/TS, ~1 process)  │
                    │                                                  │
                    │  ┌────────────┐  ┌──────────────┐  ┌──────────┐  │
                    │  │ Aggregator │─►│ State Compiler│─►│ Jev Pool │  │
                    │  │ (31700 +   │  │ (digests,     │  │ (HTTP,   │  │
                    │  │  31666     │  │  budgets,     │  │  retry,  │  │
                    │  │  clients)  │  │  caching)     │  │  429/529)│  │
                    │  └────────────┘  └──────┬───────┘  └────┬─────┘  │
                    │                         ▼               ▼        │
                    │  ┌─────────────────────────────────────────────┐ │
                    │  │        Decision Composer (pure TS)          │ │
                    │  │  thresholds • hysteresis • probability      │ │
                    │  │  blending • safety gates • order mapping    │ │
                    │  └────────────────────┬────────────────────────┘ │
                    │                       ▼                          │
                    │  ┌─────────────────────────────────────────────┐ │
                    │  │        Player Model + Learning Loop         │ │
                    │  │  event ring • EWMA skill vectors •          │ │
                    │  │  difficulty controller • telemetry log      │ │
                    │  └─────────────────────────────────────────────┘ │
                    └──────────────────────────────────────────────────┘
```

### 3.1 Component responsibilities

**jev_bridge** (new Node.js/TypeScript service — the only new runtime):

1. Holds the *single* TCP client connection to each listener.
2. Merges both `observe` streams + config into one world model at up to 10 Hz.
3. Compiles the world model into per-consumer **state digests** that fit the Jev budget.
4. Fires parallel Jev calls (pool, small concurrency cap, backoff on 429/529).
5. Runs the **Decision Composer**: probabilities → concrete protocol lines, with all the
   safety logic that used to live in the LLM's prose (now deterministic code).
6. Persists the player model + telemetry (JSONL append; hot-reloadable).

**State Compiler** — the heart of the upgrade. Produces four digests:

| Digest | Consumer | Cadence | Contents (compiled, not raw) |
|---|---|---|---|
| **Squad digest** | tactics call | 2–4 Hz | Per awake squad: avg dist, cohesion, LOS bits, hp fraction, current orders, player's facing/momentum, region adjacency to player |
| **Pacing digest** | director call | 1–2 Hz | Stress vector (hp/armor/ammo/dmg-taken-rate/kill-rate/time-since-peak), live-monster census vs cap, phase, items dropped recently, exit-route progress |
| **Buddy digest** | buddy call | 0.5–1 Hz | Buddy hp/ammo/weapon/state, relative position, threat map near buddy, player's recent orders |
| **Player digest** | learning call | 0.5 Hz + event-triggered | The EWMA skill vector (§4.2), recent trajectory, per-behavior probes |

Each digest is **≤ ~1.5k tokens** (we keep total request well under the 32k/64k budgets),
built from short-key JSON plus one compact narrative line — narrative because Jev reads text,
and a sentence like *"player backed into the nw corridor facing away from the squad, took 18
dmg in 3 s, has 9 shells"* is worth more to a language model than 30 raw numbers alone.
Both are sent: numbers for precision, narrative for grounding.

**Decision Composer** — deterministic, unit-testable:

- Threshold tables per decision (e.g. `flank_left` requires `p > 0.55` *and* a verified
  open flank region from the link graph *and* squad size ≥ 2).
- **Hysteresis**: an order must persist ≥ N tics (`for=`) before re-evaluation; mode flips
  (e.g. director phase) require 2 consecutive confident readings. This converts a jittery
  10 Hz classifier into stable command streams — the thing the single-directive-slot
  protocol actually wants.
- **Safety gates** ported from the rule director (never spawn into LOS, never horde when
  player hp < X, cap live monsters) — these were proven in `-director` and stay as hard
  code, *not* model judgment.
- **Order mapping**: probability argmax → exact verified `AI_OrderFromName` vocabulary:
  `none, chase, hold, fallback, flank_left, flank_right, focus_fire, ambush, use_door`
  (note the exact aliases `flank_left`/`flank_right` — a spelling error here silently
  falls back, per `p_ai_llm.c`).

### 3.2 Call inventory (what "more decisions" concretely means)

| # | Call | Cadence | Questions (examples) |
|---|---|---|---|
| 1 | **Squad tactics** | 2–4 Hz | `squad_stance` (choice: chase/hold/flank_left/flank_right/fallback/ambush/focus_fire) · `flank_route_open` (noul) · `player_positioning_vulnerable` (score 0–3) · `fire_discipline` (score 0–2) |
| 2 | **Director pacing** | 1–2 Hz | `phase` (choice: buildup/peak/fade/relax) · `horde_now` (noul) · `spawn_species` (choice over roster incl. Heretic/Hexen packs if loaded) · `horde_size` (score 0–4) · `spawn_corridor` (choice over candidate regions) |
| 3 | **Item economy** | event + 0.5 Hz | `drop_medkit` (noul) · `drop_ammo` (noul) · `bait_item` (noul — drop a visible pickup to lure the player into an ambush region) |
| 4 | **Buddy command** | 0.5–1 Hz | `buddy_stance` (choice: engage/defend/hold/regroup/retreat/goto/grab) · `buddy_in_danger` (noul) |
| 5 | **Player-model probes** | 0.5 Hz + events | §4 — the learning loop |
| 6 | **Wake/interrupt** | event | `wake_sleepers` (noul) · `cancel_orders` (noul) |

Peak ≈ 8–10 calls/s, each mixing 3–6 questions. At ~250–900 input tokens/call that is
roughly **8–10k tokens/s ≈ $1.3–1.6/hour** of input tokens, output free — vs ~$7/hour for the
10-qps demo TypeSafe themselves flagged as worryingly expensive. If even that is too much,
cadences are config knobs; halving all cadences quarters cost (§6).

### 3.3 Why the NPCs get *smarter*, not just faster

1. **Decision budget explodes.** Local LLM: ~1 composite decision / 5–20 s, one blob of
   prose. JEV: ~10 calls/s × 4–6 questions = **40–60 calibrated judgments per second**
   available, each independently thresholded and hysteresis-stabilized.
2. **The state gets bigger because it can.** The old prompt had to be a summary because
   tokens were slow and expensive. Now the digest ships: per-squad geometry, link-graph
   flank candidates, momentum/facing trends, sound-event history, stress derivatives —
   variables the old pipeline literally could not afford to serialize, let alone reason
   about per-monster-squad.
3. **Probabilities enable graded behavior.** Instead of `flank` on/off, the composer blends:
   squad moves on a flank vector weighted by `p(flank_open)`, holds firing discipline
   scaled by `score(fire_discipline)`, and the difficulty controller biases spawn species
   toward counters of whatever the player is weakest against (§4.4).
4. **No parse failures, no hallucinated monster names.** Every output is one of the
   constants we defined; the composer's mapping table is exhaustive and type-checked.

---

## 4. The learning loop — "gets harder by learning player patterns, real time"

This is the part a rules FSM can't do and a slow LLM could only fake. We build an explicit
**player model** and close the loop through Jev itself, so the difficulty adaptation is
*calibrated judgment on evidence*, not a hidden formula.

### 4.1 Event ring (bridge-side, 60 s window, ~1–4 KB)

Derived from both observe streams + engine signals: damage taken/dealt events, health/armor
deltas, ammo deltas, pickups, kills by weapon, hits vs shots (rate-of-fire accounting per
weapon), deaths, deaths-by-monster-type, door/key progress, time-in-combat vs time-rotating,
position heat map (region occupancy), dodge events (projectile spawned → player displacement
before impact), retreats, ambushes survived, medkit use timing.

### 4.2 Skill vector — EWMA statistics, never sent raw

Six named axes, each an exponential moving average (λ ≈ 0.05–0.2, tuned per axis), each
0–1, each with a confidence mass (observations count):

| Axis | Signal source |
|---|---|
| `aim` | hits/shots EWMA, time-to-kill vs monster HP pool |
| `movement` | dodge success rate, damage taken while mobile, strafe entropy |
| `resource` | ammo wasted on overkill, medkit hoarding vs use-at-threshold, armor discipline |
| `aggression` | engagement-initiation distance distribution, retreat frequency |
| `routing` | exit-route progress rate, backtracking frequency, dead-end time |
| `adaptability` | post-death time-to-first-kill, damage-taken trend within a level |

### 4.3 The JEV-mediated adaptation call (0.5 Hz + event-triggered)

```json
{
  "state": {
    "skill": { "aim": 0.81, "movement": 0.62, "resource": 0.44,
               "aggression": 0.73, "routing": 0.55, "adaptability": 0.77 },
    "skill_evidence": { "aim_obs": 214, "movement_obs": 61, "resource_obs": 88 },
    "trend": "movement dipped 0.09 over last 40s, aim steady",
    "recent": "cleared two ambushes without damage, but burned 60% of cells on a pain elemental",
    "difficulty": { "current": 3.4, "session_peak": 4.1, "floor": 1, "ceiling": 7 },
    "fun": { "stall_seconds": 12, "deaths_last_5min": 1, "downward_momentum": false }
  },
  "model": "jev-latest",
  "questions": {
    "skill_is_outpacing_difficulty": { "type": "noul",
      "instructions": "Is this player's demonstrated skill clearly above what the current difficulty tier demands?",
      "criteria": { "true": "Sustained high aim/movement scores, few deaths, clearing encounters well under expected time",
                     "false": "Skill metrics at or below tier expectations, or evidence is thin" } },
    "difficulty_step": { "type": "score",
      "instructions": "How much should the difficulty tier move this interval?",
      "criteria": [ "hold or ease off", "nudge up (+0.25)", "clear step up (+0.5)", "surge (+0.75, dominance across axes)" ] },
    "strongest_axis": { "type": "choice",
      "instructions": "Which skill axis is this player's clearest strength right now?",
      "criteria": { "aim": "...", "movement": "...", "resource": "...", "aggression": "...", "routing": "...", "adaptability": "..." } },
    "weakest_axis":   { "type": "choice", "instructions": "...", "criteria": { "..." : "same six" } },
    "boredom_risk":   { "type": "noul",
      "instructions": "Is the player showing boredom/stall patterns (no threat engagement, low damage exchange)?",
      "criteria": { "true": "Long no-combat stretches, low kill/damage rates, backtracking without purpose",
                     "false": "Regular engagement cadence" } },
    "frustration_risk": { "type": "noul",
      "instructions": "Is the player showing frustration patterns (repeated deaths at same spot, rage-weapon-switching)?",
      "criteria": { "true": "Multiple deaths at the same region or to the same monster type, health never above 30",
                     "false": "Deaths are varied and recovery is stable" } }
  }
}
```

**The composer owns the actual numbers.** Jev's outputs are *direction* with *confidence*;
the controller applies bounded steps (`±0.25`/interval, rate-limited to ±1.0/minute, floor
1.0, ceiling set per launch flag), then the resulting difficulty tier re-weights:

- pacing call thresholds (`horde_now` trigger, phase dwell times),
- spawn menu weights (species that counter the player's `strongest_axis` — e.g. high `aim`
  ⇒ more fast/erratic movers like lost souls & pain elementals; high `movement` ⇒
  hitscan-heavy setups that punish open ground),
- squad tactic aggressiveness (score thresholds shift by up to ±0.1),
- item economy (a struggling player gets `spawn item=` mercy drops *sooner*; a dominating
  player gets bait items instead of free medkits).

**Crucially, the model never gets to "decide difficulty" as a free variable** — it says
whether the evidence shows dominance and which axis is strongest; code converts that into a
bounded, auditable step. This is exactly TypeSafe's "atomic questions, composed in code"
pattern, and it's what makes the system debuggable.

### 4.4 Pattern recognition beyond difficulty (the "learns patterns" part)

Two lighter mechanisms ride the same event ring:

- **Habit probes** (0.2 Hz, 1–2 questions): "Does this player habitually check corners
  before doorways?" / "Does this player prefer hitscan weapons at range?" The answers bias
  **ambush placement** (`spawn_corridor` candidates get re-ranked toward spots that exploit
  the player's *un*checked angles) and monster species mixing (prefer ranged attackers vs a
  corner-checker's blind approach, prefer melee rushers vs a range-hugger).
- **Memory across sessions** (`run/jev_player_model.json`): the skill vector's long-run
  component (slower EWMA, λ≈0.01) persists between sessions, so a returning veteran starts
  at tier 3–4 instead of 1 — the thing no rules-FSM director can do. LRU-capped per-IWAD,
  and the file is plain JSON the user can delete to reset.

### 4.5 Honest-uncertainty behavior

Because Jev's calibration is the point: when `skill_evidence` mass is low (first minute of a
session) the controller **ignores** the adaptation call (requires ≥ N observations per axis)
and holds a neutral tier. When `boredom_risk` and `frustration_risk` both fire, frustration
wins — never stack difficulty on a struggling player. This is trivial to unit-test because
the policy is code, and the JEV answers are just numbers with known calibration behavior.

---

## 5. Implementation plan

### Phase 0 — Access & scaffolding (½ day)
- [ ] Jev early-access key (`TYPESAFE_API_KEY`), confirm `jev-latest` reachability + measured
      p50/p95 latency from the target machine (we log every call's wall time — this becomes
      the HUD stat and the basis for cadence tuning).
- [ ] Clone BuddyDoom; build per its CMake path (Windows: MSVC + SDL3, `build_all_win.bat`).
- [ ] Smoke the **existing** protocols with `nc`-style scripts: `observe` on 31666 and 31700,
      one `act order=hold ids=all for=70`, one `spawn type=imp count=1`. No engine changes
      needed in this phase — that's a feature of the architecture.

### Phase 1 — `jev_bridge` skeleton (1–2 days)
- [ ] Node 20+ TS project in `jev/`; tiny HTTP client for the endpoint with exponential
      backoff on 429/529, a concurrency pool (default 4), call logging (JSONL: digest hash,
      latency, answers, `model` string from response).
- [ ] TCP clients for both listeners with auto-reconnect; merged world model; JSON schema
      validation of `observe` payloads (defensive — fields "can be absent" per the docs).
- [ ] Offline mode: `--replay obs.jsonl` drives the whole pipeline from recorded
      observations. This is the development superpower — build the brain against recorded
      gameplay, not against a live game.

### Phase 2 — State Compiler + Decision Composer (2–3 days)
- [ ] Digest builders (squad/pacing/buddy/player) with unit tests on synthetic worlds.
- [ ] Composer: threshold tables, hysteresis, safety gates, exact order-vocabulary mapping,
      `for=` lifetimes. Property tests: **no emitted line may contain an order name outside
      the verified vocabulary, an id outside the current roster, or a spawn exceeding the
      live cap** — invariants the old prose-parser could never guarantee.
- [ ] Wire pacing watchdog correctly: pacing commands at least every ~15 s while
      authoritative, else intentionally fall back (§5.4).

### Phase 3 — Tactics online (2–3 days)
- [ ] Squad tactics loop (call #1) → `act` lines. Acceptance test: in `-warp 1 1`, imp squad
      executes flank_left against a verified open flank ≥ 70% of opportunities, and never
      re-orders the same squad more than once per 2 s (hysteresis proof).
- [ ] Buddy loop (call #4) → `buddy order=` lines.

### Phase 4 — Director + item economy (2–3 days)
- [ ] Pacing loop (calls #2–3): phases, species choice (roster-aware incl. Heretic/Hexen
      packs when loaded), hidden-spawn candidate scoring, bait items, mercy drops.
- [ ] A/B harness: `-aidemo` (scripted) vs JEV director on the same map, logging
      time-to-peak-intensity, damage-dealt-per-death, item-drop efficiency. This produces
      the demo numbers for the README.

### Phase 5 — Learning loop (3–4 days)
- [ ] Event ring + skill vector EWMAs with observation-count confidence.
- [ ] Adaptation call (#5) + bounded difficulty controller + per-axis spawn counters.
- [ ] Habit probes → ambush placement bias.
- [ ] Session persistence + `-jevreset` console/flag to wipe the player model.

### Phase 6 — Polish, HUD, release (2–3 days)
- [ ] Repurpose `gpumon`'s panel → **JevMon**: live calls/s, p50/p95 latency, current tier,
      skill radar, per-question probability sparklines (great demo material).
- [ ] Launcher integration (add JEV toggle + API-key field to `buddydoom_config`; key stays
      in env/config, never in the repo).
- [ ] Graceful degradation ladder (§5.4) end-to-end tested; README + `docs/JEV_DIRECTOR.md`
      (the runtime-facing doc, once reality diverges from this design — it will).

### Total: roughly 3–4 weeks part-time; Phases 0–3 alone give a working demo.

### 5.4 Degradation ladder (non-negotiable, ported from AGENT_CONTROL §8)

1. Jev error/timeout (≥ 3 consecutive) → freeze new orders; existing `for=` lifetimes expire
   naturally; **rule FSM resumes automatically** after its ~15 s pacing gap (engine behavior
   we deliberately rely on).
2. Bridge up, key invalid / quota out → same, plus HUD notice via director voice channel.
3. Bridge down entirely → engine untouched; `-director` rule mode is the runtime floor.
   The game never blocks on the socket (the listeners are poll-based by design — we keep it).
4. Latency spike (> 2 s p95 for 10 s) → automatic cadence halving, then freeze.

---

## 6. Cost & latency budget

| Cadence preset | Calls/s | Avg input tok/call | $/hour | p95 decision age |
|---|---|---|---|---|
| Aggressive (demo) | ~10 | ~700 | ~$1.50 | ≤ 0.5 s |
| Default | ~5 | ~700 | ~$0.75 | ≤ 1 s |
| Economy | ~1.5 | ~700 | ~$0.22 | ≤ 2 s |
| Offline | 0 | — | $0 | rule FSM |

(At $0.042/MTok input, output free.) Every cadence is a config knob; the hysteresis layer
guarantees behavioral stability even at economy rates because orders persist via `for=`.
Tuning tip: the digest, not the questions, dominates tokens — trim the narrative line first.

---

## 7. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Network latency/failure mid-fight | Degradation ladder §5.4; `for=` lifetimes keep orders alive through gaps; rule FSM as floor |
| Rate limits at aggressive cadence | Client pool + backoff (documented 429/529 behavior); cadence presets; coalesce digest updates |
| Early-access model changes under `jev-latest` | Log response `model`; pin thresholds to `jev-1.13.0`-era calibration; re-tune suite is unit tests on recorded digests |
| Questions drift toward "one big judgment" (reliability cliff) | Architectural lint: every question must be answerable from its digest slice alone; composer owns all composition |
| Cost creep from added questions | Questions are near-free (parallel); only state size matters — budgets enforced in the State Compiler with hard caps |
| IDs staleness across observations | Composer consumes only the freshest observe; ids re-validated against current roster every cycle (documented engine caveat) |
| Pacing watchdog starvation | Pacing heartbeat command every ≤ 15 s while authoritative; else deliberate rule-FSM fallback |
| Overfitting difficulty to one session | Two-timescale memory (fast EWMA + slow persistent component); bounded steps; evidence-mass gating |

---

## 8. Why this is technically impressive (the honest version)

- **Architecture inversion**: from "LLM as director" (slow brain, tiny state, prose parsing)
  to "model as decision primitive" (fast typed judgments, rich compiled state, deterministic
  composition). The AI finally sits *inside* the game loop instead of beside it.
- **40–60 calibrated judgments/second** across six concurrent decision surfaces — with a
  guarantee the old stack couldn't dream of: zero malformed outputs, zero hallucinated
  directives, by construction.
- **A real adaptive-difficulty system**: six-axis skill model with evidence-weighted
  confidence, two-timescale session memory, counter-species spawn selection, and
  frustration/boredom guardrails — implemented as *auditable code consuming calibrated
  probabilities*, not a vibes-based prompt.
- **Zero engine changes required** for the core loop: BuddyDoom's TCP surfaces were
  already designed for an external brain; we're swapping the brain, not the body. The one
  engine-side patch worth doing later (optional Phase 7) is a `region_heat` field +
  projectile-dodge event in `AI_Serialize` to sharpen the habit probes.
- **Replayable, testable, cheap**: recorded-observation replay mode, property-tested
  composer invariants, and a bill that's a rounding error next to the GPU rig the Ollama
  path required.

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
| Memory substrate | Engine-side C module: `files/p_nemesis.c` owning a persistent weight/weapon-bias struct, written to `nemesis_memory.dat` (sibling of `buddydoom.cfg`, matching project convention) | Survives bridge crashes, can join savegames, zero external dependency for memory, fits repo conventions. Bridge-side memory (original §4.5) is lost if the process dies. |
| Kill attribution | Hook `P_KillMobj` in `files/p_inter.c`: log the tactic in effect + the weapon that landed the kill at the moment of death | Ground truth. Inferring kills/weapons from `observe` polling deltas (original §4.1) is lossy and fragile. |
| Death-time reasoning | On enemy death, hand recent tactics + kill weapon + current weight table to the model, which *judges* the weight shift (pattern confidence, death history) instead of a hardcoded penalty | Better learning signal per event; Jev makes it ~free (~$0.00003/call). |
| Validation | Standalone browser prototype already runs the full mechanism end-to-end (fuzzy decisions blended with persistent weights, weapon-bias tracking, death-triggered reinforcement, decay) | Empirical validation beats spec. |

### 9.2 What the continuous layer adds (kept)

Death-triggered learning alone is sparse: a good player dies twice a level — two learning
events in 30 minutes — while dodging, wasting ammo, and routing constantly. The EWMA skill
vector (§4.2) samples that continuous behavior, and the difficulty controller (§4.3–4.4)
turns it into pacing/species/item adaptation between deaths. The two memories are
complementary timescales:

- **Slow memory** = nemesis weight table (per monster-type tactic weights + weapon bias),
  death-triggered, persistent across sessions.
- **Fast memory** = skill vector, continuous EWMA, session-scoped with a slow persistent
  component.

Both are serialized into every Jev state: the weight table via `AI_Serialize()` (numbers —
trivially inside the token budget), the skill vector from the bridge.

### 9.3 Jev changes the original nemesis math

1. The ~1.4 s/round cadence constraint was an Ollama number. At 70–500 ms, director rounds
   go from ~0.7/s to 2–10/s, and the death-time call is always affordable — the "second job"
   framing stops being a compromise and becomes always-on.
2. Digest compilation (§3.1) is still required either way: raw world state will not fit
   Jev's ~64k budget, and digests are what keep calls at ~250–900 tokens.

### 9.4 Merged engine touchpoints (resolved)

| File | Change |
|---|---|
| `files/p_nemesis.c` (new) | Persistent weight/weapon-bias struct; decay pass; floor/ceiling so the AI can't collapse to permanently passive; load/save `nemesis_memory.dat` |
| `files/p_inter.c` | `P_KillMobj` hook: record tactic-in-effect + killing weapon into the nemesis log; trigger the death-time Jev judgment (via bridge) |
| `files/p_ai_llm.c` | `AI_Serialize()`: append the current weight table (+ skill-vector fields forwarded from the bridge) to the observation JSON |
| `files/p_ai_director.c` | Unchanged: remains the fallback for both layers |
| `jev_bridge` | Sends both memories in the state; applies bounded weight-shift recommendations; writes back through the same observation cycle |

Resolved protocol facts for wiring (verified from `docs/MONSTER_AGENT_GUIDE.md` and
`docs/AIPLAYER.md`): order vocabulary is exactly `none, chase, hold, fallback, flank_left,
flank_right, focus_fire, ambush, use_door` (`AI_OrderFromName`); monster ids are
registry-slot+1 and must be refreshed from every observation; one directive slot per monster
(`after=` delays, never queues); only `spawn` / `spawn item=` / `director relax` reset the
rule-FSM watchdog (~15 s); one TCP client per listener (31666 director, 31700 player agent).

### 9.5 Plan impact

- **New Phase 5b (1–2 days)**: `p_nemesis.c` + `P_KillMobj` hook + weight-table in
  `AI_Serialize()` + death-time Jev call. The browser prototype's semantics port directly.
- Phase 5 (continuous loop) is unchanged and now feeds the same state payload.
- §4.5's session persistence (`jev_player_model.json`) is superseded by
  `nemesis_memory.dat` for the slow component; the bridge keeps only the fast EWMA cache.

---

## 10. Final architecture: the Nemesis Fabric (hybrid of §3–4 and §9)

> §10 supersedes the §9.5 plan impact. Principle: **memory and ground truth live in the
> engine; intelligence lives in the bridge; Jev is a stateless judge between them.**
> Each tier degrades independently and neither can block the 35 Hz tic loop.

### 10.1 The three tiers

**Tier 0 — Engine (C, deliberately dumb).**

| Piece | Detail |
|---|---|
| `files/p_nemesis.c` (new) | Per-monster-type table: tactic weights (8 AIO orders) + weapon bias (per player weapon). Decay pass + floor/ceiling clamps. Load/save `nemesis_memory.dat` (sibling of `buddydoom.cfg`). **Versioned** savegame block via `G_Agent_Archive`-style hooks — fixes the versioning gap the docs flag for the agent block. |
| Ground-truth event log | Hook `P_DamageMobj` (every hit: target type, damage, attacker weapon) **and** `P_KillMobj` (death context: tactic in effect, killing weapon). Aggregated counters per `(monster-type, weapon)` since last observe + last ~8 discrete events. A small ring in C — no pointers, no allocation in the tic path. |
| `AI_Serialize()` extension | Append the compact table + event digest. Numbers only, ~100–200 tokens — rides along free inside the digest budget. |
| New protocol mutation | `nemesis propose=<type> <field>=<delta> [...]` — engine **clamps** (`|delta| ≤ max/apply`, weights ∈ [floor, ceiling]) and applies; replies `ok`. A buggy or hostile bridge cannot unbalance the table beyond the clamps. Non-blocking, poll-based, consistent with the existing line protocol. |
| Native consumption | `p_ai_director.c` rule tactics and spawn-species selection read the table **directly** — learned behavior with zero model calls (the outage-proof floor). |

**Tier 1 — Bridge (TS, all the intelligence).**
State Compiler digests (§3.1, mandatory — this is what kills the raw-state cost con);
EWMA skill vector (§4.2) now computed **from the engine's ground-truth event ring** instead
of polling inference; the full Jev call inventory (§3.2); Decision Composer (§3.1) with
hysteresis + safety gates; and the write-back path — composer-approved, clamped
`nemesis propose` lines after every adaptation cycle.

**Tier 2 — Jev (stateless judge).**
Unchanged: atomic questions, calibrated probabilities, no memory, no prose.

### 10.2 How every prior con dies

| Prior con (source) | Killing mechanism |
|---|---|
| A: memory lost if bridge crashes (§9.1 adopted B for this) | Tier 0 owns the table; bridge only *proposes* deltas; rule director consumes it natively. A bridge crash loses at most one adaptation cycle. |
| A: kill/weapon attribution inferred from polling is lossy | `P_DamageMobj`/`P_KillMobj` hooks give per-hit weapon ground truth. The bridge **never infers** — it reads events. |
| B: death-triggered learning is sparse (2 events/level) | The damage hook makes the stream **continuous**: a shotgunner feeds dozens of ground-truth (type, weapon) counters per fight. Death-time Jev judgment stays as the large reinforcement; damage events are the small frequent ones. |
| B: raw `AI_Serialize` state too expensive (~32 KB buffer ≈ 8–10k tok) | Digest compiler is mandatory in Tier 1; the table rides along at ~100–200 tokens. |
| B: slow C iteration (MSVC + SDL3 toolchain for every change) | Tier 0 is intentionally dumb — struct + clamp + io + hooks, write-once. All retuning, thresholds, prompts live in Tier 1 where iteration is TS-fast with replay mode. |
| A: hidden difficulty scalar is invisible to the player | The nemesis personality (weapon bias, per-type grudges) is the *visible* layer; JevMon dashboards both tiers. |
| B: savegame fragility (agent block not independently versioned) | New nemesis save block ships with an explicit version tag from day one. |
| A: learned behavior needs the API | Rule director consumes the table with zero model calls — deadliness persists through full outage. |

### 10.3 Cost & call inventory deltas

- All §6 budgets hold: digests ~250–900 tok/call, cadence presets unchanged.
- Damage/death events are engine-local — **zero** extra Jev calls; they enrich existing
  calls' state instead.
- Death-time judgment call: event-triggered (~2–10/level) at ~$0.00003 each. Free.
- Adaptation call (§4.3) unchanged at 0.5 Hz; its output now lands as clamped
  `nemesis propose` deltas in addition to pacing/species/item re-weighting.
- New state fields (table + event digest) add ~100–200 tok to every call — inside budget,
  and they make *every* other call smarter (tactics see the grudge table too).

### 10.4 Fallback matrix (the "no dead modes" guarantee)

| Bridge | API | Behavior |
|---|---|---|
| up | up | Full fabric: squad tactics, pacing, items, buddy, continuous + death-time learning, clamped write-back. |
| up | down | §5.4 ladder: cadence halves, then freeze; bridge keeps digesting events and holds proposed deltas until recovery. |
| down | up | Engine keeps last table; rule director consumes it (biased tactics + biased species) — **learned behavior persists**. Game never blocks (poll-based listeners). |
| down | down | Pure rule director + persistent table from `nemesis_memory.dat`. Still visibly smarter than vanilla; memories intact for next session. |

### 10.5 Updated plan

- **Phase 2.5 (new, 1–2 days)** — Tier 0: `p_nemesis.c` (table, clamps, decay, io),
  `P_DamageMobj`/`P_KillMobj` hooks, `AI_Serialize()` extension, `nemesis propose` line,
  versioned save block, rule-director consumption of the table.
- **Phase 5 (revised, 2–3 days)** — Tier 1 learning: event-ring consumers, EWMA vector
  from ground truth, adaptation call, composer write-back via `nemesis propose`, death-time
  judgment call. The browser prototype's semantics port here, upgraded from
  death-triggered to damage-continuous.
- Everything else (Phases 0–4, 6) unchanged; every phase now also benefits from the richer
  observation.

### 10.6 Residual tradeoffs (honesty clause)

Not cons, but priced-in costs: one small engine patch is required up front (Tier 0 can't be
prototyped in TS — mitigated by keeping it dumb and write-once); two processes to debug
(mitigated by replay mode + the JevMon panel showing both tiers); the `nemesis propose`
line is a bespoke protocol extension to document in `MONSTER_AGENT_GUIDE.md` upstream if
contributed back.
