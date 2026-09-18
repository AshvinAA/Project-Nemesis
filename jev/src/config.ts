// config.ts — all knobs live here. Cadence presets, thresholds, hysteresis.
// Tuning happens here, never in the engine (Tier 0 is deliberately dumb).

export interface BridgeConfig {
  typesafe: {
    apiKey: string;          // from TYPESAFE_API_KEY env
    endpoint: string;
    model: string;           // log the returned `model`; pin thresholds per version
    concurrency: number;     // in-flight call cap
    timeoutMs: number;
  };
  game: {
    directorPort: number;    // -aidirector listener (default 31666)
    playerPort: number;      // -aiplayer listener (default 31700)
    host: string;
  };
  cadence: {
    squadMs: number;         // tactics call interval
    pacingMs: number;        // director pacing call interval
    buddyMs: number;         // buddy stance call interval
    playerModelMs: number;   // adaptation call interval
    pacingHeartbeatMs: number; // must stay << 15000 (rule FSM watchdog)
  };
  composer: {
    actThreshold: number;    // min probability to emit an act order
    actForTics: number;      // directive lifetime (default 70 ~ 2 s, engine default)
    orderHoldMs: orderHold;  // hysteresis: min interval between re-orders per squad
    phaseConfidence: number; // min confidence to flip director phase read
    maxSpawnCount: number;   // hard cap per spawn command (engine allows 8)
  };
  learning: {
    ewmaLambda: number;      // fast skill EWMA
    minEvidence: number;     // min observations before adaptation is trusted
    difficultyFloor: number;
    difficultyCeiling: number;
    maxStepPerInterval: number;
    maxRatePerMinute: number;
  };
  budget: {
    maxDigestTokens: number; // hard cap enforced by the State Compiler
    charsPerToken: number;   // rough estimate for budget guard
  };
  logDir: string;
  dryRun: boolean;           // no API key -> log calls instead of sending
}

export type CadencePreset = 'aggressive' | 'default' | 'economy';

interface orderHold { squad: number; buddy: number; }

const PRESETS: Record<CadencePreset, Pick<BridgeConfig['cadence'], 'squadMs' | 'pacingMs' | 'buddyMs' | 'playerModelMs'>> = {
  aggressive: { squadMs: 250,  pacingMs: 500,  buddyMs: 1000, playerModelMs: 2000 },
  default:    { squadMs: 400,  pacingMs: 800,  buddyMs: 1500, playerModelMs: 2000 },
  economy:    { squadMs: 1000, pacingMs: 2000, buddyMs: 3000, playerModelMs: 5000 },
};

export function loadConfig(preset: CadencePreset = 'default'): BridgeConfig {
  const apiKey = process.env.TYPESAFE_API_KEY ?? '';
  const p = PRESETS[preset];
  return {
    typesafe: {
      apiKey,
      endpoint: 'https://api.typesafe.ai/v1/systemone',
      model: 'jev-latest',
      concurrency: 4,
      timeoutMs: 4000,
    },
    game: {
      directorPort: 31666,
      playerPort: 31700,
      host: '127.0.0.1',
    },
    cadence: {
      squadMs: p.squadMs,
      pacingMs: p.pacingMs,
      buddyMs: p.buddyMs,
      playerModelMs: p.playerModelMs,
      // The rule FSM resumes after ~15 s without a pacing command; heartbeat well under.
      pacingHeartbeatMs: 5000,
    },
    composer: {
      actThreshold: 0.55,
      actForTics: 70,
      orderHoldMs: { squad: 2000, buddy: 3000 },
      phaseConfidence: 0.6,
      maxSpawnCount: 8,
    },
    learning: {
      ewmaLambda: 0.08,
      minEvidence: 12,
      difficultyFloor: 1,
      difficultyCeiling: 7,
      maxStepPerInterval: 0.25,
      maxRatePerMinute: 1,
    },
    budget: {
      maxDigestTokens: 4000,
      charsPerToken: 4,
    },
    logDir: 'log',
    dryRun: apiKey === '',
  };
}
