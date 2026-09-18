# JEV Bridge — runtime guide

The JEV Director replaces BuddyDoom's local-Ollama director with TypeSafe AI's
**Jev** (System One model): state + typed questions in, calibrated probabilities
out, ~70–500 ms per call. Full design: `docs/JEV_DIRECTOR_WORKFLOW.md` (§10 is
the shipped architecture).

## Architecture (three tiers)

```
Tier 2 — Jev API          stateless judge; atomic questions; calibrated answers
Tier 1 — jev/ (bridge)    State Compiler digests · Decision Composer · learning loop
Tier 0 — engine (C)       p_nemesis.c memory + P_DamageMobj/P_KillMobj ground truth
```

Memory and ground truth live in the **engine**; intelligence lives in the
**bridge**; Jev is a stateless judge between them. Every Jev-driven write-back
is a *proposal* the engine clamps (`p_nemesis.c`). The rule director consumes
the learned table directly, so behavior stays smart with the API down.

## Running

1. Build BuddyDoom (see upstream README; CMake finds SDL3, or MSVC nmake) and
   launch with the director listener:

   ```
   buddydoom.exe -warp 1 1 -skill 4 -aidirector 31666
   ```

   Optionally add `-aiplayer 31700` for full player-agent observations.

2. Bridge:

   ```
   cd jev
   npm install
   set TYPESAFE_API_KEY=...     (optional; without it use --mock)
   npm run dev                  # live: cadence=default
   npm run dev -- --cadence aggressive   # or economy
   npm run dev -- --mock        # deterministic offline Jev (full pipeline)
   ```

3. Replay recorded observations through the identical pipeline:

   ```
   npm run replay               # log/observations.jsonl (mock answers)
   npm run replay:dry           # digest compile only, no answers
   ```

## What it emits

Bridged decisions become exact engine protocol lines (`p_ai_llm.c` vocabulary):
`act order=... ids=... for=...`, `buddy order=...`, `spawn type=... count=...`,
`spawn item=...`, `director relax`, and the new `nemesis propose=...` write-back.
All emitted lines are logged to `log/emitted.jsonl`; every Jev call (latency,
status, returned model id) to `log/calls.jsonl`.

## Engine side (Tier 0)

- `files/p_nemesis.c/.h` — per-monster-type tactic weights + weapon bias,
  decay pass, clamped proposals, `nemesis_memory.dat` persistence (versioned).
- `files/p_inter.c` — `NEM_NoteHit` / `NEM_NoteKillM` ground-truth hooks.
- `files/p_ai_llm.c` — serializer appends the `nemesis` block to `observe`;
  parses `nemesis propose=...`; decay pass in the ticker; rule tactics and
  spawn mixing consume the learned table (no model needed).
- `files/i_system.c` — table saved on quit.

Console check: after a few kills, `observe` on :31666 shows `"nemesis":{"rows":[...]}`.

## Safety properties (tested in jev/test)

- No order outside the engine's exact vocabulary is ever emitted.
- No id outside the current monster roster is ever targeted.
- Spawns are capped and blocked under critical player stress (mercy items instead).
- Weight proposals are clamped in C; the bridge cannot unbalance the table.
- Difficulty steps are bounded, evidence-gated, frustration-first.
