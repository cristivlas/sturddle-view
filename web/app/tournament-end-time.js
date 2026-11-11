// End time of a tournament the server died under. The server notices only at
// its next startup and stamps stopped_at then, downtime included; the last
// finished game's GameEndTime (fastchess's PGN tag) is when it last ran.

import { gamePgnPath, STATUS } from "./tournament-events.js";

const END_TIME_FAIL_MSG = "Loading tournament end time failed:";

// [GameEndTime "2026-11-07T09:16:58 -0800"]: local date-time, UTC offset.
const GAME_END_RE = /\[GameEndTime "(\S+) ([+-]\d{2})(\d{2})"\]/;
// Per diedKey: end times (ISO; null: no game finished), failed fetches
// (unknown until retried), and fetches in flight.
const resolved = new Map();
const failed = new Set();
const pending = new Map();

// Failed with no exit code and no resource-check details: the error the
// server's startup pass writes.
const serverDied = (t) => t.status === STATUS.FAILED && !!t.started_at
  && !!t.last_error && t.last_error.rc == null && !t.last_error.rescheck;

// The error's own stamp: a restarted tournament dying again is a new key.
const diedKey = (t) => `${t.id}|${t.last_error.at}`;

async function lastGameEnd(api, t) {
  const n = t.games?.length ?? t.standings?.games ?? 0;
  if (!n) return null;
  const { pgn } = await api("GET", gamePgnPath(t.id, n));
  const m = GAME_END_RE.exec(pgn);
  const ms = m ? new Date(`${m[1]}${m[2]}:${m[3]}`).getTime() : NaN;
  return Number.isFinite(ms) ? new Date(ms).toISOString() : null;
}

// The end time (ISO) as known now: stopped_at, or the last game's end for a
// tournament the server died under (null: unknown). undefined: that one's
// not fetched yet (resolveEndTime).
export function knownEndTime(t) {
  if (!serverDied(t)) return t.stopped_at ?? null;
  const key = diedKey(t);
  return failed.has(key) ? null : resolved.get(key);
}

// knownEndTime, fetching the last game when needed; never rejects. A failed
// fetch is logged once and reads as unknown until a call with retry, so
// list reloads don't refetch it every time.
export function resolveEndTime(api, t, { log, retry = false } = {}) {
  if (retry && serverDied(t)) failed.delete(diedKey(t));
  const known = knownEndTime(t);
  if (known !== undefined) return Promise.resolve(known);
  const key = diedKey(t);
  if (!pending.has(key)) {
    pending.set(key, lastGameEnd(api, t)
      .then((iso) => { resolved.set(key, iso); return iso; },
        (e) => { failed.add(key); log?.(END_TIME_FAIL_MSG, e); return null; })
      .finally(() => pending.delete(key)));
  }
  return pending.get(key);
}
