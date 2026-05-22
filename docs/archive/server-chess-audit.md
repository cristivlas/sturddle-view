# Server-side chess code audit

Scope: `server/sturddle_view/**` -- modules that touch the chess domain
(board state, move legality, FEN/PGN parsing, UCI I/O, evals, openings,
tablebase, results). Focus: factoring, code reuse, testability, DRY.
Excludes: pure transport/wiring (`app.py`, `auth.py`, `events.py`,
`api/ws.py`, `api/fs.py`, `api/settings.py`, etc.).

Date: 2026-05-16. Tree at commit `136cf28` (branch `feat/view-edit`).
Last updated: 2026-05-17 (P0-P3 of test battery done; branch `server/refactor`).

## 0. Goals and non-negotiables

**Goals:**
- Increase **reusability** of chess-domain code so planned features
  (PGN export, future annotations, tournament game export, etc.)
  don't grow `HumanVsEngine` further or duplicate logic.
- Increase **testability** so behavior covered today only through
  Playwright e2e (mode interactions, clock invariants, board-event
  payload assembly) becomes addressable by fast unit tests.
- **Harden** the chess infra by collapsing divergent constants and
  one-off chess.Board/popen_uci snippets into named primitives with
  explicit invariants.

**Non-negotiable: no performance regression.** Every refactor must
keep the hot paths -- PGN parsing, header scans, move replay, engine
spawn -- at or below current latency. Where a refactor touches a
known hot path, a perf regression test is required (see section 8).
"It's idiomatic Python now" is not a defense if a bench gets slower.

**Tournament paths are perf-supercritical.** `pgn_tail` runs at 1 Hz
on growing tournament files; `pgn_stats` is recomputed on every poll.
Neither may regress under any circumstance. Any PR touching these paths
must include a perf bench showing no regression. If a bench cannot be
written first (see P4 "DO NOT TOUCH" locks), the PR is blocked until one
is added. The `_iter_games_uncached`, `_iter_games_keyed`, and
`rewrite_drop_partial_pairs` line-scan paths must never be replaced with
`chess.pgn.read_game` -- that would regress by ~50x.

## 1. Module map

| Module | LoC | Role | Chess-dep surface |
|---|---:|---|---|
| `play/human_vs_engine.py` (HVE) | 1841 | Single-game driver: board, clocks, view/edit/analysis, UCI engine, PGN autosave | `chess.Board`, `chess.engine`, `chess.pgn` -- 57 hits |
| `play/import_position.py` | 438 | FEN/PGN parse -> `ImportedPosition` (moves, clocks, evals, comments) | `chess.Board`, `chess.pgn` -- 14 hits |
| `play/game_store.py` | 108 | JSON snapshot of in-progress play game | none (string moves) |
| `play/tablebase.py` | 25 | Syzygy WDL/DTZ probe wrapper | `chess.syzygy` |
| `openings.py` | 97 | ECO/name lookup from lichess TSVs | `chess.pgn` (parse the PGN-style book entries) |
| `engines.py` | 476 | Engine registry + UCI probe (`probe_engine`) + `resolve_selected` | `chess.engine.popen_uci`, error classification |
| `api/chess_utils.py` | 42 | `POST /api/chess/apply-move` stateless helper | `chess.Board`, `chess.Move` |
| `api/game.py` | 493 | `/game/*` routes -> HVE | imports `parse_fen`, `parse_pgn` |
| `tournament/uci_parse.py` | 164 | UCI line parser for the proxy stream | `chess.Board`, `chess.Move` |
| `tournament/pgn_tail.py` | 378 | Append-tail of `games.pgn` -> `PgnGameRecord` | `chess.pgn.read_game`, illegal-move guards |
| `tournament/pgn_stats.py` | 1333 | Standings/Elo/SPRT, pair-orphan rewrite | `chess.pgn.read_game/read_headers` |
| `tournament/pgn_reconcile.py` | 242 | Move-list matcher (proxy <-> PGN) | none directly; consumes parsed lists |
| `tournament/orchestrator.py` | 1358 | Tournament lifecycle + pairing FSM | one `chess.Board(fen).push_uci()` site |
| `tournament/fastchess.py` | 604 | Fastchess CLI wrapper | none chess-domain (just subprocess) |
| `tournament/proxy.py` | 330 | Per-engine stdio proxy | none chess-domain |

### Where chess logic lives, summarized

- **Play side** (`play/`): one huge stateful class (HVE) plus a stateless
  parser (`import_position`) plus three small adapters
  (`game_store`, `tablebase`, `openings`). Most "chess primitive" calls
  live in HVE.
- **API side** (`api/`): two endpoint modules contain chess logic --
  `chess_utils.py` (one stateless verb) and `game.py` (a long REST
  facade that re-parses imports during commit).
- **Tournament side** (`tournament/`): chess logic spread thin --
  `pgn_tail` reads complete games, `pgn_stats` reads headers, `uci_parse`
  recognizes lines, `orchestrator` runs a single `Board.push_uci()` for
  the pairing FSM, `pgn_reconcile` matches move lists with no chess
  primitives at all.

## 2. Findings

### 2.1 No shared "chess primitives" module

The pattern `chess.Board(start_fen) if start_fen else chess.Board()`
appears ~10 times in `human_vs_engine.py` alone (415, 849, 925, 1108,
1246, 1585, 1819, plus `_moves_san` at 82) and once in
`import_position.py` (313). Every site re-encodes the same "None means
startpos" convention. A `board_from(start_fen)` one-liner would deduplicate
and document the convention in one place.

Other small repeats:
- `result = "0-1" if X_white else "1-0"` (HVE 669, 1412) and
  `result/termination = "1/2-1/2", ...` (1756, 1758).
- `"white" if board.turn == chess.WHITE else "black"` -- in HVE
  (`_engine_color`, `_clock_event`, `_board_event`),
  `uci_parse._parse_position`, `import_position.parse_fen/parse_pgn`.
- `popen_uci` with Windows `CREATE_NO_WINDOW` flag and an `env`
  overlay -- duplicated between `engines.probe_engine` (lines 98-108)
  and `HumanVsEngine._spawn_engine` (lines 265-274). Same kwargs
  construction; same Windows guard. The only delta is `args` handling
  (probe passes them through `[engine_path, *args]`, HVE checks for
  empty `_engine_args` first).

### 2.2 `_DECISIVE_RESULTS` duplicated three ways

- `tournament/pgn_stats.py:25-27, 221` defines `_WHITE_WIN`,
  `_BLACK_WIN`, `_DRAW_VALUES`, `_DECISIVE_RESULTS` (includes Unicode
  `½-½`).
- `tournament/pgn_tail.py:25` redeclares
  `_DECISIVE_RESULTS = frozenset({"1-0", "0-1", "1/2-1/2"})` -- without
  `½-½`.
- HVE writes `1-0`/`0-1`/`1/2-1/2` as inline string literals (669,
  1412, 1756, 1758).

The slight divergence (Unicode draw) is the bug-prone kind: a PGN with
`½-½` is decisive in `pgn_stats` but skipped by `pgn_tail`.

There is also a memory rule on file: "No string literals -- repeated
strings must be named constants." These violate it.

### 2.3 `HumanVsEngine` is doing five jobs

The 1841-line class composes:

1. **UCI engine lifecycle** (`_spawn_engine`, `_ensure_engine`,
   `_quit_engine`, `_cancel_think`, `_run_analysis`, `_patch_uci_log`,
   `swap_engine`, `apply_engine_settings_live`) -- ~350 LoC.
2. **Chess clock** (`_tc`, `_white_time`, `_black_time`,
   `_clock_history`, `_consume_turn_time`, `_remaining`, `_tick_loop`,
   `_handle_flag_fall`, pause/resume) -- ~250 LoC, with the take-back
   invariant "one snapshot per ply" interleaved through every state
   transition.
3. **Play-mode game state** (board, move stack, engine-to-move,
   submit_move, takeback, switch_sides, resign, end detection,
   PGN autosave, persistence) -- ~400 LoC.
4. **View mode** (`_view_full_moves`, cursor nav, `_view_eval_history`,
   `_view_comments`, `_view_root_comment`, view-payload builder,
   PGN result/termination shim) -- ~350 LoC.
5. **Edit mode** (`_edit_pre_fen`, `_edit_view_snapshot`,
   `_restore_view_snapshot`, commit/cancel) -- ~150 LoC.

Plus the event-fanout helpers (`_board_event`, `_clock_event`,
`_publish_*`, `snapshot_events`, `republish_state`).

Symptoms of the size:
- The constructor declares 30+ instance attributes. View-mode fields are
  prefixed `_view_*`, edit-mode `_edit_*`, analysis `_analysis_*`,
  but the discipline is by convention only -- a typo elsewhere can
  trip mode guards silently.
- Every entry point starts with the same 4-5 lines of guard checks
  (`if self._editing/_viewing/_paused/_analysis_mode: raise`).
  Repeated in `new_game`, `submit_move`, `takeback`, `switch_sides`,
  `resign`, `pause`, `resume`, `start_analysis`, `enter_view_mode`,
  `enter_edit_mode`, `view_goto`, `play_from_here`, etc. The set of
  forbidden modes differs per operation, but the *shape* (read mode,
  raise on conflict) doesn't.
- `_board_event` (lines 1566-1684) is 120 lines mixing four sources:
  opening payload, view payload, tablebase payload, base FEN/ply.
  Hard to test in isolation; every test that exercises view payload
  is an end-to-end test (`test_e2e_view_mode_names`,
  `test_e2e_perspective_sync`, etc.).
- The take-back invariant ("`_clock_history[i]` is the snapshot
  *before* ply i") is enforced by manual `append`/`pop` in five
  places (`submit_move` 495, `takeback` 593-601, `_think_and_play`
  1502, plus the seed in `new_game` 437-449, plus the view-mode reset
  in `_reset_view_state`). Wrapping these in a small `ClockHistory`
  type would make the invariant explicit and testable.

### 2.4 `parse_fen` round-trip in `/game/edit/commit`

`api/game.py:363` calls `parse_fen(fen).summary` to recompute the
summary after `hve.commit_edit(fen)` already validated and pushed the
FEN. That's two full board constructions for one accepted edit. Not a
perf concern -- a sub-millisecond extra parse -- but it does mean the
"FEN is valid here" invariant is checked by two different code paths
with slightly different rejection messages (HVE uses `explain_invalid`,
parse_fen uses its own `"illegal position (e.g. adjacent kings, ...)"`
string). Pick one.

### 2.5 Two UCI line parsers

`tournament/uci_parse.py` and the in-stream parsing inside
`HumanVsEngine._think_and_play` / `_run_analysis` overlap on
`info`/`score`/`pv`/`depth` extraction. The HVE path uses the typed
dict that python-chess hands back from `engine.analysis()`; the
tournament path parses the raw text line from the proxy.

These can't reasonably share code today (different inputs: typed dict
vs raw string), but the *output shape* differs needlessly:
- HVE `_serialize_info` -> `{"depth", "seldepth", "nodes", "nps",
  "tbhits", "hashfull", "time", "score": {"cp"|"mate"}, "pv": [SAN],
  "pv_uci": [UCI]}`.
- `uci_parse._parse_info` -> `{"depth", "seldepth", "time", "nodes",
  "nps", "hashfull", "tbhits", "multipv", "score_cp"|"score_mate",
  "pv": [UCI]}` (flat `score_*`, no SAN, no `pv_uci`).

Two normalizers for the same UI panel.

### 2.6 PGN walking patterns repeated

Three places walk PGN games node-by-node, replaying onto a board:

- `import_position.parse_pgn` (lines 322-330, plus the second pass at
  357-402 for clocks/evals).
- `pgn_tail._parse_delta` (lines 354-365) -- pushes to compute UCI
  list, with the same illegal-move guard pattern.
- `pgn_stats.read_game_record` (lines 339-343) -- pushes to compute
  `final_fen` and `last_move`.
- HVE `_maybe_save_pgn` (lines 1819-1828) -- replays its own move
  stack to attach `[%clk]` annotations.

Each writes its own `for node in game.mainline(): board.push(...);
on illegal -> warn/break`. A shared "walk PGN and yield
`(node, board, move_uci, mover_white)`" iterator would let each caller
take what it needs without re-implementing the walk.

### 2.7 Process spawn / engine launch convention duplicated

Three call sites build "spawn a UCI engine cross-platform":

- `engines.probe_engine` (a one-shot probe with timeout).
- `HumanVsEngine._spawn_engine` (long-lived play engine).
- `HumanVsEngine._run_analysis` (throwaway analysis engine, calls
  `_spawn_engine` with overrides -- so this one is fine).

Both *first* sites assemble `popen_kwargs` the same way:
```
popen_kwargs = {}
if env: popen_kwargs["env"] = {**os.environ, **env}
if sys.platform == "win32":
    popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
```
plus the `command = [path, *args] if args else path` line. There's no
shared helper.

### 2.8 Test coverage is broad but skewed end-to-end

`server/tests/` has 50+ files. Coverage by module:

- **Strong unit coverage**: `openings`, `import_position` (via
  `test_import_position`, `test_pgn_eval_parse`, `test_pgn_comments`),
  `game_store` (`test_game_persistence`), `tablebase`,
  `tournament/uci_parse` (`test_tournament_uci_parse`),
  `tournament/pgn_*` (multiple).
- **Weak unit coverage / mostly e2e**: `HumanVsEngine`. The class is
  large and stateful; most behavior is exercised via
  `test_e2e_*` browser tests (Playwright). Pure-Python unit tests for
  HVE exist for narrow slices (`test_takeback`, `test_pause`,
  `test_edit_mode`, `test_swap_engine`) but mode-interaction
  matrices are mostly covered through the UI.

If HVE were broken into smaller objects (clock, view-cursor,
engine-supervisor) each would become trivially unit-testable without
spinning a real engine binary.

### 2.9 Smaller observations

- `chess_utils.apply_move` is the only thing under `/api/chess/*`. The
  endpoint exists for legacy reasons (the web app's edit-mode flow
  used to round-trip moves through it). With server-authoritative
  HVE today, it has no remaining callers in the repo's web code path
  besides a code-search hit in `web/app/perspectives/play.js`.
  Worth confirming and either deleting or documenting as a public API.
- `openings.OpeningBook._cache` is a class-level dict keyed by `Path`.
  No invalidation, no thread safety. Fine for prod (read-only data ships
  with the app), but a hidden globals-style cache.
- `play/__init__.py`, `api/__init__.py`, `tournament/__init__.py` are
  empty. Sub-package public API is implicit.

## 3. Recommendations

Ordered by ROI; each rec is independently shippable.

### R1 (high ROI, low risk) -- create `play/_chess_helpers.py`

A small module of pure helpers used by HVE, import_position, and
api/chess_utils. Suggested surface:

```
# Convention: None means standard startpos.
def board_from(start_fen: str | None) -> chess.Board: ...
def side_to_move(board: chess.Board) -> str: "white"|"black"
def moves_san(board, start_fen=None) -> list[str]: ...
def replay_uci(start_fen: str | None, moves_uci: list[str]) -> chess.Board: ...
def flip_pov(score: dict) -> dict: ...   # already in import_position
def explain_invalid(board) -> str: ...   # already in import_position
```

Removes ~15 line-for-line duplicates across HVE and
import_position. Pure functions -- unit tests are one-liners.

### R2 (high ROI, low risk) -- consolidate result constants

New `play/_results.py` (or extend `_chess_helpers`):

```
WHITE_WIN = "1-0"
BLACK_WIN = "0-1"
DRAW = "1/2-1/2"
DRAW_VARIANTS = frozenset({DRAW, "½-½"})  # accept both on read
DECISIVE_RESULTS = frozenset({WHITE_WIN, BLACK_WIN, *DRAW_VARIANTS})

def loser_result(loser_white: bool) -> str: ...
def winner_result(winner_white: bool) -> str: ...
```

Replace the three `_DECISIVE_RESULTS` declarations and the inline
literals in HVE. Closes the `pgn_tail` vs `pgn_stats` divergence on
`½-½`. Satisfies the "no string literals" project rule.

### R3 (high ROI, medium risk) -- extract `EngineSupervisor` from HVE

Move UCI lifecycle (~350 LoC) out of `HumanVsEngine` into a
collaborator: `play/engine_supervisor.py`.

Responsibilities:
- `spawn(path, args, env, options, overrides) -> UciProtocol` (shared
  with `engines.probe_engine` via a common `_popen_kwargs(env)` helper).
- `cancel_search()`, `quit()`, `swap(path)`, `apply_settings_live()`.
- `analysis_search(board, limit, on_info)`, `play_search(board, limit,
  on_info) -> Move`.
- Owns `_uci_log_tasks`, `_engine_options/_args/_env`,
  `_engine_name`.

HVE shrinks from 1841 -> ~1200 LoC, and the new module is unit-testable
with a stub `UciProtocol`. Co-locates the duplicated process-spawn
logic with `engines.probe_engine` via the shared `_popen_kwargs`.

**P6 scope (2026-05-17):** `play_search` / `analysis_search` dropped from
the surface. Search loops stay in HVE; supervisor owns process lifecycle
only. Rationale: `on_info` callbacks would leak event-shape coupling
(eval_pov, bus, game_id, event kinds) into a UCI-only module. Audit
section 7 R3 tests and section 8.2 perf gates already target only the
narrower surface, so the deviation is self-consistent with the rest of
the audit. Real duplication in the two search loops is fixed by an
HVE-private `_pump_engine_info` helper. HVE lands at ~1500 LoC after P6;
the original 1200 target requires P7+P8+P11 to land as well.

### R4 (medium ROI, medium risk) -- extract `ChessClock` from HVE

A `play/chess_clock.py` (~150 LoC) holding `tc`, white/black times,
`_clock_history`, `_turn_started_at`, `pause`, `resume`,
`consume_turn`, `remaining`, `tick`, `flag_check`,
`append_snapshot`, `pop_snapshot`.

The take-back invariant ("one snapshot per ply") becomes the clock's
internal invariant, asserted in one place rather than five. HVE asks
the clock to snapshot before pushing a move; clock returns
`(white, black)` on pop.

Unblocks unit tests for take-back at clock granularity (today they
need a full HVE with a real engine).

### R5 (medium ROI, low risk) -- introduce a `Mode` enum

Replace `_viewing: bool`, `_editing: bool`, `_paused: bool`,
`_analysis_mode: bool` with a single `mode: Mode` field
(`PLAY`/`PAUSED`/`VIEWING`/`EDITING`/`ANALYZING`) and per-operation
allowed-modes sets.

Today's 4-bool design encodes invalid states (`_viewing=True` +
`_editing=True`); the guard checks at the top of every method are
defending against them. A Mode FSM would push the check to one
decorator/helper and eliminate the 4-5-line preamble in 15 methods.

### R6 (medium ROI, low risk) -- shared PGN walk iterator + builder

Two related primitives, both in the new `chess/` package
(see section 4):

**`chess/pgn_walk.py` -- one iterator, covers read AND write callers:**

```
def walk_mainline(game, start_board=None) -> Iterator[
    tuple[chess.pgn.ChildNode, chess.Board, bool]
]:
    """Yield (node, board_BEFORE_move, mover_white) for each mainline
    node, advancing the board as it goes. Read callers consume
    node.comment / node.clock(); write callers call node.set_clock(...)
    on the yielded node. Raises on illegal moves."""
```

Read consumers: `import_position.parse_pgn`, `pgn_tail._parse_delta`,
`pgn_stats.read_game_record`. Write consumer: the PGN builder below
(annotating per-ply `[%clk]`). Removes 4 hand-rolled walks with their
own illegal-move guards.

**`chess/pgn_build.py` -- producer primitive, NOT just an iterator:**

```
def build_pgn(
    *,
    start_fen: str | None,           # None = standard startpos
    moves_uci: list[str],
    clock_history: list[tuple[float, float]] | None = None,
    final_clocks: tuple[float, float] | None = None,
    headers: dict[str, str],         # Event/Site/Date/White/Black/...
    opening: tuple[str, str] | None = None,  # (ECO, name)
    result: str = "*",
    termination: str = "unterminated",
) -> str:
    """Build a complete PGN text. Pure function; no I/O."""
```

This was missing from the original audit (R6 covered consumers only).
Three current/planned producers all need it:

- `HVE._maybe_save_pgn` (today): autosave to `pgn_dir`. Becomes a
  ~15-line wrapper around `build_pgn` + filename derivation +
  `atomic_write_text`.
- `GET /game/pgn` (planned, see [pgn-export.md](pgn-export.md) Option A,
  the recommended option): play-mode export endpoint. Calls
  `build_pgn` with the live HVE state.
- View-mode-FEN export (planned, same proposal): builds a PGN from
  `_view_full_moves` + headers + optional `_view_clock_history`. Same
  builder, different inputs.

Without `build_pgn` as a primitive, the export feature would either
duplicate the autosave's PGN assembly (~70 LoC) or layer a sixth job
onto `HumanVsEngine`. With it, the export endpoint is ~15 lines and
HVE shrinks slightly instead of growing.

The pgn-export proposal calls this `_build_pgn` and scopes it to HVE.
Putting it in `chess/` instead makes it reusable by the view-mode-FEN
export path (which is not on the HVE class today) and by future
non-HVE callers (e.g. tournament game export from
`pgn_stats.read_game_record`'s data).

### R7 (low ROI, low risk) -- unify UCI info shape

Define a single `EngineInfo` schema:

```
{depth, seldepth, time, nodes, nps, hashfull, tbhits,
 score: {cp|mate}, pv: [SAN], pv_uci: [UCI]}
```

Have `tournament/uci_parse._parse_info` build the same shape (with
`score_cp`/`score_mate` rolled into a `score` sub-object). The web
client gets one renderer instead of two.

### R8 (low ROI, low risk) -- audit `/api/chess/apply-move`

Confirm no live caller. If unused, delete. If used (legacy mobile
client?), document and add a regression test. Eliminates an
otherwise-orphan chess module from `api/`.

### R9 (defer) -- split `_board_event` builder

The view-payload assembly in `_board_event` (lines 1592-1660) is a
~70-line conditional that builds a different dict per mode. Extract
`_view_payload()` / `_play_payload()` methods. Easy after R5 lands
(Mode enum dispatches naturally).

### R10 (defer) -- split `pgn_stats.py` (1333 LoC)

Out of scope for this audit (the file is mostly Elo/SPRT math, not
chess primitives), but worth flagging: standings, Ordo-fit, SPRT,
and the rewrite-orphans logic could split cleanly into three files
of ~400 LoC each.

## 4. Suggested target layout

```
server/sturddle_view/
  chess/                      # new package, no app/HTTP imports
    __init__.py
    board.py                  # board_from, side_to_move, replay_uci, moves_san
    results.py                # WHITE_WIN/BLACK_WIN/DRAW/DECISIVE_RESULTS
    pgn_walk.py               # walk_mainline iterator (read + annotate)
    pgn_build.py              # build_pgn(...) producer primitive
    invalid.py                # explain_invalid (moved from import_position)
  play/
    human_vs_engine.py        # 1841 -> ~1100 after R3/R4 extraction
    engine_supervisor.py      # NEW (R3)
    chess_clock.py            # NEW (R4)
    mode.py                   # NEW (R5) Mode enum + allowed-transitions
    import_position.py        # consumes chess.pgn_walk
    game_store.py             # unchanged
    tablebase.py              # unchanged
  api/
    chess_utils.py            # delete if R8 confirms no callers
    game.py                   # consumes hve directly; same shape
  tournament/
    uci_parse.py              # output unified per R7
    pgn_tail.py               # consumes chess.pgn_walk
    pgn_stats.py              # consumes chess.results
    ...
  engines.py                  # share _popen_kwargs with EngineSupervisor
  openings.py                 # unchanged
```

No `chess/` -> `play/` or `chess/` -> `tournament/` imports either way
beyond the new helpers; `chess/` is the leaf.

## 5. What is already good

- `import_position.py` is a clean, pure module. The dataclass-based
  output, the PGN-format documentation in docstrings, and the
  format-detection logic in `api/game._parse_import_payload` are
  well-factored.
- `openings.OpeningBook` is small, dependency-light, and easy to swap
  out. The process-wide cache is documented as a trade-off rather
  than hidden.
- `tournament/uci_parse.py` is a tight ~165-line module with clear
  per-verb parsing. No state, easy to test.
- `tournament/pgn_tail.py`'s tail/parse split, the per-poll byte cap,
  and the boundary-snapping logic are good defensive code with
  matching docstring context.
- `engines.probe_engine` classifies exceptions by *type*, not by
  parsing platform-specific text -- exactly right.
- The test suite's separation of e2e (Playwright) from pure unit
  tests is clear from filenames.

## 6. Validating the primitives against planned features

The `chess/` package is only worth introducing if its primitives are
reusable enough that *planned* features (not just today's duplicates)
can build on them without growing HVE further. Cross-check below.

### [pgn-export.md](pgn-export.md) -- PGN export endpoint (proposed)

Needs:
- Play-mode export: `GET /game/pgn` builds a PGN from the live HVE
  game. Same assembly as autosave.
- View-mode-after-FEN-import export: builds a PGN from
  `_view_full_moves` + `_view_clock_history` + minimal headers (no
  HVE-live state involved).
- View-mode-after-PGN-import export: served by existing
  `recent_imports` -- no chess code needed.

Covered by R6's `chess/pgn_build.py` for both build paths. The
autosave's `_maybe_save_pgn` and the new endpoint share the builder;
the view-mode-FEN path uses the same builder with different inputs.
The proposal's `_build_pgn` (scoped to HVE) becomes `build_pgn`
(in `chess/`), reusable by callers that don't have an HVE instance.

### Client-side annotations / commentary (proposed)

Mentioned in pgn-export.md as a future direction. A future endpoint
that lets the user attach a comment to a ply would need to:
- Round-trip the comment through the PGN (export and re-import).
- Live in the same `[%clk]`-style annotation slot on `chess.pgn.ChildNode`.

Covered by R6 if `walk_mainline` yields the node (callers set
`node.comment` directly, the builder already serializes whatever is
on the node). No new primitive needed.

### Tournament game export (hypothetical)

`pgn_stats.read_game_record` already returns a PGN string for one
historical tournament game (read from disk). If a future feature
wanted to re-build a tournament game PGN from parsed records (rather
than disk-slurp), `chess/pgn_build.py` covers it.

### What this exercise caught

The original R6 ("shared PGN walk iterator") was scoped to *consumers*
only -- `parse_pgn`, `pgn_tail`, `pgn_stats.read_game_record`. The
pgn-export proposal needs a *producer* primitive too, and the
view-mode-FEN export path lives outside HVE entirely. Without
adding `pgn_build.py` to the package surface, the export feature
would either duplicate ~70 LoC of HVE's autosave logic or layer a
sixth job onto `HumanVsEngine`. The package was almost-reusable but
not enough -- worth flagging that "audit recommendations should be
validated against planned features, not just today's duplicates."

## 7. Test-driven refactoring plan

Each recommendation lists the **new tests to write first** (red),
then the refactor (green), then existing tests as the regression
backstop. Existing coverage is named so the reader can see what's
already protecting the path.

The order matches the recommendation order -- R1 first because
everything else imports from `chess/`.

### R1 -- `chess/board.py` helpers

**New tests** -- `tests/test_chess_board.py` (all pure, fast):

- `test_board_from_none_is_startpos` -- `board_from(None).fen() == STARTING_FEN`.
- `test_board_from_fen_round_trips` -- arbitrary FEN in/out.
- `test_board_from_invalid_fen_raises_valueerror` -- not a custom type;
  preserves caller's existing catch sites.
- `test_replay_uci_from_startpos` -- list of legal moves -> expected FEN.
- `test_replay_uci_from_custom_fen` -- non-startpos start, two plies.
- `test_replay_uci_rejects_illegal_move` -- raises with the offending UCI.
- `test_replay_uci_rejects_malformed_uci` -- e.g. `"e2e9"`.
- `test_side_to_move_white_black` -- startpos = "white", post-1.e4 = "black".
- `test_moves_san_from_startpos` -- matches existing HVE output for a known game.
- `test_moves_san_from_custom_fen` -- non-startpos replay produces correct SAN.

**Existing regression backstop**: `test_import_position.py`,
`test_view_mode.py`, `test_edit_mode.py`, every `test_e2e_*` that
renders moves.

### R2 -- `chess/results.py` constants

**New tests** -- `tests/test_chess_results.py`:

- `test_decisive_results_contains_all_three_decisives`.
- `test_decisive_results_accepts_unicode_draw` -- `"½-½" in DECISIVE_RESULTS`.
  (This is the bug-prone divergence between `pgn_stats` and `pgn_tail`
  today; locks it down.)
- `test_winner_result_white` -- `winner_result(True) == "1-0"`.
- `test_winner_result_black` -- `winner_result(False) == "0-1"`.
- `test_loser_result_white` -- `loser_result(True) == "0-1"`.

**Existing backstop**: `test_pgn_reconciliation.py`,
`test_pgn_tail.py`, `test_tournament_pgn_stats.py` -- already
exercise the literals via fixtures.

**Migration regression**: a one-liner test confirming `pgn_tail` and
`pgn_stats` import from the same constant (no second definition).

### R3 -- `EngineSupervisor` extraction

**New tests** -- `tests/test_engine_supervisor.py` (uses a fake UCI
protocol stub, no real engine binary):

- `test_spawn_applies_options_filters_managed` -- supervisor passes
  per-engine + global options, drops managed/unknown ones.
- `test_spawn_includes_overrides_for_analysis` -- analysis-mode
  Threads override wins.
- `test_spawn_uses_windows_creation_flag` -- mock `sys.platform`,
  assert `creationflags` set. Mirror for non-Windows = absent.
- `test_spawn_env_overlays_parent` -- per-engine env is layered on
  `os.environ`, doesn't replace it.
- `test_spawn_args_list_appended_to_command` -- empty list -> bare
  path; non-empty -> `[path, *args]`.
- `test_cancel_search_sends_stop` -- supervisor calls `engine.send_line("stop")`
  on cancel.
- `test_cancel_search_falls_back_to_transport_close_on_timeout`.
- `test_quit_engine_idempotent` -- second quit is a no-op.
- `test_swap_clears_options_and_args` -- after swap, prior per-engine
  state doesn't leak to next launch.
- `test_apply_settings_live_kills_engine_for_respawn`.
- `test_uci_log_emits_send_and_recv` -- with a stub bus.

**Shared spawn-kwargs helper** (`_popen_kwargs(env)`):

- `test_popen_kwargs_no_env_no_flags_on_posix`.
- `test_popen_kwargs_includes_creationflag_on_win32`.
- `test_popen_kwargs_overlays_env_on_parent`.

This helper is also imported by `engines.probe_engine`, so add:
- `test_probe_engine_and_supervisor_use_same_popen_kwargs` -- import
  the helper from both call sites and assert identity. Cheap test
  that catches a divergence going forward.

**Existing backstop**: `test_engines.py`, `test_engines_api.py`,
`test_apply_engine_settings_live.py`, `test_swap_engine.py`,
`test_hve_engine_defaults.py`, `test_e2e_engine_options_broken.py`.

### R4 -- `ChessClock` extraction

**New tests** -- `tests/test_chess_clock.py` (pure, fast, no asyncio
beyond a fake monotonic):

- `test_initial_state_both_sides_at_initial_seconds`.
- `test_consume_turn_subtracts_elapsed_and_adds_increment` -- mock
  `time.monotonic`, advance 1.5s, verify the side-just-moved lost 1.5s
  + gained inc.
- `test_remaining_for_thinking_side_ticks_down`.
- `test_remaining_for_idle_side_constant`.
- `test_pause_freezes_elapsed_no_increment` -- elapsed baked in,
  increment NOT credited (no move).
- `test_resume_resets_turn_start`.
- `test_snapshot_invariant_one_per_ply` -- the take-back invariant
  asserted directly: after N moves, `len(history) == N`.
- `test_pop_snapshot_restores_prior_clocks` -- the take-back behavior
  in pure form.
- `test_switch_sides_snaps_elapsed_no_increment` -- mirrors HVE's
  current switch logic.
- `test_flag_check_returns_loser_when_remaining_zero`.
- `test_flag_check_no_loser_while_paused`.
- `test_seed_clock_history_pads_missing_entries_with_initial` --
  current HVE behavior when PGN clock entries are None.

**Existing backstop**: `test_takeback.py`, `test_pause.py`,
`test_pause_api.py`, `test_e2e_takeback_paused.py`.

### R5 -- `Mode` FSM

**New tests** -- `tests/test_play_mode_fsm.py`:

- `test_initial_mode_is_play`.
- For each (mode, operation) pair in the audit's allowed/forbidden
  matrix, assert allow vs raise. Parametrize over (PLAY, PAUSED,
  VIEWING, EDITING, ANALYZING) x (submit_move, takeback,
  switch_sides, resign, pause, resume, start_analysis,
  enter_view_mode, enter_edit_mode, view_goto, play_from_here,
  commit_edit, cancel_edit). The table is the spec; the test is
  one parametrize.
- `test_mode_transition_play_to_paused_and_back`.
- `test_mode_transition_view_to_edit_requires_view_mode_first`.
- `test_mode_transition_analysis_requires_paused_or_view`.
- `test_invalid_transitions_raise_typed_error` -- not just
  `RuntimeError("edit mode is on")` -- a `ModeConflictError`
  carrying `current` and `attempted`. Eliminates the string-matching
  in callers.

**Existing backstop**: every HVE mode-conflict test currently
asserts `RuntimeError` with a substring -- update those to match
the new typed exception in the same PR.

### R6 -- `chess/pgn_walk.py` + `chess/pgn_build.py`

**New tests for `pgn_walk`** -- `tests/test_chess_pgn_walk.py`:

- `test_walk_yields_one_tuple_per_mainline_node`.
- `test_walk_from_custom_start_board` -- non-startpos PGN.
- `test_walk_mover_white_alternates_from_startpos`.
- `test_walk_mover_white_respects_custom_start_with_black_to_move`.
- `test_walk_advances_board_in_place` -- consumers see
  `board_BEFORE_move`, the board is post-move on next iteration.
- `test_walk_raises_on_illegal_move`.
- `test_walk_empty_game_yields_nothing`.
- `test_walk_supports_node_annotation_writes` -- demonstrate the
  write use case: walk, call `node.set_clock(...)`, serialize,
  re-parse, observe annotations preserved.

**New tests for `pgn_build`** -- `tests/test_chess_pgn_build.py`:

- `test_build_minimal_pgn_from_startpos_no_moves` -- just headers,
  no `[Result "*"]`-only edge case.
- `test_build_with_moves_includes_san_movetext`.
- `test_build_round_trips_through_chess_pgn_parser` -- write,
  re-parse, assert headers + moves match input.
- `test_build_from_custom_fen_emits_fen_and_setup_headers`.
- `test_build_attaches_clk_per_ply` -- `clock_history` -> `[%clk]`
  on every node, verifiable via `node.clock()`.
- `test_build_attaches_clk_for_final_clocks_when_history_short` --
  mirror current HVE behavior (live clock for last ply).
- `test_build_includes_eco_and_opening_headers_when_provided`.
- `test_build_omits_eco_when_opening_is_none`.
- `test_build_result_and_termination_round_trip`.
- `test_build_timecontrol_header_formats_int_seconds`.

**Equivalence test** -- pin the autosave refactor:
- `test_build_pgn_matches_current_autosave_output` -- snapshot a
  known game's PGN output before R6 (capture from
  `_maybe_save_pgn`), assert the new `build_pgn` produces
  byte-identical text for the same inputs. Lets you swap the
  implementations without regressing on PGN-diff-sensitive tests.

**Consumer migrations** -- after `walk_mainline` is in:
- Confirm `import_position.parse_pgn`, `pgn_tail._parse_delta`,
  `pgn_stats.read_game_record` produce identical output before/after
  via existing test suites (`test_import_position.py`,
  `test_pgn_eval_parse.py`, `test_pgn_comments.py`,
  `test_pgn_tail.py`, `test_pgn_reconciliation.py`,
  `test_tournament_pgn_stats.py`). No new tests required if
  outputs are unchanged; if any output differs, the suite will
  catch it.

**New feature tests** -- driven by [pgn-export.md](pgn-export.md):
- `test_export_play_mode_returns_pgn_for_in_progress_game`.
- `test_export_play_mode_returns_pgn_for_finished_game`.
- `test_export_view_mode_fen_builds_minimal_pgn_from_view_moves`.
- `test_export_view_mode_pgn_redirects_to_recent_imports_hash`.
- `test_export_filename_in_content_disposition`.

### R7 -- unified UCI info schema

**New tests** -- `tests/test_uci_info_schema.py`:

- `test_serialize_info_from_typed_dict_matches_schema` -- the HVE
  path output structure (post-refactor).
- `test_parse_info_from_raw_line_matches_schema` -- the
  `uci_parse` path output structure.
- `test_both_paths_produce_same_keys_for_same_data` -- feed the
  same depth/score/pv via both routes; assert dicts compare equal
  (after SAN-vs-UCI normalization).

**Existing backstop**: `test_tournament_uci_parse.py` covers
`uci_parse._parse_info` today; no HVE-side `_serialize_info`
unit tests exist (covered only by e2e).

**New gap-filler**:
- `test_serialize_info_score_pov` -- white POV, black POV, STM POV;
  one parametrize. Easy now, hard to write before R7 because the
  current function is a closure over `eval_pov`.

### R8 -- `/api/chess/apply-move` audit

**New tests** -- only if kept:
- `test_apply_move_accepts_legal_uci`.
- `test_apply_move_returns_204_on_late_bestmove`.
- `test_apply_move_rejects_invalid_fen`.
- `test_apply_move_rejects_invalid_uci`.

If deleted: no new tests; remove route from any e2e that touches it
(grep `web/app/` for `/api/chess/apply-move`).

### R9 -- split `_board_event` into per-mode payload builders

**New tests** -- `tests/test_board_event_payload.py`:

- `test_play_payload_omits_view_field`.
- `test_view_payload_includes_cursor_and_total_plies`.
- `test_view_payload_eval_at_cursor` -- with synthesized eval history.
- `test_view_payload_comment_at_cursor` -- root comment at cursor 0;
  per-ply at cursor > 0.
- `test_view_payload_game_over_on_pgn_result`.
- `test_view_payload_threefold_termination_from_can_claim`.
- `test_tablebase_field_only_present_when_prober_set`.
- `test_opening_field_null_for_imported_games` -- start_fen set ->
  no opening lookup.

These exist *post* the extraction; today this code path is only
testable via `test_e2e_view_mode_names`, `test_e2e_perspective_sync`,
etc.

### Cross-cutting: pre-refactor regression freeze

Before any R3-R6 patch lands, capture the current outputs as snapshots
to detect unintended drift:

- `tests/fixtures/pgn_autosave_snapshots/` -- run a deterministic
  series of moves through HVE today, write the resulting autosave
  PGN to a fixture. After R6, the same series must produce the
  same PGN. (Already partially covered by `test_pgn_save.py` --
  extend to cover non-startpos games and games with seeded clocks.)
- `tests/fixtures/board_event_snapshots/` -- one snapshot per mode
  (play, paused, viewing-at-cursor-N, editing, analyzing); same
  before/after R9.

These belong in the suite for the long term, not just during the
refactor.

## 8. Performance regression tests

Hot paths in the chess code today, with current characteristics worth
preserving. Each refactor item identifies which (if any) of these it
touches; perf tests gate those PRs.

### 8.1 Inventory of hot paths

| Hot path | Current technique | Why it matters |
|---|---|---|
| `pgn_stats._iter_games_uncached` | Header-only line scan with regex (not `chess.pgn.read_game`). Comment says "~50x faster" on multi-MB PGNs. | Standings/SPRT recomputed on every poll of a long tournament's PGN. |
| `pgn_stats._iter_games_keyed` | `(mtime_ns, size)` cache on top of the above. | Eliminates repeat parses across pollers. |
| `pgn_stats.rewrite_drop_partial_pairs` | Line-scan block splitter, not `chess.pgn`. Comment: "avoids ... full move-tree parse which dominates rewrite time on multi-MB files." | Runs on resume after a kill. |
| `pgn_tail._parse_delta` | Byte-bounded delta (256KB cap), `asyncio.to_thread`, running char-pos counter ("was O(N^2) on a multi-MB delta"). | Runs at 1Hz on growing tournament PGNs. |
| `pgn_tail._snap_to_boundary` | `rfind(b"\n\n[")` on the delta window. | Keeps the parser off mid-game-truncated buffers. |
| `engines.probe_engine` | Bounded UCI handshake (default 500ms). | UI calls this on every `GET /engines`. |
| `HVE._spawn_engine` + `_ensure_engine` | Spawn on first need, reuse across moves. | Re-spawn per move would add seconds of latency on heavy engines. |
| `openings.OpeningBook.load` | Process-wide cache keyed by dir path; parse takes ~3s. | Startup-only without the cache. |
| `openings.OpeningBook.lookup` | Tuple-prefix walk down to length 30. | Called on every play-mode `board_update`. |
| `HVE._board_event` | Synchronous build inside the asyncio publish path. | Fires on every move and every view nav. |
| Per-poll PGN tail (gated on subscribers) | Skips parsing entirely when no WS subscribers. | Avoids paying for an unwatched tournament. |

### 8.2 Per-recommendation perf gates

| Rec | Touches a hot path? | Required perf test |
|---|---|---|
| R1 board helpers | Yes -- `board_from(start_fen)` in HVE's `_board_event` and per-move publish path | `bench_board_from_vs_inline` -- assert no slower than the inline ternary (within 5%) over 100k iterations. Trivial -- catches a future "validate the FEN inside the helper" temptation. |
| R2 result constants | No (constant lookup) | None. |
| R3 EngineSupervisor | Yes -- `_spawn_engine`, `_cancel_think` | `bench_spawn_supervisor_vs_inline` -- spawn a dummy UCI binary 50x; assert median spawn latency within 10% of pre-refactor. `bench_cancel_think_grace_period` -- assert the 500ms timeout is preserved (no accidental bump to 2s, etc.). |
| R4 ChessClock | Yes -- `_remaining` called per tick (4Hz) and per publish; `_consume_turn_time` per move | `bench_clock_remaining` -- 1M calls under fixed `time.monotonic` mock; assert < N ns/call (capture pre-refactor baseline). The current inline version is ~5 lines of arithmetic; the extracted version must not introduce indirection that shows up at tick rate. |
| R5 Mode FSM | Yes -- guard check at top of every HVE entry point | `bench_mode_guard` -- 1M `assert_allowed(mode, op)` calls; must be ≤ current 4-bool check time (within 10%). A dict lookup is faster than 4 branches; harder to regress, but worth pinning. |
| R6 pgn_walk | Yes -- `pgn_tail._parse_delta`, `pgn_stats.read_game_record`, `import_position.parse_pgn` | `bench_walk_mainline_vs_inline` -- parse a 1000-game fixture PGN both ways; assert within 5% wall time. Critical: the existing `pgn_tail` walk is in the hot async path and must not get slower. |
| R6 pgn_build | Mixed -- autosave fires on every move (in-loop), export endpoint is on-demand | `bench_build_pgn_100_ply_game` -- assert ≤ current `_maybe_save_pgn` build time (excluding file write). One-shot baseline, regression gate. |
| R6 (DO NOT TOUCH) | `pgn_stats._iter_games_uncached`, `_iter_games_keyed`, `rewrite_drop_partial_pairs` | **Stay regex/line-scan.** Switching these to `chess.pgn.read_game` or to a shared walker would regress by ~50x per the existing module's comments. `bench_iter_games_keyed_1k_games` and `bench_rewrite_partial_pairs_1k_games` pin current performance; any PR that tries to "consolidate" these into the walker fails the bench. |
| R7 unified info schema | Yes -- `_serialize_info` runs per engine info event (many per second on fast TCs) | `bench_serialize_info` -- 100k calls; assert within 10% of pre-refactor. |
| R8 apply-move audit | No | None. |
| R9 board_event split | Yes -- per move + per view nav | `bench_board_event_play_payload` and `bench_board_event_view_payload` -- assert within 5% of pre-refactor. Extracting a function shouldn't matter, but with attribute lookups and per-call dict construction it's worth measuring. |

### 8.3 Benchmark suite mechanics

Where to put them: `server/tests/perf/` (new dir; pytest-collected but
skipped by default).

Activation: `pytest -m perf` or env var `SV_RUN_PERF_BENCHES=1`. CI
runs them on a labeled "perf" job, not on every PR -- benches are
flaky under shared-runner load, so the gate is "perf job green" not
"every PR green."

Format: each bench is a pytest function timing a tight loop with
`time.perf_counter_ns`, compared against a baseline JSON checked in at
`server/tests/perf/baselines.json`. Tolerance: 10% by default; tighter
where called out above. Baseline regenerated by `pytest --update-perf-baselines`
under controlled conditions (idle laptop, fixed CPU governor); the
update is its own reviewed PR.

Fixtures:
- `tests/fixtures/perf_1k_games.pgn` -- 1000 short games for tailer/
  stats benches. Generate once, check in (~few MB).
- `tests/fixtures/perf_100_ply_game.pgn` -- one long game for build/walk.
- `tests/fixtures/fake_uci_engine.py` -- a minimal stdin/stdout UCI
  script for spawn benches (no real engine required).

What perf tests do NOT cover:
- Engine search latency (depends on the engine binary; not our code).
- Network/WS throughput (covered by e2e workspace tests, indirectly).
- Disk write speed (`atomic_write_text` -- OS-bound).

### 8.4 What to do when a bench fails

Treat a perf test failure the same as a correctness test failure: the
PR doesn't land. Options:

1. Find and fix the regression (usually possible -- an accidental
   per-call object allocation, an extra attribute lookup, a logging
   call in a hot loop).
2. If the new behavior is genuinely worth the cost (rare for these
   refactors), update the baseline in a separate, explicitly-reviewed
   PR with a written justification in the commit body. Don't bundle a
   baseline bump with the refactor PR -- the cost should be obvious in
   review.

The "5% / 10%" tolerances are noise floors, not budgets. If a benchmark
goes from 100ns to 108ns "but it's still under tolerance," that's
still a regression in aggregate over millions of calls; investigate
before merging.

## 9. Non-recommendations

- **Don't** wrap `python-chess` behind a project facade. It is
  already the right level of abstraction. R1 helpers above are
  thin convenience wrappers, not an abstraction layer.
- **Don't** split `tournament/orchestrator.py` (1358 LoC) on chess
  grounds. Its size is from the pairing FSM + reconciliation wiring,
  not chess logic -- only one line uses `chess.Board` directly.
- **Don't** factor `_serialize_info` and `uci_parse._parse_info`
  into one function. The inputs differ (typed dict vs raw line);
  what should be shared is the output schema, not the parser (R7).

## 10. Reviewer notes

Verified against the working tree (branch `server/refactor`, post-`136cf28`).

**Battery status (2026-05-17):** P0-P6 complete.

- P0: perf infrastructure (`tests/perf/`, fixtures, fake UCI engine) -- done.
- P1: pre-extraction characterization snapshots (clock, autosave, board_event) -- done.
- P2: R1 `chess/board.py` (`board_from`, `side_to_move`, `replay_uci`, `moves_san`) -- done.
- P3: R2 `chess/results.py` (result constants + `loser_result`/`winner_result`) -- done;
  `½-½` divergence between `pgn_tail` and `pgn_stats` closed; inline result literals
  removed from `HVE`, `pgn_tail`, `pgn_stats`. `ViewModeParams` dataclass also added to
  HVE as part of P2 cleanup (11-arg `enter_view_mode` sprawl).
- P5: R6b `chess/pgn_build.py` -- done; `build_pgn` pure function added;
  `HVE._maybe_save_pgn` reduced to a ~15-line wrapper (was 74 lines).
- P6: R3 `play/engine_supervisor.py` -- done; UCI lifecycle extracted;
  shared `_popen_kwargs` in `engines.py`; HVE delegates spawn/ensure/
  cancel/quit/swap/apply_settings_live. Search loops stay in HVE behind
  a new `_pump_engine_info` helper. HVE: 1841 -> 1768 LoC (delta tempered
  by passthrough properties preserving the existing test surface).
  `play_search` / `analysis_search` not extracted (see section 3 R3 notes).

**Structural findings are correct.** All identified bugs, patterns, and
recommendations are confirmed:

- The `1/2-1/2` vs `½-½` divergence between `pgn_tail` and `pgn_stats`
  (section 2.2) was a real bug -- fixed by P3.
- Duplicated `popen_uci`/`CREATE_NO_WINDOW` construction confirmed in
  `engines.probe_engine` and `HVE._spawn_engine` -- fixed by P6.
- Both UCI info schema shapes (section 2.5) confirmed as described -- server-side
  unification landed in P9 (`chess/engine_info.py`); web tournament view still
  reads legacy keys via a `tournament/uci_parse.py` compat shim. Web migration
  is a follow-up PR.
- HVE constructor: 47 instance attributes (well above the stated "30+") -- reduced
  by P6 (EngineSupervisor) and P7 (ChessClock).
- `_board_event` mixes four payload sources; confirmed ~119 lines -- still pending (R9/P11).
- PGN walk duplication across four sites confirmed -- still pending (R6a/P4).
- Result literals as inline strings -- fixed by P3.

**LoC (branch `server/refactor`, 2026-05-17):** The "Actual" column below reflects the
current working tree. Some modules grew slightly from the P2/P3 migrations.

| Module | At audit | Now |
|---|---:|---:|
| `human_vs_engine.py` | 1744 | 1841 |
| `pgn_stats.py` | 1161 | 1333 |
| `orchestrator.py` | 1234 | 1358 |
| `engines.py` | 419 | 476 |
| `game.py` | 417 | 493 |
| `pgn_tail.py` | 332 | 376 |
| `fastchess.py` | 534 | 604 |
| `import_position.py` | 400 | 438 |
| `pgn_reconcile.py` | 207 | 242 |
| `proxy.py` | 288 | 330 |
| `uci_parse.py` | 144 | 166 |
| `openings.py` | 79 | 97 |
| `game_store.py` | 90 | 108 |
| `chess_utils.py` | 32 | 43 |
| `tablebase.py` | 20 | 25 |

Line citations are navigation hints only -- use grep, not line numbers.

### TDD battery gaps

All 20 claimed backstop test files exist. The battery is otherwise
sound but had four gaps; two are now closed by P0/P1.

**[CLOSED by P1] R4 -- no characterization pass before extraction.**
Clock characterization tests (`test_chess_clock_characterization.py`,
`test_pgn_autosave_characterization.py`) and `board_event` snapshots are
now committed under `tests/fixtures/`.

**R3 -- stub interface unspecified.**
`test_cancel_search_sends_stop` asserts `engine.send_line("stop")` but
`python-chess` `UciProtocol` does not expose `send_line` directly --
the call goes through `engine.protocol.send_line`. The stub must match
the actual protocol interface or the tests will fail to compile. The
battery plan (P6) adds a pinning note at the top of
`test_engine_supervisor.py` before any R3 test is written.

**R5 -- allowed/forbidden transition matrix missing.**
The audit says "the table is the spec" for the (mode x operation)
parametrize, but the table itself is not in the document. The battery
plan (P8) requires the matrix be committed as `ALLOWED_MODES_BY_OP` in
`play/mode.py` before the parametrized test is written.

**[CLOSED by P0] Section 8 perf infrastructure is unbuilt.**
`tests/perf/` now exists with `conftest.py`, `baselines.json`, `_bench.py`,
and `test_smoke.py`. Fixtures `perf_1k_games.pgn`, `perf_100_ply_game.pgn`,
and `fake_uci_engine.py` are committed under `tests/fixtures/`.
