// Headless live-runner state for a single tournament. Owns the bug-prone
// data the tournament view layers render: active engine processes, live
// pairings, resolved games, and the capped event log. Pure data + mutation
// helpers -- no DOM, no windows, no fetching. The Arena workspace and the
// Studio perspective each drive these with their own orchestration (which
// view to repaint, coalescing, policy) over the same maps.

import { EVT, KIND, STATUS } from "./tournament-events.js";
import { APP_EVT } from "./app-events.js";

// Cap the in-memory event log; seenSeqs is kept in lockstep so the dedup
// set can't outgrow the visible log.
export const EVENT_LOG_LIMIT = 500;

export function createLiveState() {
  return {
    // proxy_id -> { engineName }
    activeProxies: new Map(),
    // proxy_id -> { pairId, proxyA, engineA, sideA, proxyB, engineB, sideB,
    // seq }. Both proxies in a pair map to the same info object; seq is the
    // proxy_paired event's _seq (null when seeded from REST).
    livePairings: new Map(),
    // High-water state_seq of applied REST snapshots (0 = none yet).
    stateSeq: 0,
    // pair_id -> { gameN, result, termination }. From game_reconciled; lets a
    // re-open mark resolved windows for frozen-rehydration.
    resolvedGames: new Map(),
    eventLog: [],
    // Server-stamped sequence numbers already in eventLog (backfill/live dedup).
    seenSeqs: new Set(),
  };
}

// Register a confirmed pairing from an event or REST payload; both
// proxies map to the same info object.
function setPairing(s, p, seq) {
  const info = { pairId: p.pair_id, proxyA: p.proxy_a, engineA: p.engine_a, sideA: p.side_a,
                 proxyB: p.proxy_b, engineB: p.engine_b, sideB: p.side_b, seq };
  s.livePairings.set(p.proxy_a, info);
  s.livePairings.set(p.proxy_b, info);
}

// Seed proxies/pairings from a REST detail payload. While RUNNING the API can
// lag WS events, so additions are additive and pairing removals seq-guarded:
// drop only pairings the snapshot postdates (seq <= state_seq) yet no longer
// lists -- dissolved during a WS gap. When not RUNNING replace authoritatively.
export function seedFromDetail(s, detail) {
  const seq = detail.state_seq ?? null;
  // A stale snapshot (older in-flight GET resolving late) must not
  // resurrect or remove anything a newer one already settled.
  if (seq != null) {
    if (seq < s.stateSeq) return;
    s.stateSeq = seq;
  }
  if (detail.status !== STATUS.RUNNING) s.activeProxies.clear();
  for (const p of (detail.proxies_active || [])) {
    if (p.proxy_id && !s.activeProxies.has(p.proxy_id)) {
      s.activeProxies.set(p.proxy_id, { engineName: p.engine_name || null });
    }
  }
  if (detail.status !== STATUS.RUNNING) s.livePairings.clear();
  if (detail.status === STATUS.RUNNING && seq != null) {
    const activePairs = new Set((detail.pairings_active || []).map((p) => p.pair_id));
    for (const [pid, info] of s.livePairings) {
      if ((info.seq ?? 0) <= seq && !activePairs.has(info.pairId)) {
        s.livePairings.delete(pid);
      }
    }
  }
  for (const p of (detail.pairings_active || [])) {
    if (s.livePairings.has(p.proxy_a) || s.livePairings.has(p.proxy_b)) continue;
    setPairing(s, p, seq);
  }
}

// Append one event to the log (deduped by _seq, kept seq-sorted, capped).
// Returns true when the log changed. game_reconciled upgrades a prior row in
// applyReconciled, so it never lands here.
export function addLogEntry(s, evt) {
  if (evt.payload?.kind === KIND.GAME_RECONCILED) return false;
  const seq = evt.payload?._seq;
  if (seq != null && s.seenSeqs.has(seq)) return false;
  // A run start (always the run's first event) obsoletes the previous
  // run's chatter: restart wiped those games, so flush before appending.
  if (evt.kind === EVT.STATUS && evt.payload?.status === STATUS.RUNNING) {
    s.eventLog.length = 0;
    s.seenSeqs.clear();
  }
  if (seq != null) s.seenSeqs.add(seq);
  const tsRaw = evt.payload?._ts;
  const ts = (tsRaw ? new Date(tsRaw) : new Date())
    .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  s.eventLog.push({ ts, kind: evt.kind, payload: evt.payload, _seq: seq });
  s.eventLog.sort((a, b) => (a._seq ?? 0) - (b._seq ?? 0));
  while (s.eventLog.length > EVENT_LOG_LIMIT) {
    const evicted = s.eventLog.shift();
    if (evicted?._seq != null) s.seenSeqs.delete(evicted._seq);
  }
  return true;
}

// Upgrade the prior game_finished row for this pair with the matched
// result/termination/game_n (one game = one row), record the resolution, and
// notify live windows via the global RECONCILED event.
export function applyReconciled(s, evt, tournamentId) {
  const pid = evt.payload?.pair_id;
  if (!pid) return;
  if (evt.payload.game_n != null) {
    s.resolvedGames.set(pid, {
      gameN: evt.payload.game_n,
      result: evt.payload.result,
      termination: evt.payload.termination,
    });
  }
  for (let i = s.eventLog.length - 1; i >= 0; i--) {
    const ent = s.eventLog[i];
    if (ent.payload?.kind === KIND.GAME_FINISHED && ent.payload?.pair_id === pid) {
      ent.payload = {
        ...ent.payload,
        result: evt.payload.result,
        termination: evt.payload.termination,
        game_n: evt.payload.game_n,
        reconciled: true,
      };
      break;
    }
  }
  window.dispatchEvent(new CustomEvent(APP_EVT.RECONCILED, {
    detail: {
      pairId: pid,
      result: evt.payload.result,
      termination: evt.payload.termination,
      gameN: evt.payload.game_n ?? null,
      tournamentId,
    },
  }));
}

// Apply one event's effect to the proxy/pairing/resolved maps.
export function applyEventKind(s, evt, inner, tournamentId) {
  if (inner === KIND.PROXY_STARTED) {
    const pid = evt.payload.proxy_id;
    if (pid) s.activeProxies.set(pid, { engineName: evt.payload.engine_name || null });
  } else if (inner === KIND.PROXY_ENDED) {
    const pid = evt.payload.proxy_id;
    if (pid) s.activeProxies.delete(pid);
  } else if (inner === KIND.PROXY_PAIRED) {
    const p = evt.payload;
    setPairing(s, p, p._seq ?? null);
  } else if (inner === KIND.GAME_FINISHED) {
    s.livePairings.delete(evt.payload.proxy_a);
    s.livePairings.delete(evt.payload.proxy_b);
  } else if (inner === KIND.GAME_RECONCILED) {
    applyReconciled(s, evt, tournamentId);
  } else if (
    inner === KIND.DONE || inner === KIND.STOPPED ||
    (evt.kind === EVT.STATUS &&
     [STATUS.STOPPED, STATUS.DONE, STATUS.FAILED].includes(evt.payload?.status))
  ) {
    s.activeProxies.clear();
  }
}
