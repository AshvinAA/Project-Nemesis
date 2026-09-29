"""All agent knobs in one place (mirrors the jev/ bridge's config.ts convention)."""

# --- Engine link -----------------------------------------------------------
HOST = "127.0.0.1"          # engine listeners are loopback-only by design
DIRECTOR_PORT = 31666       # -aidirector listener (single client!)
SOCKET_TIMEOUT = 0.05       # seconds per blocking recv; keeps the loop 10 Hz
RECV_DEADLINE = 1.0         # max seconds to wait for one full reply line
RECONNECT_DELAY = 0.5       # seconds between reconnect attempts

# --- Agent loop ------------------------------------------------------------
POLL_HZ = 10.0
POLL_PERIOD = 1.0 / POLL_HZ
ORDER_REFRESH_POLLS = 5     # re-issue the directive every N polls (~0.5 s);
                            # engine default for=70 (~2 s) would outlive stale
                            # decisions, so we send short for= and refresh.
ORDER_FOR_TICS = 35         # ~1 s directive lifetime (engine is 35 Hz)
RESPAWN_COOLDOWN = 2.0      # seconds between spawn attempts (avoid spam)

# --- World facts (verified from engine source) -----------------------------
NEMESIS_TYPE = "shotgunguy" # AI_TypeName vocabulary (p_ai_llm.c)
NEMESIS_SPAWN_ARG = "shotgun"  # P_Director_TypeByName vocabulary
NEMESIS_SPAWNHEALTH = 30    # vanilla spawnhealth of MT_SHOTGUY

# --- Episode / training (Phase 3; declared now, used later) ----------------
GAMMA = 0.95
ALPHA_START = 0.3
ALPHA_FLOOR = 0.1
EPSILON_START = 0.2
EPSILON_FLOOR = 0.05
EPSILON_DECAY_EPISODES = 30
QTABLE_PATH = "nemesis/rl/qtable.json"
