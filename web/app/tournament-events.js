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

// Lines worth surfacing first from a runner_crash stderr_tail.
const CRASH_RE = /error|fatal|fail/i;
// How long a crash toast stays up -- long enough to read a stderr line.
export const CRASH_TOAST_DURATION_MS = 10000;

// Pick the most informative line from a runner_crash payload: first
// error-ish stderr line, else the first line, else the exit code.
export function crashErrorLine(payload) {
  const tail = payload?.stderr_tail || [];
  return tail.find((l) => CRASH_RE.test(l)) || tail[0] || `exit code ${payload?.rc}`;
}

// Effective SPRT params when a stored default is empty/partial -- mirrors the
// server's _SPRT_DEFAULTS, applied before a tournament starts.
export const SPRT_DEFAULTS = { elo0: 0, elo1: 10, alpha: 0.05, beta: 0.05, model: "normalized" };

// SPRT param invariants enforced by fastchess at startup (NOT by the
// server's compute_sprt, which accepts any 0<alpha,beta<1): 0<alpha<1,
// 0<beta<1, alpha+beta<1, elo0<elo1. Each rule flags the fields it blames;
// returns the set of bad keys -- empty means valid. Shared by the SPRT chip
// popup (per-field red) and the create-time guard.
const inUnit = (x) => Number.isFinite(x) && x > 0 && x < 1;
const SPRT_RULES = [
  (p) => inUnit(p.alpha) || ["alpha"],
  (p) => inUnit(p.beta) || ["beta"],
  (p) => p.alpha + p.beta < 1 || ["alpha", "beta"],
  (p) => p.elo0 < p.elo1 || ["elo0", "elo1"],
];

export function sprtParamErrors(params) {
  return new Set(SPRT_RULES.flatMap((rule) => {
    const r = rule(params);
    return r === true ? [] : r;
  }));
}

// Normalize the live SPRT payload's bounds + verdict for the standings line and
// the info-wall meter (both read the same shape).
export function sprtVerdict(sprt) {
  return {
    lo: sprt.lower_bound, hi: sprt.upper_bound, llr: sprt.llr,
    concluded: sprt.status != null && sprt.status !== SPRT.CONTINUE,
    isH1: sprt.status === SPRT.H1,
  };
}

// Periodic refresh cadence for the selected tournament while running.
// Catches WS gaps (reconnects, missed payloads) and pulls a fresh
// games-played count from the server (read from fastchess's config.json
// per request -- there is no live push for that counter).
export const POLL_INTERVAL_MS = 5000;
