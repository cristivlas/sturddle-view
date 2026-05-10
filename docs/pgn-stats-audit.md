# pgn_stats audit -- findings and recommendations

Status: audit only. No code changes proposed yet; pick items off the
priority list and convert to issues.

## Scope

`server/sturddle_view/tournament/pgn_stats.py` (computation) plus the
SPRT pipeline that feeds it: orchestrator wiring, API surface,
template UI.

## Correction to prior assumption

Orchestrator support for SPRT is **already wired**: `fastchess.py`
lines 221-231 forward `template["sprt"]` to fastchess as `-sprt
elo0=... elo1=... alpha=... beta=... model=...`. The API also already
calls `compute_sprt` on every `GET /api/tournaments/{id}` when
`template.sprt` is present (`api/tournaments.py:136-141`), and the
standings window already renders an `.wb-sprt` line when the response
includes it (`tournament-workspace.js:325-343`).

The single missing link is **UI configuration**: the template form
exposes adjudication and concurrency knobs but no SPRT fields. See
`tournament-template-form.js:9,98` -- explicitly tagged "Phase 2".

So end-to-end SPRT today is reachable only by hand-crafting a template
JSON via API; everything else is in place.

## Gauntlet support

Unlike SPRT, gauntlet runs end-to-end. The gap is in result
presentation, not scheduling.

### What works

- `fastchess.py:187-190` emits `-tournament gauntlet` and `-seeds N`
  correctly. Covered by `test_build_command_gauntlet_with_seeds`.
- Form (`tournament-template-form.js:11-76`) exposes gauntlet as a
  type, conditionally shows the seeds field, validates
  `seeds < num_engines`.
- Engine ordering is preserved through UI -> API -> fastchess. By
  fastchess convention engine[0] is the gauntlet leader.

### Gaps

1. **No per-engine Elo for N >= 3.** `compute_standings`
   (`pgn_stats.py:273-310`) only computes Elo when `len(engines) == 2`.
   For round-robin with N >= 3 this is correct (no per-pair sample,
   would need Bradley-Terry). For gauntlet it leaves real information
   on the floor: leader-vs-field and each-challenger-vs-leader are
   valid head-to-head Elo computations. W/L/D counts are still
   correct; the Elo column is just empty.

2. **No gauntlet-aware presentation.** `tournament-workspace.js`
   renders the same standings table for both modes. There is no
   "leader vs challengers" framing, and the workspace can't tell the
   two modes apart from the API response (the response doesn't
   surface `tournament_type`).

3. **No UX hint that engine order is load-bearing.** The form lets
   you reorder, but nothing tells the user that engine[0] is the
   gauntlet leader. Spec mentions "order matters for gauntlet
   seeding" (`tournament-spec.md:432`) but doesn't define the leader
   role. Easy to misuse silently.

4. **Single test.** Only the CLI flags are tested. No gauntlet
   standings test, no resume test, no leader-designation test.

### Recommendations

- **Stats fix (server-only)**: in `compute_standings`, accept a
  `tournament_type` argument. For `gauntlet`, compute leader Elo from
  combined W/L/D vs field, and per-challenger Elo from head-to-head
  vs leader. API caller already knows the type from
  `template["tournament_type"]`.
- **API**: include `tournament_type` in the standings response so the
  UI can render it appropriately.
- **UI hint**: a one-line note next to the engine list when the
  selected type is gauntlet -- "First engine is the leader; it plays
  all others."
- **Tests**: add a gauntlet fixture to `test_tournament_pgn_stats.py`
  that exercises the leader-vs-field standings.

## Findings in pgn_stats.py

### 1. Dead code: `parse_pgn_string` (lines 488-502)

- Never called from anywhere in the repo (verified by grep).
- Uses `io.StringIO` and `chess.pgn` but neither is imported at module
  top -- first call would raise `NameError`.
- Docstring claims "test helper" but no test imports it.

Recommendation: delete. Tests already use `_write_pgn(tmp_path, body)`.

### 2. Silent data loss in `_iter_pairs` (lines 351-385)

- Trailing odd game is dropped silently (line 369).
- Pair-break on engine mismatch (line 375-376) silently abandons the
  rest of the file. In a 2-engine SPRT this "shouldn't happen", but
  if it does we want to know.
- Games with `Result` other than `1-0`, `0-1`, `1/2-1/2`, `½-½` are
  silently filtered upstream by `_iter_games`.

Recommendation: log at `WARNING` when any of these occur; keep
behavior identical otherwise.

### 3. Zero-variance fallback (lines 449-452)

When all pairs score identically, variance is floored at `1e-12`.
LLR explodes; the resulting status is mathematically meaningless but
returned as if normal. Magic number is uncommented.

Recommendation: when `var <= 0` and `n >= 2`, return `status="continue"`
with `llr=0.0` and a comment that it's degenerate. Do not invent
significance from a zero-variance sample.

### 4. No SPRT parameter validation (lines 406-412)

`elo0 > elo1`, `alpha <= 0`, `beta >= 1`, etc. all pass through. The
API catches `KeyError` and `NotImplementedError` only
(`api/tournaments.py:140`).

Recommendation: validate at the bottom of `compute_sprt` before
running; raise `ValueError`. Catch it in the API and return `None`
with a log line.

### 5. UTF-8 errors silently replaced (line 174)

`errors="replace"` masks PGN corruption. Acceptable default but no
log when replacement actually fires.

Recommendation: low priority. Defer.

### 6. `½-½` (Unicode draw) defined in `_DRAW_VALUES` but untested

Line 19 includes it; no test exercises it. Either drop it or add a
test.

## Other tournament bugs found during audit

### 7. Orchestrator leaks fastchess process if `start()` fails post-spawn

`fastchess.py:385` spawns the subprocess, then lines 396-417 do
post-spawn setup (`assign_to_job`, drain task creation,
`await self._emit("started", ...)`). If anything in that window
raises:

- the subprocess is live
- the supervisor task (line 421) was never created
- the orchestrator's except handler (`orchestrator.py:438-445`) clears
  `_active_id`, `_proxy_secret`, pairing state, and updates store
  status to `STOPPED` -- but never calls `self._runner.stop()`

Result: orchestrator believes the tournament is stopped while the
runner still has a live `_proc`. `is_running()` returns True.
On Windows the Job Object at least contains the children; on POSIX
the process is fully orphaned.

Recommendation: in the except handler at `orchestrator.py:438`, call
`await self._runner.stop()` before resetting state. `stop()` is
idempotent (returns early if `_proc is None` or already exited), so
this is safe even when the failure happened before spawn.

```python
except Exception:
    try:
        await self._runner.stop()
    except Exception:
        log.exception("rollback: runner.stop() failed")
    self._active_id = None
    self._proxy_secret = None
    self._reset_pairing_state()
    self._store.update_status(t.id, STATUS_STOPPED, stopped_at=_now())
    raise
```

Testability: medium. Inject a failure into `_emit("started", ...)`
or monkey-patch `assign_to_job` to raise; assert `is_running()` is
False after the rollback.

### 8. Partial pairs leak into completed tournaments after Stop/Resume

A round in this codebase is a color-flipped pair of games. Pair
completeness is load-bearing for every statistic we compute --
pentanomial SPRT explicitly works on per-pair scores in {0, 0.5, 1,
1.5, 2}, gauntlet Elo assumes balanced color exposure, and even
W/L/D standings are biased if one color of a pair is missing.

Empirical case (tournament `55505ae0d3234e8ba6d3fc11d20dc05e`,
4096 games expected, "done" status, 4077 games shown):

- PGN contains 4193 raw game records.
- 116 of those are `(round, white, black)` duplicates from
  Stop/Resume cycles. Last-wins dedup leaves 4077.
- 19 rounds have only one of the two color-flipped games.
- The 19 affected round numbers correlate with the resume offsets
  observed in fastchess.log (32, 50, 116, 122, 127, 153, 174 ...).

Mechanism: on Windows our Stop closes the Job Object
(`fastchess.py:453`, `KILL_ON_JOB_CLOSE`) with no grace period. Game
1 of the pair is already in the PGN; game 2 is in flight when the
process tree dies. On Resume, fastchess sees the partial round,
treats it as advanced enough, and moves on. POSIX has a 2-second
SIGTERM grace and is less affected, but a crash or power loss would
produce the same outcome on any platform.

19 Stops over the tournament's lifetime = 19 missing games.

#### Verification finding: fastchess `config.json` complicates the rewrite

Inspecting `config.json` from the affected tournament:

- `opening.start` is a single integer ("next opening to use"). Drives
  resume position. Not per-round/per-pair.
- `stats.<pair>` block stores per-pair W/L/D counters AND the
  pentanomial table (penta_WW, WD, WL, DD, LD, LL).
- For tournament `55505ae0...`: `W+L+D = 1197+1131+1768 = 4096`
  (matches the **expected** game count); pentanomial sums to 2048
  pairs. fastchess thinks every pair is complete.

But the PGN only contains 4077 unique games (after dedup). Inference:
fastchess increments its in-memory stats counter when a game
finishes, **before** the PGN write hits disk. A kill mid-write loses
the PGN line but the counter has already advanced. On resume,
fastchess loads `config.json`, sees the pair as complete, and skips
the round -- the PGN content is **not** the source of truth for
resume position.

So the originally-sketched "rewrite PGN, fastchess resume fills the
gaps" approach does **not** work as-is. fastchess's own counters
must also be reconciled, and that reconciliation has problems:

- `opening.start` is a single sequential int. Setting it back to the
  earliest partial round forces re-play of every later round too,
  generating large amounts of duplicate work.
- `stats.<pair>` decrements for removed games are computable, but
  pentanomial counts can't be exactly reconstructed from a partial
  pair (we don't know what color/outcome the missing half would have
  produced). We'd lose information.

#### Proposed fix (Path A, this branch)

Surgical scope: keep stats honest, do not attempt resume completion.

1. On orchestrator startup, before reconciling tournament status,
   walk the PGN and identify partial pairs.
2. If any are found, atomic-rewrite the PGN via `_atomic` to drop
   the games belonging to partial pairs. Also dedup
   `(round, white, black)` duplicates (last-wins) in the same pass.
   Preserve the prior file as `games.pgn.bak` for one cycle.
3. Surface the count of dropped games in the API response and the
   workspace UI. Tournament status reflects "done with N partial
   pairs dropped" rather than silently misleading.
4. Do **not** touch fastchess's `config.json`. If the user clicks
   Resume on a partial-pair tournament, fastchess will (correctly,
   given its own state) advance past the now-missing rounds. That
   is a known limitation, documented to the user.

After Path A, `compute_standings` and `compute_sprt` operate on a
clean PGN: every pair complete, no resume duplicates. SPRT pair
walking and Elo math become correct by construction.

Effort: small. Logic is mostly already in `_iter_games`; the new
piece is the rewrite step. Testability: high (hand-craft a PGN
with partial pairs, run the rewrite, assert partials gone, backup
preserved).

Perf cost on restart: ~100-200ms for a 13MB / 4193-game PGN.

#### Deferred: Path B (resume completion)

Actually replaying the missing halves of partial pairs requires
reconstructing fastchess's `config.json` -- decrementing
per-pair stats, resetting `opening.start` to a round number that
lets fastchess fill the gaps without redoing complete rounds, and
accepting some loss in pentanomial accuracy (or recomputing it
from the cleaned PGN, which means dropping the in-flight pair
data fastchess held but never wrote).

This is more of a feature ("repair and resume a tournament with
partial pairs") than a bug fix. It also depends on fastchess
internal layout we don't control across versions. Defer until
there's a concrete need.

#### Belt-and-suspenders mitigations

These do not replace the rewrite but reduce the likelihood of
hitting it in the first place, or limit the damage if a rewrite
turns out to be incomplete:

- ~~Graceful Windows shutdown. Send `GenerateConsoleCtrlEvent(
  CTRL_BREAK_EVENT, pid)` to fastchess before closing the Job.~~
  **Tried 2026-05-10; fastchess ignores CTRL_BREAK.** A 10s grace
  produced no behavior change; the fallback Job close is what
  actually stops the process. Reverted, since the path adds Pause
  latency without correctness benefit. Reconsider only if upstream
  fastchess gains a CTRL_BREAK handler.
- Surface partial-pair count in the API response and the workspace.
  Even with the rewrite in place, exposing "completed N of M pairs"
  in the UI keeps users from thinking a `done` tournament is
  cleaner than it is.

## Test gaps

- `½-½` draw notation
- All-draws SPRT (zero variance branch)
- All-decisive-same-direction SPRT (zero variance branch)
- `read_game_pgn` with out-of-range `game_n` (negative, zero, > N)
- `_iter_pairs` with engine-name mismatch in middle of file
- SPRT param validation (once added)

## SPRT UI: separate work

Adding the SPRT section to `tournament-template-form.js` is a single
file change (~50 lines) plus matching `setValues`/`getValues`/
`validate` plumbing. No backend or API changes needed. Tracked here
only as a pointer; design lives in `docs/tournament-spec.md` lines
333-336.

## Performance

Today's design holds up under typical workloads. The concerns below
show up at scale (many tournaments / large PGNs / cold cache) or
after server restarts.

### What is already good

- `_iter_games` cache is per-path, single-entry, keyed by
  `(mtime_ns, st_size)`. One entry per tournament's `games.pgn`.
  Bounded by the number of tournaments on disk; ~100KB per
  1000-game tournament. No leak.
- `_serialize(with_stats=True)` triggers three walks in sequence
  (`compute_standings`, `compute_games_list`, `compute_sprt`), but
  they all dispatch to the same `_iter_games` and hit the cache --
  exactly one parse per request when the file hasn't changed.
- `_TAG_RE` is compiled once at module load; header-only scan
  skips move trees. ~100-200ms for a 5000-game PGN on a cold
  cache, near-zero on a hot one.
- PGN tailer reads only the appended bytes per tick (`stat`-then-
  seek-to-offset); no full rescans during steady state.
- Sync FastAPI handlers run on the threadpool, so PGN parsing
  never blocks the event loop.
- Workspace polls at 5s, not 1Hz. Standings are not recomputed on
  every WS event -- the client refreshes wholesale every 5s and
  trusts the server cache between refreshes.

### Real concerns

#### P1. List endpoint is O(N) over PGNs on cold cache

`api/tournaments.py:155` calls `_serialize(t, with_standings=True)`
for every tournament. After a server restart (cache cold), with 50
tournaments at 1000 games each this is ~50 sequential PGN parses,
~5-10 seconds total before the response returns. After the cache
warms, near-zero.

Options:
- (a) Drop `with_standings=True` from the list endpoint; let each
  workspace fetch its own standings via `/api/tournaments/{id}`.
  Requires UI change to handle missing standings on initial render.
- (b) Warm the cache on startup with a background task that walks
  every tournament's PGN once, off the critical path.
- (c) Persist a small "summary cache" (W/L/D per engine + game
  count) to each tournament's state.json on game finish; serve from
  it without parsing.

Recommendation: (a) first. It's the smallest change and the data
is rarely needed in the list view (the list mostly shows status
and name).

#### P2. Tailer re-scans full PGN on orchestrator restart

`pgn_tail.py` starts at `_offset=0` after a restart and walks the
whole file to re-emit games to the reconciliation queue. Uses
`chess.pgn.read_game` (full move tree), not the header-only path,
because reconciliation needs `uci_moves`. For a 10k-game tournament
this is 1-2 seconds at startup.

The tailer's `_offset` and `_game_n` are pure in-memory state. No
checkpoint to disk.

Option: persist `(offset, game_n)` to a sidecar file (e.g.
`<tournament>/tailer.checkpoint`) on each successful poll, atomically
via the existing `_atomic` helper. On restart, seek to the saved
offset. Worst case (checkpoint older than truth) just re-emits a
handful of games to reconciliation -- already idempotent.

Effort: small. Testability: high (write checkpoint, restart,
assert offset honored).

#### P3. compute_standings / compute_sprt do not survive across
fastchess appends

The `_iter_games` cache invalidates the moment fastchess writes one
new game (size changes, mtime changes). So during an active
tournament, every workspace refresh re-parses the entire PGN from
scratch -- even though only a handful of games are new. At 1000
games this is ~50ms per refresh per running tournament, which is
fine for one tournament but climbs O(N) when several are running
concurrently.

Option: change `_iter_games` from a single-snapshot cache to an
append-aware cache that keeps the prior tuple and only parses bytes
past the prior `st_size`. Mtime check is unchanged for invalidation
on rewrites/truncation.

Effort: medium. Touches a hot path; needs careful tests for
edge cases (file shrinks, file replaced wholesale).

#### P4. Three walks per /api/tournaments/{id} request even on hot cache

`compute_standings`, `compute_games_list`, and `compute_sprt` each
iterate the cached games tuple top-to-bottom. The cache prevents
re-parsing, but doesn't prevent three sequential O(games) scans.
For a 5000-game tournament this is ~3x the work it has to be.

Option: introduce a single internal walk that produces a typed
record (white, black, result, round, ply_count, ...) and let
the three functions consume from one pass. `compute_sprt` needs
the round tag that the others don't, so the unified record needs
to carry it.

Effort: medium. Net win is small (~10% of an already-fast call);
defer unless P1+P3 don't move the needle enough.

### Perf priority

| # | Item | Effort | Impact | Notes |
|---|------|--------|--------|-------|
| P1 | Drop standings from list endpoint | small | high (cold cache) | Simplest: just stop computing them in the list path |
| P2 | Tailer offset checkpoint | small | medium (restart) | Atomic sidecar via `_atomic` |
| P3 | Append-aware `_iter_games` cache | medium | high (steady-state running tournament) | Hot-path edit; needs careful tests |
| P4 | Unify the three walks | medium | low | Defer; revisit only if profiling justifies |

## Test strategy

Don't pick one methodology for the whole list. Each item below is
tagged with how to approach it.

### TDD (pure function, cheap fixture, fast feedback)

Items 2, 3, 4, 5, 6, 10, 11, 12. All of these are pgn_stats math,
validation, or API response shape. Pattern: write a small PGN to
`tmp_path` (or build a `TestClient` request), assert the return
value, then implement. Ten-second feedback loop.

### Test-after (TDD has negative ROI)

- **Pure deletions** (#1 `parse_pgn_string`): nothing to test first.
  `git grep` to confirm no callers, then delete.
- **Log-warning items** (#7 `_iter_pairs` warnings): `caplog`
  assertions are weaker than return-value tests; write them, but
  don't spend time on phrasing.
- **UI copy / UX hints** (#9 gauntlet leader label): Playwright e2e
  for `<label>` text is slow and flaky. Make the change, eyeball it,
  add a thin assertion only if the copy is load-bearing.
- **SPRT UI form** (#14): meaningful coverage requires the full form
  lifecycle (open, fill, submit, assert posted body). Write one good
  e2e *after* the form is built.

### Integration test with mocks (regression guard, not driver)

- **Orphan-on-start-failure** (#8): test requires injecting a failure
  between subprocess spawn and supervisor creation -- e.g.,
  monkey-patch `assign_to_job` or the event emit to raise. The test
  is mostly proving "the mock raised, the rollback happened"; it
  won't catch a different post-spawn failure we didn't think to
  inject. Worth doing as a regression guard.

### Defer

- #13 (UTF-8 replace logging): hard to trigger cleanly; low value.

### Oracle: real PGNs + `ordo`

For development-time validation of gauntlet Elo (#11) and any future
multi-engine rating work, `ordo` is the standard reference. Plan:

- Keep small synthetic fixtures in the tree for unit tests
  (deterministic, fast, asserts specific expected values).
- Separately, during development, validate the implementation
  against real PGNs by comparing our output to `ordo`'s on the same
  file. This is exploratory, not part of CI.
- If a real-world PGN exposes a bug, distill it into a minimal
  synthetic fixture and check that into the test suite.

Real PGNs are also useful for exposing parser edge cases the
synthetic fixtures miss: aborted games, unusual headers, mid-game
adjudications, encoding quirks. When one of those bites, again --
distill into a minimal fixture, don't import the whole file.

## Priority list (low-effort, high-testability first)

| # | Item | Effort | Testability | Notes |
|---|------|--------|-------------|-------|
| 1 | Delete `parse_pgn_string` | trivial | n/a | Pure removal; nothing references it |
| 2 | Test `½-½` draw | trivial | high | Add one line to existing test fixture |
| 3 | Test all-draws SPRT (zero var) | trivial | high | Drives finding #3 fix |
| 4 | Fix zero-variance fallback (#3) | small | high | Test from #3 locks behavior |
| 5 | SPRT param validation (#4) | small | high | Pure function, easy to unit test |
| 6 | `read_game_pgn` out-of-range tests | small | high | Add 3 cases |
| 7 | Log warnings in `_iter_pairs` (#2) | small | medium | Assert on caplog |
| 8 | Orphan fastchess on start failure (#7) | small | medium | Real correctness bug; inject failure post-spawn |
| 8a | Partial-pair PGN rewrite (Path A, no resume completion) | small | high | Surgical scope; doesn't touch fastchess config.json |
| 8b | ~~Graceful Windows Stop (CTRL_BREAK + grace)~~ | -- | -- | Tried 2026-05-10; fastchess ignores CTRL_BREAK. Reverted |
| 8c | Surface partial-pair count in API/UI | small | medium | Honest reporting after 8a |
| 8d | Path B: resume completion via config.json reconstruction | medium-large | medium | Deferred -- depends on fastchess internals |
| 9 | Gauntlet UX hint (leader = engine[0]) | trivial | low | One label change in the form |
| 10 | Gauntlet standings test fixture | small | high | Locks current W/L/D behavior before changing math |
| 11 | Gauntlet Elo (leader vs field, challenger vs leader) | medium | high | `compute_standings(tournament_type=...)`; pure server-side |
| 12 | Surface `tournament_type` in API standings response | trivial | high | One line in `api/tournaments.py` |
| 13 | UTF-8 replace logging (#5) | small | low | Hard to trigger cleanly; defer |
| 14 | SPRT UI in template form | medium | medium | Separate PR; spec already exists |
| P1 | Drop standings from list endpoint | small | high | See Performance section |
| P2 | Tailer offset checkpoint | small | medium | See Performance section |
| P3 | Append-aware `_iter_games` cache | medium | high | See Performance section |
| P4 | Unify the three walks | medium | low | See Performance section; defer |

Items 1-6 are all single-file, server-only, fully unit-testable.
Items 7-8 add log lines (caplog assertions). Item 9 is the only one
that touches the web layer.
