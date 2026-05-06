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