# Project Nemesis

**BuddyDoom + JEV Director**: id's 1993 DOOM (SDL3 fork) where the monster
director is TypeSafe AI's **Jev** — a "System One" decision model that returns
calibrated, typed decisions in ~100 ms — running a persistent **nemesis memory**:
the demons learn how *you* kill them and get harder in real time.

- `BuddyDoom/` — the engine (upstream fork). Our additions: `files/p_nemesis.c/.h`
  (learned weight table + ground-truth event ring, persisted to `nemesis_memory.dat`),
  damage/kill hooks in `files/p_inter.c`, nemesis observe/proposals in `files/p_ai_llm.c`,
  learned spawn/tactic bias in `files/p_ai_director.c`.
- `jev/` — the bridge (Node/TS): digests the game's observations, asks Jev atomic
  questions, composes decisions with deterministic safety gates, writes learned
  weight shifts back as engine-clamped proposals. Replay mode + mock Jev for
  offline runs. See `jev/README.md`.
- `docs/JEV_DIRECTOR_WORKFLOW.md` — the full design & build record.
- `HANDOFF.md` — **start here if you're new (human or AI)**: complete state,
  verified protocol facts, what broke and why, and the ordered next steps.

## Quick start

```sh
# 1. Build the game (MSVC + SDL3 or CMake; see BuddyDoom/README.md)
# 2. Launch with the director listener:
BuddyDoom/run/buddydoom.exe -warp 1 1 -skill 4 -aidirector 31666

# 3. Bridge:
cd jev && npm install
set TYPESAFE_API_KEY=your-key
npm run dev            # live (or: npm run dev -- --mock for offline)
npm run replay         # recorded-observation pipeline test
```

Engine builds without a Jev key or the bridge still work: the rule director
consumes the learned table natively, and nothing ever blocks the game loop.
