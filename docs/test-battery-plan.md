# Test battery execution plan

Durable, multi-iteration plan for building the test battery defined in
[server-chess-audit.md](server-chess-audit.md) sections 7 and 8. Each
phase is sized to fit one PR / one Claude Code session. Future
sessions pick up from the status header and execute the next pending
phase.

Last updated: 2026-05-17 (P0, P1, P2, P3, P4, P5, P6 done).

Related docs:
- [server-chess-audit.md](server-chess-audit.md) -- the spec.
- [pgn-export.md](pgn-export.md) -- consumes R6 primitives.

## Status

Mark a phase done by checking the box, flipping its **State** line to
`done`, and appending the merging commit SHA. See "Conventions" at the
bottom for the exact ritual.

- [x] P0  Perf infrastructure and fixtures
- [x] P1  Pre-extraction characterization snapshots
- [x] P2  R1 -- chess/board.py helpers
- [x] P3  R2 -- chess/results.py constants
- [x] P4  R6a -- chess/pgn_walk.py iterator and consumer migration
- [x] P5  R6b -- chess/pgn_build.py producer and autosave swap
- [x] P6  R3 -- EngineSupervisor extraction
- [ ] P7  R4 -- ChessClock extraction
- [ ] P8  R5 -- Mode FSM and typed conflict error
- [ ] P9  R7 -- unified UCI info schema
- [ ] P10 R8 -- /api/chess/apply-move audit
- [ ] P11 R9 -- _board_event payload split

## Cross-phase invariants

These hold for every phase. Violations are blockers, not nits.

- ASCII only in source and docs added by this battery.
- No new e2e tests. The existing Playwright suite is the regression
  backstop. This battery adds unit and perf tests only.
- Every new module gets a one-line module docstring stating its
  single responsibility.
- Once P3 (R2) lands, no inline result literals (`"1-0"`, `"0-1"`,
  `"1/2-1/2"`) remain anywhere under `server/sturddle_view/`. The R2
  PR migrates every existing site.
- Every new test must fail in isolation before the corresponding
  refactor lands. Commit the red, then commit the green. Two commits
  minimum per refactor phase.
- Perf benches that compare against pre-refactor behavior MUST be
  added and baselined against the OLD implementation FIRST, in a
  separate commit, before any refactor code lands. See "Perf
  baseline capture protocol" under Conventions for the exact ritual.
  Affected phases: P4, P5, P6, P7, P9, P11.
- Perf benches are gated by env var `SV_RUN_PERF_BENCHES=1` (project
  rule: env var prefix is `SV_`). Default `pytest` run skips them.
- Do not bundle a refactor PR with a perf baseline update. Baseline
  bumps ship in their own reviewed PR with a written justification.
- No string literals for any value that already has a named constant.
- No inline helpers. Helpers go in the module they belong to and are
  imported.
- Propose before coding non-trivial changes; ask before committing.
- Do not introduce `chess/` -> `play/` or `chess/` -> `tournament/`
  imports. `chess/` is a leaf package.
- Tournament paths (`pgn_tail`, `pgn_stats`) are perf-supercritical.
  Neither may regress under any circumstance. `_iter_games_uncached`,
  `_iter_games_keyed`, and `rewrite_drop_partial_pairs` must stay as
  regex/line-scan -- never replace with `chess.pgn.read_game`.

## P0 -- Perf infrastructure and fixtures

- **State**: done (pending merge SHA)
- **Depends on**: none
- **Goal**: Stand up `tests/perf/` plus the fixtures and fake UCI
  engine that every later perf gate needs. Closes audit section 10
  gap #4.
- **Files to create / modify**:
  - `server/tests/perf/__init__.py`
  - `server/tests/perf/conftest.py` (perf marker registration,
    `SV_RUN_PERF_BENCHES` skip, `--update-perf-baselines` option,
    baseline load/compare helper)
  - `server/tests/perf/baselines.json` (empty object skeleton)
  - `server/tests/perf/_bench.py` (shared timer + compare helper; no
    inline helpers in individual bench files)
  - `server/tests/perf/test_smoke.py` (one trivial bench proving the
    harness records and compares)
  - `server/tests/fixtures/perf_1k_games.pgn` (1000 short games,
    generated deterministically; check in)
  - `server/tests/fixtures/perf_100_ply_game.pgn` (one long game)
  - `server/tests/fixtures/fake_uci_engine.py` (minimal stdin/stdout
    UCI loop: id, uciok, isready/readyok, position/go/bestmove)
  - `server/tests/fixtures/generate_perf_fixtures.py` (deterministic
    generator; committed so fixtures are reproducible; not invoked at
    test time)
- **Test list**:
  - `test_perf_harness_skipped_without_env_var`
  - `test_perf_harness_runs_when_env_var_set`
  - `test_perf_harness_compares_against_baseline_within_tolerance`
  - `test_perf_harness_update_flag_rewrites_baseline`
- **Definition of done**:
  - `pytest server/tests/perf/` collects but skips (default).
  - `SV_RUN_PERF_BENCHES=1 pytest server/tests/perf/test_smoke.py`
    runs the smoke bench and passes.
  - `pytest --update-perf-baselines server/tests/perf/test_smoke.py`
    writes baselines.json.
  - Fake UCI engine survives a `python -m chess.engine` handshake in
    a one-off smoke (manual verification noted in commit body).
  - Fixture files under 5 MB each.
- **Out of scope**: any chess/play/tournament module changes; any
  refactor; any per-recommendation bench (those land with their
  refactor PR).

## P1 -- Pre-extraction characterization snapshots

- **State**: done (pending merge SHA)
- **Depends on**: P0
- **Goal**: Pin current behavior of HVE clock and PGN autosave so
  later extractions are byte-comparable. Closes audit section 10
  gap #1.
- **Files to create / modify**:
  - `server/tests/test_chess_clock_characterization.py` (new; tests
    current HVE clock methods through HVE, no extraction yet)
  - `server/tests/test_pgn_autosave_characterization.py` (new;
    deterministic move series, capture autosave output)
  - `server/tests/fixtures/pgn_autosave_snapshots/` (new dir;
    expected PGN text files)
  - `server/tests/fixtures/board_event_snapshots/` (new dir;
    one JSON snapshot per mode for use in P11)
- **Test list**:
  - `test_hve_clock_remaining_white_idle_constant`
  - `test_hve_clock_remaining_black_thinking_decrements`
  - `test_hve_consume_turn_subtracts_elapsed_and_adds_increment`
  - `test_hve_clock_history_one_entry_per_ply`
  - `test_hve_clock_pop_on_takeback_restores_prior_clocks`
  - `test_hve_clock_pause_freezes_no_increment`
  - `test_hve_clock_seed_from_pgn_pads_missing_with_initial`
  - `test_autosave_startpos_short_game_matches_snapshot`
  - `test_autosave_from_custom_fen_matches_snapshot`
  - `test_autosave_with_seeded_clock_history_matches_snapshot`
  - `test_board_event_snapshot_play_mode_recorded`
  - `test_board_event_snapshot_paused_recorded`
  - `test_board_event_snapshot_viewing_at_cursor_recorded`
  - `test_board_event_snapshot_editing_recorded`
  - `test_board_event_snapshot_analyzing_recorded`
- **Definition of done**:
  - All listed tests green against current HVE (no refactor).
  - Snapshot fixtures committed and reproducible by re-running the
    test with `--snapshot-update` (or equivalent regenerate flag --
    pick one mechanism in the PR and document it).
  - No production code touched in this phase.
- **Out of scope**: extracting `ChessClock`; extracting payload
  builders; any change under `server/sturddle_view/`.

## P2 -- R1 chess/board.py helpers

- **State**: done (pending merge SHA)
- **Depends on**: P0
- **Goal**: Land `chess/board.py` with the helpers everything else
  imports, plus the per-recommendation perf gate.
- **Files to create / modify**:
  - `server/sturddle_view/chess/__init__.py` (new)
  - `server/sturddle_view/chess/board.py` (new)
  - `server/sturddle_view/play/human_vs_engine.py` (replace inline
    `chess.Board(start_fen) if start_fen else chess.Board()` and
    `"white"/"black"` ternaries at every site)
  - `server/sturddle_view/play/import_position.py` (replace one
    inline site at line 313 area)
  - `server/sturddle_view/api/chess_utils.py` (use `board_from`)
  - `server/sturddle_view/tournament/uci_parse.py` (use
    `side_to_move` for the `_parse_position` ternary)
  - `server/tests/test_chess_board.py` (new)
  - `server/tests/perf/test_bench_board_from.py` (new)
- **Test list** (from audit section 7, R1):
  - `test_board_from_none_is_startpos`
  - `test_board_from_fen_round_trips`
  - `test_board_from_invalid_fen_raises_valueerror`
  - `test_replay_uci_from_startpos`
  - `test_replay_uci_from_custom_fen`
  - `test_replay_uci_rejects_illegal_move`
  - `test_replay_uci_rejects_malformed_uci`
  - `test_side_to_move_white_black`
  - `test_moves_san_from_startpos`
  - `test_moves_san_from_custom_fen`
  - Perf: `bench_board_from_vs_inline` (within 5% over 100k iters)
- **Definition of done**:
  - All R1 tests green.
  - Perf bench green under `SV_RUN_PERF_BENCHES=1`; baseline written.
  - `grep` confirms no remaining inline `chess.Board(... if ... else ...)`
    or `"white" if board.turn == chess.WHITE else "black"` in the
    listed modules.
  - All existing tests still green.
- **Out of scope**: results constants (P3); pgn walk (P4); any HVE
  decomposition.
- **Notes**:
  - Approved scope addition: `enter_view_mode` signature collapsed
    into a `ViewModeParams` dataclass; defensive `RuntimeError` for a
    missing edit snapshot in `cancel_edit` replaced with an `assert`.
    Both landed in eaa7112 with reviewer sign-off; not a deviation.

## P3 -- R2 chess/results.py constants

- **State**: done (pending merge SHA)
- **Depends on**: P2
- **Goal**: Single source of truth for result strings; close the
  `pgn_tail` vs `pgn_stats` unicode-draw divergence; satisfy the
  "no string literals" rule.
- **Files to create / modify**:
  - `server/sturddle_view/chess/results.py` (new)
  - `server/sturddle_view/tournament/pgn_stats.py` (drop
    `_WHITE_WIN`, `_BLACK_WIN`, `_DRAW_VALUES`, `_DECISIVE_RESULTS`;
    import from chess.results)
  - `server/sturddle_view/tournament/pgn_tail.py` (drop local
    `_DECISIVE_RESULTS`; import from chess.results; pick up unicode
    draw)
  - `server/sturddle_view/play/human_vs_engine.py` (replace inline
    result literals with named constants and `winner_result` /
    `loser_result`)
  - `server/tests/test_chess_results.py` (new)
- **Test list** (from audit section 7, R2):
  - `test_decisive_results_contains_all_three_decisives`
  - `test_decisive_results_accepts_unicode_draw`
  - `test_winner_result_white`
  - `test_winner_result_black`
  - `test_loser_result_white`
  - `test_loser_result_black`
  - `test_pgn_tail_and_pgn_stats_share_constant_identity` (import
    both module attributes and assert `is`)
- **Definition of done**:
  - All R2 tests green.
  - Existing `test_pgn_reconciliation.py`, `test_pgn_tail.py`,
    `test_tournament_pgn_stats.py` still green.
  - `grep -n '"1-0"\|"0-1"\|"1/2-1/2"' server/sturddle_view/` returns
    only the definitions in `chess/results.py`.
  - No perf bench required (constant lookup).
- **Out of scope**: anything beyond constant migration. No HVE
  restructure; no walk changes.

## P4 -- R6a chess/pgn_walk.py iterator and consumer migration

- **State**: done (df87f0a)
- **Depends on**: P2 (for board helpers used by walk)
- **Goal**: One iterator replaces four hand-rolled PGN walks; pin the
  read-side performance.
- **Files to create / modify**:
  - `server/sturddle_view/chess/pgn_walk.py` (new)
  - `server/sturddle_view/play/import_position.py` (consume
    `walk_mainline` in both passes -- main parse and the
    clock/eval/comment second pass)
  - `server/sturddle_view/tournament/pgn_tail.py` (`_parse_delta`
    consumes `walk_mainline`)
  - `server/sturddle_view/tournament/pgn_stats.py`
    (`read_game_record` consumes `walk_mainline`); do NOT touch
    `_iter_games_uncached`, `_iter_games_keyed`,
    `rewrite_drop_partial_pairs` -- they stay regex/line-scan per
    audit section 8.2
  - `server/tests/test_chess_pgn_walk.py` (new)
  - `server/tests/perf/test_bench_walk_mainline.py` (new)
  - `server/tests/perf/test_bench_iter_games_keyed.py` (new --
    locks current 50x line-scan behavior against future "consolidate
    into walker" temptations)
  - `server/tests/perf/test_bench_iter_games_uncached.py` (new --
    same lock for the uncached path; audit section 8.2 lists both)
  - `server/tests/perf/test_bench_rewrite_partial_pairs.py` (new --
    same lock)
- **Test list** (from audit section 7, R6 walk portion):
  - `test_walk_yields_one_tuple_per_mainline_node`
  - `test_walk_from_custom_start_board`
  - `test_walk_mover_white_alternates_from_startpos`
  - `test_walk_mover_white_respects_custom_start_with_black_to_move`
  - `test_walk_advances_board_in_place`
  - `test_walk_raises_on_illegal_move`
  - `test_walk_empty_game_yields_nothing`
  - `test_walk_supports_node_annotation_writes`
  - Perf: `bench_walk_mainline_vs_inline` (within 5% on 1000-game
    fixture)
  - Perf: `bench_iter_games_keyed_1k_games` (lock current speed)
  - Perf: `bench_iter_games_uncached_1k_games` (lock current speed)
  - Perf: `bench_rewrite_partial_pairs_1k_games` (lock current speed)
- **Definition of done**:
  - All R6-walk tests green.
  - All three perf benches green; baselines committed.
  - Existing `test_import_position.py`, `test_pgn_eval_parse.py`,
    `test_pgn_comments.py`, `test_pgn_tail.py`,
    `test_pgn_reconciliation.py`, `test_tournament_pgn_stats.py`
    still green (no output diffs).
- **Out of scope**: pgn_build (P5); HVE autosave changes (P5).

## P5 -- R6b chess/pgn_build.py producer and autosave swap

- **State**: done (fbb9c94 red; green pending commit SHA)
- **Depends on**: P1 (autosave characterization snapshots), P2, P3,
  P4
- **Goal**: Producer primitive landed; HVE autosave reduced to a
  thin wrapper; export endpoint groundwork in place (the endpoint
  itself ships separately per pgn-export.md, but the builder is
  ready).
- **Files to create / modify**:
  - `server/sturddle_view/chess/pgn_build.py` (new)
  - `server/sturddle_view/play/human_vs_engine.py`
    (`_maybe_save_pgn` becomes a ~15-line wrapper around
    `build_pgn` + filename derivation + `atomic_write_text`)
  - `server/tests/test_chess_pgn_build.py` (new)
  - `server/tests/perf/test_bench_build_pgn.py` (new)
- **Test list** (from audit section 7, R6 build portion):
  - `test_build_minimal_pgn_from_startpos_no_moves`
  - `test_build_with_moves_includes_san_movetext`
  - `test_build_round_trips_through_chess_pgn_parser`
  - `test_build_from_custom_fen_emits_fen_and_setup_headers`
  - `test_build_attaches_clk_per_ply`
  - `test_build_attaches_clk_for_final_clocks_when_history_short`
  - `test_build_includes_eco_and_opening_headers_when_provided`
  - `test_build_omits_eco_when_opening_is_none`
  - `test_build_result_and_termination_round_trip`
  - `test_build_timecontrol_header_formats_int_seconds`
  - `test_build_pgn_matches_current_autosave_output` (equivalence
    against P1 snapshots; byte-identical)
  - Perf: `bench_build_pgn_100_ply_game` (at or below current
    `_maybe_save_pgn` build time, file write excluded)
- **Definition of done**:
  - All R6-build tests green.
  - Autosave characterization snapshots from P1 still byte-identical.
  - Perf bench green; baseline committed.
  - `_maybe_save_pgn` body shrunk; assertion in PR description with
    before/after LoC.
- **Out of scope**: new export endpoints (separate PR per
  pgn-export.md); view-mode-FEN export path (separate PR).

## P6 -- R3 EngineSupervisor extraction

- **State**: done (pending merge SHA)
- **Depends on**: P0 (fake UCI engine for spawn bench), P2
- **Goal**: UCI lifecycle out of HVE; shared `_popen_kwargs` with
  `engines.probe_engine`. Closes audit section 10 gap #2 (stub
  interface pinned to `python-chess` `UciProtocol`).
- **Sub-task before any supervisor test is written**: pin the stub
  interface. Document in a top-of-file docstring in
  `tests/test_engine_supervisor.py` which methods of
  `chess.engine.UciProtocol` the stub implements
  (`send_line` lives on `protocol`, not the wrapper; `play`,
  `analysis`, `quit`, `configure`, `transport`). The stub must
  match this surface exactly. Reference: audit section 10, R3 gap.
- **Files to create / modify**:
  - `server/sturddle_view/play/engine_supervisor.py` (new)
  - `server/sturddle_view/engines.py` (factor `_popen_kwargs(env)`;
    have `probe_engine` and `EngineSupervisor.spawn` both call it)
  - `server/sturddle_view/play/human_vs_engine.py` (delegate
    `_spawn_engine`, `_ensure_engine`, `_quit_engine`,
    `_cancel_think`, `_run_analysis`, `_patch_uci_log`,
    `swap_engine`, `apply_engine_settings_live` to supervisor)
  - `server/tests/test_engine_supervisor.py` (new; uses the stub
    described above)
  - `server/tests/test_popen_kwargs.py` (new; shared-helper tests)
  - `server/tests/perf/test_bench_spawn_supervisor.py` (new; uses
    `fixtures/fake_uci_engine.py` from P0)
  - `server/tests/perf/test_bench_cancel_think.py` (new)
- **Test list** (from audit section 7, R3):
  - `test_spawn_applies_options_filters_managed`
  - `test_spawn_includes_overrides_for_analysis`
  - `test_spawn_uses_windows_creation_flag`
  - `test_spawn_env_overlays_parent`
  - `test_spawn_args_list_appended_to_command`
  - `test_cancel_search_sends_stop`
  - `test_cancel_search_falls_back_to_transport_close_on_timeout`
  - `test_quit_engine_idempotent`
  - `test_swap_clears_options_and_args`
  - `test_apply_settings_live_kills_engine_for_respawn`
  - `test_uci_log_emits_send_and_recv`
  - `test_popen_kwargs_no_env_no_flags_on_posix`
  - `test_popen_kwargs_includes_creationflag_on_win32`
  - `test_popen_kwargs_overlays_env_on_parent`
  - `test_probe_engine_and_supervisor_use_same_popen_kwargs`
  - Perf: `bench_spawn_supervisor_vs_inline` (median within 10% over
    50 spawns of fake engine)
  - Perf: `bench_cancel_think_grace_period` (500ms preserved)
- **Definition of done**:
  - All R3 tests green; stub surface documented in test file
    docstring.
  - All listed existing backstop tests green:
    `test_engines.py`, `test_engines_api.py`,
    `test_apply_engine_settings_live.py`, `test_swap_engine.py`,
    `test_hve_engine_defaults.py`.
  - Perf benches green; baselines committed.
  - HVE LoC dropped (note before/after in PR body).
- **Out of scope**: clock extraction (P7); mode FSM (P8); info
  schema (P9).
- **Notes**:
  - Supervisor surface narrowed from the audit's R3 (no `play_search` /
    `analysis_search`). Search loops stay in HVE; supervisor owns
    process lifecycle (spawn/configure/cancel/quit/swap/log) only.
    `_think_and_play` and `_run_analysis` share a new HVE-private
    `_pump_engine_info(analysis, game_id, board)` helper for their
    common filter+serialize+publish loop. Full rationale in
    server-chess-audit.md section 3 R3.

## P7 -- R4 ChessClock extraction

- **State**: pending
- **Depends on**: P1 (clock characterization snapshots), P2
- **Goal**: Clock + take-back invariant in one class with one
  internal assertion site.
- **Files to create / modify**:
  - `server/sturddle_view/play/chess_clock.py` (new)
  - `server/sturddle_view/play/human_vs_engine.py` (delegate `_tc`,
    `_white_time`, `_black_time`, `_clock_history`,
    `_turn_started_at`, `pause`, `resume`, `_consume_turn_time`,
    `_remaining`, `_tick_loop`, `_handle_flag_fall`, snapshot
    push/pop in `submit_move` / `takeback` / `_think_and_play`)
  - `server/tests/test_chess_clock.py` (new; pure, fast, fake
    monotonic)
  - `server/tests/perf/test_bench_clock_remaining.py` (new)
- **Test list** (from audit section 7, R4):
  - `test_initial_state_both_sides_at_initial_seconds`
  - `test_consume_turn_subtracts_elapsed_and_adds_increment`
  - `test_remaining_for_thinking_side_ticks_down`
  - `test_remaining_for_idle_side_constant`
  - `test_pause_freezes_elapsed_no_increment`
  - `test_resume_resets_turn_start`
  - `test_snapshot_invariant_one_per_ply`
  - `test_pop_snapshot_restores_prior_clocks`
  - `test_switch_sides_snaps_elapsed_no_increment`
  - `test_flag_check_returns_loser_when_remaining_zero`
  - `test_flag_check_no_loser_while_paused`
  - `test_seed_clock_history_pads_missing_entries_with_initial`
  - Perf: `bench_clock_remaining` (1M calls under mocked monotonic;
    pre-refactor baseline captured before swap)
- **Definition of done**:
  - All R4 tests green.
  - All P1 clock characterization tests still green.
  - Backstop tests green: `test_takeback.py`, `test_pause.py`,
    `test_pause_api.py`, `test_e2e_takeback_paused.py`.
  - Perf bench within 10% of baseline.
- **Out of scope**: mode FSM (P8); engine work (P6); payload split
  (P11).

## P8 -- R5 Mode FSM and typed conflict error

- **State**: pending
- **Depends on**: P2
- **Goal**: Replace 4 booleans (`_viewing`, `_editing`, `_paused`,
  `_analysis_mode`) with one `Mode` enum + per-operation allowed
  sets and a typed `ModeConflictError`. Closes audit section 10
  gap #3.
- **Sub-task before the parametrize test is written**: produce the
  allowed/forbidden (mode x operation) matrix in
  `server/sturddle_view/play/mode.py` as a `dict[Op, frozenset[Mode]]`
  named `ALLOWED_MODES_BY_OP`. Reverse-engineered from current HVE
  guard clauses on:
  `submit_move`, `takeback`, `switch_sides`, `resign`, `pause`,
  `resume`, `start_analysis`, `enter_view_mode`, `enter_edit_mode`,
  `view_goto`, `play_from_here`, `commit_edit`, `cancel_edit`.
  Modes: `PLAY`, `PAUSED`, `VIEWING`, `EDITING`, `ANALYZING`.
  The matrix is the spec; check it in BEFORE the parametrized test.
- **Files to create / modify**:
  - `server/sturddle_view/play/mode.py` (new -- `Mode` enum,
    `Op` enum, `ALLOWED_MODES_BY_OP`, `ModeConflictError`,
    `assert_allowed(mode, op)`)
  - `server/sturddle_view/play/human_vs_engine.py` (drop four
    booleans; install single `self._mode: Mode = Mode.PLAY`;
    replace 15-ish guard preambles with `assert_allowed`)
  - `server/tests/test_play_mode_fsm.py` (new)
  - Update mode-conflict assertions in 6 existing files to expect
    `ModeConflictError` rather than `RuntimeError` substring
    (audited count: ~39 sites across `test_edit_mode.py`,
    `test_view_mode.py`, `test_pause.py`, `test_takeback.py`,
    `test_tournament_fastchess.py`, `test_tournament_orchestrator.py`)
  - `server/tests/perf/test_bench_mode_guard.py` (new)
- **Test list** (from audit section 7, R5):
  - `test_initial_mode_is_play`
  - `test_mode_matrix_parametrized` (one parametrize across
    `(Mode, Op)` Cartesian product; allows or raises per
    `ALLOWED_MODES_BY_OP`)
  - `test_mode_transition_play_to_paused_and_back`
  - `test_mode_transition_view_to_edit_requires_view_mode_first`
  - `test_mode_transition_analysis_requires_paused_or_view`
  - `test_invalid_transitions_raise_typed_error` (asserts
    `ModeConflictError.current` and `attempted` populated)
  - Perf: `bench_mode_guard` (1M calls; within 10% of 4-bool check)
- **Definition of done**:
  - All R5 tests green.
  - All 39 migrated mode-conflict tests green against new exception.
  - Perf bench within 10%.
  - `grep` confirms `_viewing`, `_editing`, `_paused`,
    `_analysis_mode` attributes removed from HVE.
- **Out of scope**: payload split (P11); engine/clock work.

## P9 -- R7 unified UCI info schema

- **State**: pending
- **Depends on**: P6 (`_serialize_info` is easier to test once it
  is reachable through `EngineSupervisor`)
- **Goal**: Single `EngineInfo` schema serialized by both paths.
- **Files to create / modify**:
  - `server/sturddle_view/play/engine_supervisor.py` (own
    `serialize_info` that returns the unified shape)
  - `server/sturddle_view/tournament/uci_parse.py` (`_parse_info`
    returns the same shape with `score_cp` / `score_mate` folded
    into `score`)
  - `server/sturddle_view/play/human_vs_engine.py` (call new
    `serialize_info`; drop the closure-over-`eval_pov` private
    helper)
  - `server/tests/test_uci_info_schema.py` (new)
  - `server/tests/perf/test_bench_serialize_info.py` (new)
- **Test list** (from audit section 7, R7):
  - `test_serialize_info_from_typed_dict_matches_schema`
  - `test_parse_info_from_raw_line_matches_schema`
  - `test_both_paths_produce_same_keys_for_same_data`
  - `test_serialize_info_score_pov` (white POV, black POV, STM POV;
    parametrized)
  - Perf: `bench_serialize_info` (100k calls within 10% of baseline)
- **Definition of done**:
  - All R7 tests green.
  - `test_tournament_uci_parse.py` updated for new schema shape and
    green.
  - Web client side noted (informational only -- web changes are out
    of scope for this battery, but the PR description records that
    the renderer can be unified in a follow-up).
  - Perf bench within 10%.
- **Out of scope**: web renderer unification.

## P10 -- R8 /api/chess/apply-move audit

- **State**: pending
- **Depends on**: none (independent; can land anywhere after P0)
- **Goal**: Confirm caller status; delete or pin.
- **Files to create / modify**:
  - If deleted: `server/sturddle_view/api/chess_utils.py` removed;
    route unregistered in `server/sturddle_view/app.py` (or its
    router include site); any e2e referencing the endpoint pruned.
  - If kept: `server/tests/test_apply_move_api.py` (new).
- **Sub-task before deciding delete vs keep**: grep `web/app/` and
  `server/` for `/api/chess/apply-move`; record findings in the PR
  description. Confirm with maintainer (project rule: ask before
  destructive change).
- **Test list** (only if kept; from audit section 7, R8):
  - `test_apply_move_accepts_legal_uci`
  - `test_apply_move_returns_204_on_late_bestmove`
  - `test_apply_move_rejects_invalid_fen`
  - `test_apply_move_rejects_invalid_uci`
- **Definition of done**:
  - Decision recorded in PR body with grep evidence.
  - Either the four tests are green or the module is gone with all
    other tests still green.
- **Out of scope**: web client refactors.

## P11 -- R9 _board_event payload split

- **State**: pending
- **Depends on**: P1 (board_event snapshots), P8 (Mode enum makes
  dispatch natural)
- **Goal**: Extract `_view_payload()` and `_play_payload()` from
  the ~120-line `_board_event`; test in isolation.
- **Files to create / modify**:
  - `server/sturddle_view/play/human_vs_engine.py` (extract two
    methods; `_board_event` becomes a dispatcher on `self._mode`)
  - `server/tests/test_board_event_payload.py` (new)
  - `server/tests/perf/test_bench_board_event.py` (new -- both
    play and view payloads)
- **Test list** (from audit section 7, R9):
  - `test_play_payload_omits_view_field`
  - `test_view_payload_includes_cursor_and_total_plies`
  - `test_view_payload_eval_at_cursor`
  - `test_view_payload_comment_at_cursor`
  - `test_view_payload_game_over_on_pgn_result`
  - `test_view_payload_threefold_termination_from_can_claim`
  - `test_tablebase_field_only_present_when_prober_set`
  - `test_opening_field_null_for_imported_games`
  - Perf: `bench_board_event_play_payload` (within 5%)
  - Perf: `bench_board_event_view_payload` (within 5%)
- **Definition of done**:
  - All R9 tests green.
  - P1 board_event snapshots still byte-identical.
  - Backstop e2e tests green: `test_e2e_view_mode_names.py`,
    `test_e2e_perspective_sync.py`, `test_e2e_replay_cursor.py`.
  - Perf benches within 5%.
- **Out of scope**: further HVE splits (deferred R10).

## Conventions for future sessions

### Marking a phase done

1. Tick the checkbox in the Status section.
2. Flip the phase's `**State**: pending` line to
   `**State**: done (<sha>)` where `<sha>` is the merging commit's
   short SHA.
3. Update the "Last updated" date at the top of this file.
4. If the phase changed scope mid-flight, note the deviation under
   the phase's body in a `**Notes**:` bullet list.

### Adding a new phase

If scope creeps beyond the planned 12 phases, append a new phase
with the next ID (P12, P13, ...). Add it to the Status checkbox
list. Do not reorder existing phases. Do not silently absorb new
work into an existing phase that has already been partially
executed.

### Red-then-green discipline

Every refactor phase must commit the failing tests first
(passing only the trivial path or skipped via xfail with a
ticket-style comment), then the implementation. Two commits per
refactor phase, minimum. This protects against accidentally
writing a test that already passes against the unrefactored code
and therefore tests nothing.

### Perf benches

- Default `pytest` run skips perf tests.
- Run with `SV_RUN_PERF_BENCHES=1 pytest server/tests/perf/`.
- Regenerate baselines with
  `SV_RUN_PERF_BENCHES=1 pytest --update-perf-baselines server/tests/perf/`
  under a quiet machine (idle, AC power, fixed CPU governor).
- Never bundle a baseline bump with a refactor PR. Baseline bumps
  ship as their own PR with a written justification.
- A bench that fails by less than the noise tolerance is still a
  signal. 100ns -> 108ns is a real regression in aggregate.
  Investigate before merging.

### Perf baseline capture protocol

Phases that swap behavior (P4, P5, P6, P7, P9, P11) must NOT
baseline the new implementation against itself. Sequence:

1. **Commit A** (baseline capture): add the bench file pointing at
   the CURRENT, unrefactored implementation. Run
   `SV_RUN_PERF_BENCHES=1 pytest --update-perf-baselines <file>` and
   commit `baselines.json` together with the bench. Commit message:
   `perf: baseline <name> before <phase-id> refactor`. No production
   code touched.
2. **Commit B** (red tests): add the unit tests for the new module;
   they fail because the new module does not exist yet.
3. **Commit C** (green refactor): land the refactor. Re-run the perf
   bench WITHOUT `--update-perf-baselines`; it must stay within
   tolerance of the baseline captured in Commit A. If it drifts
   beyond tolerance, do not update the baseline -- investigate the
   regression (audit section 8.4).
4. If the perf bench cannot be written against the OLD
   implementation because the bench input shape only exists after
   the refactor (rare; flag in PR), document the deviation in the
   phase body under `**Notes**:` and capture an alternative pre/post
   timing in the PR description.

Phases adding new perf benches that are NOT comparisons (P0 smoke,
P4 "DO NOT TOUCH" locks for `pgn_stats` line-scan paths) can baseline
and bench in the same PR -- the bench protects against future
regressions, not against this refactor.

### Project rules reminder

- ASCII only.
- No `Co-Authored-By` trailers in commits.
- Minimal commits (one logical change per commit).
- No inline string literals once a named constant exists.
- Propose before coding non-trivial changes.
- Ask before committing.
- Never inline helpers; helpers live in the owning module and are
  imported.
- No unsolicited visual changes.
- Env var prefix is `SV_`.
