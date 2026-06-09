// Shared event-kind and tournament-status string constants.
// Server emits these verbatim; treat as a wire-protocol enum.
// Cross-ref: server/sturddle_view/tournament/{orchestrator,fastchess,store}.py

export const EVT_PREFIX = "tournament_";
export const EVT = {
  STATUS: "tournament_status",
  UPDATE: "tournament_update",
};

// Inner payload kinds carried by tournament_update events.
export const KIND = {
  PROXY_STARTED:  "proxy_started",
  PROXY_ENDED:    "proxy_ended",
  PROXY_PAIRED:   "proxy_paired",
  PROXY_UNPAIRED: "proxy_unpaired",
  GAME_FINISHED:  "game_finished",
  GAME_RECONCILED: "game_reconciled",
  RUNNER_LOG:     "runner_log",
  RUNNER_CRASH:   "runner_crash",
  DONE:           "done",
  STOPPED:        "stopped",
};

export const STATUS = {
  RUNNING: "running",
  STOPPED: "stopped",
  DONE:    "done",
  FAILED:  "failed",
};

// SPRT conclusion carried in sprt.status.
// Cross-ref: server/sturddle_view/tournament/pgn_stats.py (SPRT_*).
export const SPRT = {
  H0:       "H0",
  H1:       "H1",
  CONTINUE: "continue",
};

// Periodic refresh cadence for the selected tournament while running.
// Catches WS gaps (reconnects, missed payloads) and pulls a fresh
// games-played count from the server (read from fastchess's config.json
// per request -- there is no live push for that counter).
export const POLL_INTERVAL_MS = 5000;
