# PGN reconciliation -- featurette spec

Reconcile live-observed games (proxy-driven) with fastchess's PGN
output (authoritative for results) by matching on **move list**, not
on `(white_name, black_name) + N`. Fills in the `result` /
`termination` / `game_n` fields on `game_finished` events that today
are always `*` / `unknown` / `null`. As a bonus, the captured move
list enables in-app replay of any watched game.

Status: shipped. This doc describes current behavior.

## Problem

`docs/tournament-spec.md` # "What end-of-game looks like" documents
the current limitation: pair dissolution is the sole game-end signal,
but result/termination/`game_n` are reported `unknown` because
fastchess's `Started/Finished game N` stdout cannot be unambiguously
joined to a `pair_id` under concurrency (same-name engines, no
per-slot identifier).

The spec's own list of rejected alternatives includes "PGN tail
polling for results" -- rejected on the grounds that it doesn't solve
the `pair_id <-> N` mapping. That reasoning assumed matching on
`(white, black, N)`. **This featurette uses a different match key:
the full move list of the game**, which is unique enough in practice
to disambiguate even under concurrency with same-name engines.

## Approach

### Move-list capture (proxy-driven, server-side)

Every `position startpos moves ...` line a proxy forwards already
carries the cumulative move list. `uci_parse._parse_position`
already extracts it as `moves: list[str]`. No protocol change.

The orchestrator currently only retains the **latest** `position` as
a snapshot. We add a new keying:

- On pair confirmation (`_recompute_groups`, the new-pair branch),
  initialize `_pair_moves[pair_id] = []`.
- On every `position` line ingested for a proxy belonging to a
  confirmed pair, replace `_pair_moves[pair_id]` with the parsed
  `moves` list (longer wins; both proxies of the pair report the
  same move list one ply apart, and the longer is the
  most-recently-played-out state). Whitelist: only update when
  `moves` is a strict prefix-or-extension of the current list, to
  ignore book-line collisions during the transient phase before
  divergence.

Storage: one `list[str]` per active pair. ~6 bytes/move x ~80 plies
x ~200 parallel games = ~100KB. Cap per-pair length at 600 plies as
a runaway guard.

### PGN delta tailer (one per tournament)

A single async task per running tournament:

- 1 Hz `Path.stat()` on the tournament's `games.pgn`.
- On `(mtime_ns, size)` change, open from last-known offset, read
  delta, parse newly-completed games into a list of
  `(white, black, result, termination, move_list, round_tag, n)`.
- The PGN is append-only (`-pgnout append=true` in
  `fastchess.py:248`), so a sequential offset suffices. Truncation
  detection: if `size < last_offset`, reset to 0 and reparse.

Key cross-platform / perf properties:

- `Path.stat()` is stdlib, identical on Linux + Windows; returns
  `st_mtime_ns` and `st_size`.
- Cost is one syscall per second per running tournament (<=1
  active at any time given the orchestrator's single-active
  invariant). Noise floor.
- No `watchdog` dependency, no per-game pollers, no per-OS
  branching.

Implementation note: parse moves out of the PGN movetext into UCI
to compare against `_pair_moves`. The PGN movetext is SAN; convert
via `python-chess` (already a dep) by replaying SAN onto a
`chess.Board()` and extracting `move.uci()` per move. Parse only
newly-appended games, not the whole file (the byte-offset slice
makes this O(delta), not O(file)).

### Match queue (per tournament)

Two halves drained against each other on every tailer wake:

- **Pending pair-end queue.** Pushed by `_dissolve_pair` for any
  pair whose `_pair_moves` is at least `MIN_PLIES_FOR_MATCH = 12`
  long. Carries `(pair_id, white_pid, black_pid, engine_a,
  engine_b, move_list, dissolved_at)`.

- **Recent PGN games ring buffer** (e.g. last 256). Populated by
  the tailer.

Match algorithm, run after each tailer parse pass and after each
new pair-end push:

```
for pending in pending_queue:
    for pgn_game in recent_pgn_games:
        if pgn_game.matched: continue
        if move_list_equal(pending.move_list, pgn_game.uci_moves):
            mark pgn_game matched
            emit `game_reconciled` event with the resolved
                result/termination/game_n + pair_id
            remove from pending_queue
            break
```

Match key is **UCI move-list equality with end-anchored alignment**
(post the min-plies gate). The captured engine-side list is normally
1-2 plies *shorter* than the PGN: fastchess never sends a follow-up
`position startpos moves <full>` after the engines' final
`bestmove` -- the game ended, the next `position` would only land at
`ucinewgame` for the next game, which is for a different pair under
concurrency. Adjudication can also leave the captured list 1 ply
*longer* than the PGN (engine emitted bestmove, fastchess decided
to adjudicate before applying it).

`_moves_match` walks back from the end:

- accepts `len(captured) - len(pgn)  in  {-N, ..., +1}` (any prefix of
  PGN, plus a single overrun ply);
- compares `captured[i] == pgn[i]` for `i` from the last shared
  index down to 0; first mismatch rejects.

No hashing; both queues are bounded so the linear scan is
microseconds.

### Eviction / abandonment

- **Pending side:** entries older than `RECONCILE_TIMEOUT_S = 60`
  are dropped (PGN never arrived -- likely a fastchess crash). One
  INFO log per drop.
- **PGN side:** never swept by time. End-of-slot pairs (engines
  that completed their last assigned game) stay alive without
  `ucinewgame` until tournament teardown -- the matching PGN flush
  is then arbitrarily older than 60s when the pair finally
  dissolves. The deque cap (`queue_max = 256`) bounds memory.
- `_pair_moves[pair_id]` is dropped in `_dissolve_pair` after the
  list is captured into the pending entry.
- **Terminal teardown.** Order: `_dissolve_all_pairs` (parks any
  surviving pairs via `add_pending`), then `tailer.finalize()`
  drains the PGN to EOF. Finalize runs the existing run loop in
  "exit on EOF" mode; since fastchess has been reaped via
  `proc.wait()`, the file is complete and the loop terminates
  deterministically (no timeout). Records read during the drain
  fire `_on_pgn_record`, which matches against the just-parked
  entries. `_reset_pairing_state` then wipes any remaining state.
- WS close (user closes window): does **not** affect pending
  entries -- tournament-scoped, not viewer-scoped.

### Tailer subscriber gate

The tailer runs only while at least one WS client is subscribed to
a game in the tournament (`_game_subscribers` non-empty). 0->1
transition starts the run loop; 1->0 transition stops it, unless
pending entries are still awaiting a PGN match (in which case the
post-record drain check stops it once the queue empties). When
nobody is watching, no 1Hz stat+parse work runs.

Reconciliation correctness across pause/resume: the tailer's file
offset is instance state and survives stop/start, so a resumed
tailer reads the delta accumulated during the pause. At teardown,
`finalize()` works whether the tailer is currently running or
paused (the paused branch drains via `poll_once` from the
caller's context).

### Min-plies floor

Below `MIN_PLIES_FOR_MATCH = 12` plies, **skip matching entirely**.
The pair dissolution emits the current `result="*"` /
`termination="unknown"` event and we don't attempt enrichment.
This is no worse than today and avoids opening-prefix collisions.

12 plies is a heuristic -- short enough that virtually every real
game qualifies, long enough that two book-following games with the
same opening have already diverged for both engines. Tunable.

## Replay (deferred phase)

Once `_pair_moves` is captured per pair, replay-from-pair-id is
trivial: the move list is server-side memory until dissolution,
and lives in the pending-match queue until reconciled or abandoned.

A "replay this game" UI button on a finished Schedule row would
need:

1. A REST endpoint to fetch the move list for a `pair_id` (live or
   recently dissolved + reconciled).
2. Persistence question: in-memory only (lost on server restart),
   or write to a per-tournament `games-replay.jsonl`? Defer
   decision until the main reconciliation work has shipped and
   we know users want replay.

Replay is **not in this featurette's first slice.** Listed for
context: the data we capture for matching is exactly what replay
needs.

## Event schema additions

`game_reconciled` (new event):

```
{
  "tournament_id": str,
  "pair_id": str,
  "game_n": int | null,        // populated when matched; else null
  "white": str,                // engine name
  "black": str,
  "result": str,               // "1-0" | "0-1" | "1/2-1/2" | "*"
  "termination": str,          // PGN [Termination "..."] header
  "matched": bool,             // true on successful PGN match
  "ply_count": int,
}
```

Emitted **after** `game_finished` (which keeps its current
`unknown` shape -- consumers that don't care about reconciliation
need not change). Subscribers that *do* care upgrade their
display when `game_reconciled` arrives. If reconciliation times
out, no `game_reconciled` is emitted and the `game_finished`
event remains the final word.

Existing `game_finished.game_n` field is **not** populated by
this work -- kept `null` per spec. The reconciled `game_n` lives
on the new event so consumers don't need to reason about
"updated" `game_finished` payloads.

## Pair identity in stored PGN (`pgn_stats`)

Separate from live reconciliation, `pgn_stats` identifies pairs
**structurally** in `games.pgn` for standings / SPRT / orphan-drop.
This avoids treating `[Round]` as a globally-unique pair ID -- which
it isn't: fastchess can reassign `[Round]` values across a
Pause/Resume boundary, and pre-config-patch PGNs may contain
collisions where the same Round value appears on multiple distinct
color-flipped pairs.

The scheme:

1. Group games by `(Round, frozenset({White, Black}))`. Two games
   with the same Round value but different engine sets (gauntlet)
   land in different buckets.
2. Within each bucket, greedily match color-flips:
   `(A-white, B-black)` pairs with one unmatched
   `(B-white, A-black)`.
3. Paired games count in standings and in the SPRT pair-list.
   Unmatched games (**orphans**) still count in standings W/L/D
   but are excluded from SPRT pairs and become drop candidates
   for `rewrite_drop_partial_pairs`.

Single-game tours (`paired=False`) disable pair formation entirely:
every game stands alone, no orphan detection, no rewrites, SPRT
uses the logistic (trinomial) model only.

Gauntlet `[Round]` convention (verified on a 3-engine live run):
fastchess increments `[Round]` per `(opening, challenger)` match-up,
so each Round contains exactly 2 games of one engine set,
color-flipped. The `(round, engine-set)` bucket key handles this
trivially -- the engine-set component is the same for both games in
the bucket.

## Risks and mitigations

- **Move-list ambiguity.** Two distinct games producing identical
  move lists is theoretical only at min-plies = 12 in any realistic
  tournament; ignored.
- **PGN write straddles a poll.** A game that finished mid-write
  shows up on the next poll. Tailer reparses from the last clean
  offset on each wake; in-flight bytes don't break anything because
  we only emit completed games (movetext terminated by Result tag).
- **Server restart between dissolution and PGN flush.** Move list
  was in memory; lost on restart. Pair stays unreconciled. PGN
  remains authoritative for standings -- no regression. Document.
- **Match queue overflow.** Bounded at 256 pending entries; oldest
  evicted with no `game_reconciled` emission. Same effective
  outcome as today.
- **PGN parse cost.** Header-only fast path (already implemented in
  `pgn_stats._iter_games_uncached`) is the model; movetext SAN
  replay only needed on the delta. python-chess parse is the
  bottleneck if a tournament emits hundreds of finishes per second
  -- not realistic at human-watchable timecontrols.

## Same-name self-play

Same-engine vs same-engine matchups are **structurally impossible**
in the current design -- both at the picker (UI dedupe) and at pair
confirmation (orchestrator rejects same-engine-name candidates as
phantoms from book-line collisions). See `docs/tournament-spec.md`
# "Self-play (deferred)" for the full rationale and the registry
workaround (register the binary twice under distinct display names).

Reconciliation matches on **UCI move list**, which is independent of
engine names, so this featurette would work transparently if/when
self-play is unblocked. Nothing here adds a new self-play
constraint, and nothing here needs to change when the existing
constraint is lifted.

## Cost summary

- **CPU**: 1 `stat()` per second per active tournament. Each
  PGN-delta wake parses only newly-appended games. Match algo is
  `O(pending * recent_pgn)` -- bounded sets, microseconds.
- **Memory**: <= ~100KB per tournament (pair-moves + pending queue
  + ring buffer).
- **Disk**: none (read-only tail of an existing file).
- **Network**: none (proxy POST traffic unchanged).

## Replay UX caveats

Replay (`GET /api/tournaments/{id}/games/{game_n}/pgn`) is wired
through the Play perspective's view mode via `POST /game/import`. The
Replay button appears on a Live game window's result banner once
`game_reconciled` lands -- without `game_n` the PGN slice isn't
addressable, so unreconciled games (rare) show no Replay button.

- Re-importing a PGN replaces whatever was loaded in Play. The confirm
  dialog only protects against discarding an in-progress vs-engine
  game; consecutive Replays silently swap.
- `play-from-here` from a tournament position picks up
  engine-vs-human, not engine-vs-engine -- the user gets the analytical
  branch, not a rerun of the original matchup.

## Open questions

1. Should `game_reconciled` carry the full move list, or is `pair_id`
   enough to look up replay-side later? Lean toward "not in the
   event; fetch on demand for replay." Keeps event size bounded.
2. Is `MIN_PLIES_FOR_MATCH = 12` right, or should it scale with the
   opening book ply depth (template's book plies)? Probably yes --
   `max(12, book_plies + 4)` so we always require divergence past
   book.
3. Do we want to backfill `game_finished.game_n` (mutating an event
   already emitted is awkward), emit a separate `game_reconciled`
   (current proposal), or both? Current proposal: only the new
   event. Existing consumers untouched.

## Logging strategy

Server-side logging uses stdlib `logging`. Two CLI flags split level
control: `--debug` raises the `sturddle_view` logger tree to DEBUG;
`--server-debug` does the same for `uvicorn` (independent so app-
debug runs don't drown in per-request server noise). Within an
app-debug run, env flags `SV_DEBUG_RECONCILE=1` and
`SV_DEBUG_PAIRING=1` further gate per-subsystem traces.

**Always-on (INFO).** One line per success, one line per silent
failure. Anything more is noise.

- `reconciled pair=<short_id> game_n=<N> result=<R> termination=<T> plies=<P>`
  -- emitted from the orchestrator when `_emit_reconciled` fires.
- `reconcile timeout pair=<short_id> plies=<P> age=<T>s` -- emitted
  when the timeout sweep drops a pending entry. Real-game signal,
  actionable.
- `reconcile late pair=<short_id> plies=<P> age=<T>s` -- emitted
  when a match completes more than `RECONCILE_LATE_WARNING_S = 5s`
  after the pending entry was enqueued. Early warning that flush
  latency is drifting toward the timeout.

**Always-on (WARNING).** Already implemented in `pgn_tail.py`:
parse crash, illegal move, task hang on stop, queue overflow.
Nothing new at this level for the reconciliation queue itself.

**Debug (visible with `--debug`).** Always emitted at DEBUG level;
visible only when the `sturddle_view` logger is at DEBUG:

- `dissolve pair=<id> plies=<N> terminal=<bool> ...` -- one per
  pair end. Useful for spot-checking match rate (each non-terminal
  dissolve should be followed by a `reconciled`).

**Debug + `SV_DEBUG_RECONCILE=1`.** Per-subsystem traces, only on
when both `--debug` is set *and* the env flag is set:

- per pending enqueue: pair_id, plies, white/black engine names.
- per PGN record arrival: game_n, white/black, plies.
- per match attempt: closest-by-length miss diagnostic.
- per tailer delta: `PgnTailer parsed delta=<B>B games=<N>
  new_offset=<O>` -- one line per parse pass with non-empty delta.
  Useful for sanity-checking what the tailer is doing under load.

**When to enable.** Ask the user explicitly to set
`SV_DEBUG_RECONCILE=1` before reproducing a reconciliation issue.
Don't leave it enabled -- the per-record + per-match traffic at
high concurrency is voluminous.

## Operator knobs

Five constants are env-overridable for production tuning:
`SV_RECONCILE_TIMEOUT_S`, `SV_RECONCILE_LATE_WARNING_S`,
`SV_RECONCILE_QUEUE_MAX`, `SV_EVENT_HISTORY_MAX`, `SV_PGN_TAIL_POLL_S`.
See [env-vars.md](env-vars.md) for defaults and effects.

Algorithm constants (`_MAX_CAPTURED_OVERRUN_PLIES`,
`MIN_PLIES_FOR_MATCH`) are not env-overridable -- they encode
properties of the fastchess + UCI protocol, not operator tunables.

## Future revisit

Items deliberately deferred from the first three slices. None
blocks current behavior; revisit once the featurette has been
exercised in practice.

- **Backfill replay edge case.** A workspace opened *very* late
  in a long-running tournament can find both `game_finished` and
  `game_reconciled` for the same game evicted from the
  200-event ring. The standings table stays correct (PGN-derived);
  only the event-log row is missing. Mitigation if needed: bump
  ring size, or expose a "reconciled summary by pair_id" REST
  endpoint computed from the PGN at request time.
- **Multi-tournament scoping.** All reconciliation state is held
  flat (one queue, one tailer, one `_pair_moves`). If/when the
  single-active invariant is lifted (see `tournament-spec.md`
  # "Why single-active"), each of these would need to be keyed by
  tournament id, and proxies would need to carry a tournament tag
  in their broadcast payload so `ingest_proxy_lines` routes to the
  right instance. Same shape, just keyed.
- **Pending-side timeout latency.** `RECONCILE_TIMEOUT_S = 60s`
  bounds how long we wait for a missing PGN flush before logging
  `reconcile timeout`. If real-game timeouts start appearing under
  load (slow disk, batched flushes), bump the constant. The
  `RECONCILE_LATE_WARNING_S = 5s` log emits before any timeout
  fires, so drift can be spotted early.
