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
| 9 | Gauntlet UX hint (leader = engine[0]) | trivial | low | One label change in the form |
| 10 | Gauntlet standings test fixture | small | high | Locks current W/L/D behavior before changing math |
| 11 | Gauntlet Elo (leader vs field, challenger vs leader) | medium | high | `compute_standings(tournament_type=...)`; pure server-side |
| 12 | Surface `tournament_type` in API standings response | trivial | high | One line in `api/tournaments.py` |
| 13 | UTF-8 replace logging (#5) | small | low | Hard to trigger cleanly; defer |
| 14 | SPRT UI in template form | medium | medium | Separate PR; spec already exists |

Items 1-6 are all single-file, server-only, fully unit-testable.
Items 7-8 add log lines (caplog assertions). Item 9 is the only one
that touches the web layer.
