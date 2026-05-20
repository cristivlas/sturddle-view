# PGN Export — Design Proposal

Status: proposal, not yet implemented.

## Problem

Users have no way to save the current game from the browser to local
disk. The feature must cover:

- Play mode (in-progress and finished games).
- View mode after a PGN import (must round-trip the original PGN
  verbatim: headers, variations, NAGs, `%clk`, `%emt`, PV lines,
  cutechess/fastchess annotations, root comment, etc.).
- View mode after a FEN import (no game history beyond the FEN).
- Any cursor position; download contains the full game, not a slice.
- Games that start from a non-standard FEN.
- Must work with current editing feature, and be future proof to new features (e.g. client-side annotations/commentary).

Frontend is chess-dumb; all chess knowledge lives server-side via
python-chess. No client-side PGN assembly.

## Existing Infrastructure (relevant)

- `recent_imports` store (`server/sturddle_view/recent_imports.py`):
  content-addressed by SHA-256, persists trimmed raw text verbatim,
  retrievable via `GET /game/recent-imports/{hash}`. Already returns
  the original PGN/FEN text byte-for-byte.
- `import_game` (`server/sturddle_view/api/game.py`) saves the raw
  import text on every successful `/game/import` and returns the
  `hash` to the client.
- `_maybe_save_pgn` (`server/sturddle_view/play/human_vs_engine.py`):
  builds a `chess.pgn.Game` from the play-mode board, sets headers,
  attaches per-ply `[%clk]` annotations, writes the result to
  `pgn_dir` (if configured). The PGN-building logic is currently
  bound to the file-write path.

## View Mode is NOT Lossy at the Persistence Layer

`parse_pgn` strips/sanitizes the parsed in-memory state (comments
get machine annotations stripped, variations dropped, etc.) but the
**raw text** passed to `/game/import` is preserved verbatim in
`recent_imports`. Any view-mode game opened via PGN import is
recoverable as the user's original bytes.

For PGN imports, no new server endpoint is needed: the client
already has the hash from the import response and can call the
existing `GET /game/recent-imports/{hash}`.

For FEN imports, the same store holds the raw FEN text under the
same hash mechanism.

## Gap

Play-mode games have no raw-text origin. The PGN must be generated
from the live board (`chess.pgn.Game.from_board()` plus headers and
`%clk` annotations). This is exactly what `_maybe_save_pgn` does,
but currently coupled to disk I/O and to the `pgn_dir` setting.

## Option A — Implemented

`_maybe_save_pgn` already delegated to the standalone `build_pgn()`
function in `chess/pgn_build.py`, so no extract-method refactor was
needed. Instead, a new `get_pgn_text() -> tuple[str, str] | None`
method was added to `HumanVsEngine`:

- Returns `(pgn_text, suggested_filename)` or `None` (no game / no moves).
- **View mode with raw text** (`_view_raw_text` set): returns the
  verbatim original import text -- zero metadata loss.
- **View mode without raw text** (entered via `/game/view/start`
  fork, not a direct import): rebuilds from `_view_full_moves` and
  available headers. This is the one lossy case; annotations on the
  pre-fork prefix are lost.
- **Play mode**: builds fresh PGN from the live board with
  `result="*"` / `termination="unterminated"` for in-progress games.
- **FEN-only view** (no moves): returns `None`; endpoint replies 409.

`ViewModeParams` and `_ViewSnapshot` both carry `view_raw_text`.
`import_game` passes `raw_text` as `view_raw_text` so the verbatim
bytes travel with the session for the duration of the view.

One new endpoint: `GET /game/pgn` -- returns the file with
`Content-Disposition: attachment`.

Frontend: floppy-disk button on both play and view ribbons
(`desktop-only`). Single `onSavePgn` handler does a plain `fetch`
and triggers a blob download with the server-supplied filename.

### Pros

- Smallest blast radius. No changes to data model, no new state on
  HVE, no new persistence.
- Autosave path keeps its exact current semantics; the refactor is
  mechanical (extract method).
- Clear separation: imports come from `recent_imports`, play-mode
  comes from a fresh build.

### Cons

- Two code paths on the client (hash-based vs endpoint-based).
  Client must know which mode it is in. (It already does, via the
  `viewing` flag and the import hash it holds.)
- Play games are not retained anywhere unless the user has
  `pgn_autosave` + `pgn_dir` configured. Re-downloading the same
  finished game after a page reload is not possible.
- View mode after `play_from_here` followed by more moves: original
  hash no longer matches the current game. Export would have to
  fall through to the play-mode builder for the post-fork state,
  which is fine but worth calling out.

## Option B — Unified: Reuse `recent_imports` for Play Games

On game end (and optionally on-demand via `GET /game/pgn`), call
`_build_pgn(...)` and then `recent_imports.save(fmt="pgn", text=...)`
with a summary like `"Human vs MyEngine — 1-0 (checkmate)"`. The
returned hash is published on a `board_update` field
(`play_pgn_hash`) or returned by `/game/pgn`.

Frontend always downloads via `GET /game/recent-imports/{hash}`.
Single code path.

### Pros

- One client code path for both modes. Simplest frontend.
- Free side effect: completed play games appear in the recent-imports
  dropdown, so users can re-open / replay them like any imported PGN.
  This is arguably a feature (the dropdown becomes a session log).
- Survives reload: the hash is persisted; re-downloading a finished
  game across reboots works.
- Mid-game export still works: we can either save mid-game
  snapshots (noisy) or only save at game-end and serve a one-shot
  fresh build for in-progress exports.

### Cons

- Polluting recents with auto-saved play games changes the meaning
  of "recent imports" — it becomes "recent games." The dropdown,
  cap (50), and eviction now compete with explicitly imported
  positions. May need a separate cap or a `fmt="play"` tag and a
  UI filter.
- Save-on-every-move (to support mid-game export via hash) would
  rewrite the same hash slot repeatedly (content-addressed: hash
  changes per move = new blobs every move = eviction churn). Either
  accept "no mid-game hash, fall back to fresh build" or skip the
  save until game-end.
- Race condition surface: `_maybe_save_pgn` already runs on resign,
  time forfeit, normal end, and unterminated autosave. Adding a
  recents.save into the same code path needs care so a half-written
  game doesn't replace a finished one.
- Coupling: the play engine now writes to a store that was scoped
  to user-initiated imports. Conceptual creep.

## Recommendation

Option A. The autosave-to-`pgn_dir` feature stays as-is; the new
endpoint is a 20-line wrapper around an extracted `_build_pgn`. The
"two client paths" objection is small in practice — the frontend
already knows whether it's in view mode and already holds the
import hash, so the branch is one `if`.

Option B's "completed games show up in recents" is appealing but
better delivered as an explicit feature with its own store /
dropdown section, not as a side effect of solving export.

## Resolved Questions

1. Mid-game: `result="*"`, `termination="unterminated"`. Implemented.
2. Post-`play_from_here` fork: exports the live board; pre-fork
   annotations are lost. Accepted cost.
3. Filename: `sturddle-{YYYYMMDD-HHMMSS}-{white}-vs-{black}.pgn`,
   non-alnum chars replaced with `_`, delivered via
   `Content-Disposition`. Implemented.
4. FEN-only view: 409, no download. Implemented.

<!-- ===================================================================
     TODO sections below. Search "TODO:" to find actionable items.
     Status tags: [TODO] not started, [WIP] in progress, [DONE] complete.
     =================================================================== -->

## Engine Evaluations in Exported PGN

Status: **[DONE]**

### Gap

Saved games carry no engine evaluations. Two layers:

- `build_pgn()` has no `eval_history` parameter; there is no path to
  serialize per-ply scores even when they exist.
- Play mode never accumulates per-ply evals. `_pump_engine_info()`
  streams scores to the WebSocket for UI only; nothing is retained.
- The play->view transition (`view_start()` ->
  `play_game_snapshot()` -> `enter_view_mode()`) uses a direct state
  copy that mirrors the same gap: moves and clocks only.

View-mode imports already populate `_view_eval_history` (white POV)
via `_parse_pgn_eval`, so the read side is solved; the write side
and the play-mode capture are not.

### Output Format Decision

Use cutechess/fastchess convention end-to-end. No backward compat
constraint in this project.

- Per-ply trailing comment token: `{<eval>/<depth> <time>s}`.
- Eval: float pawns (`+0.34`) or `M<n>` / `-M<n>` for mate.
- POV: STM (engine's own score for the side that just moved).
- Time: elapsed for that move, in seconds.
- Drop `[%clk]`; per-move elapsed time is in the same token.

Memory representation stays white POV (matches the existing
importer and `_view_eval_history` shape). POV flip happens at write
time in `build_pgn()` on black-to-move plies.

### Capture (Play Mode)

Add `_eval_history: list[dict | None]` on HVE, appended once per
ply, snapshot taken **post-move**. For engine plies, capture the
deepest score seen during the search that produced the move; for
human plies, append `None`. Normalize to white POV at capture.
Entry shape: `{"cp": int}` or `{"mate": int}`, optionally with
`"depth": int` -- matches `_view_eval_history`.

Take-back pops `_eval_history` alongside `move_stack`; the
invariant `len(_eval_history) == len(move_stack)` holds at all
times outside the lock-held push.

### Persistence

`_eval_history` is part of `GameState` and survives server
restart. Old saves (pre-`eval_history`) are upgraded transparently
by `restore_from` -- length mismatch falls back to all-None so the
invariant holds and subsequent engine searches still capture.

Without persistence, a finished game saved across a restart would
lose every pre-restart engine eval (the in-memory list resets to
`[None] * n_plies`), defeating the export feature for the most
common case.

### Plumbing

- `play_game_snapshot()` includes `eval_history`.
- `ViewModeParams` / `_ViewSnapshot` carry `eval_history`.
- `enter_view_mode()` stores into `_view_eval_history` unchanged.
- `GameState` carries `eval_history`; `_persist` writes it,
  `restore_from` reads it.
- `build_pgn()` gains `eval_history: list[dict | None] | None`;
  when present, writes the cutechess token per ply, flipping POV
  for black-to-move plies, and **drops `[%clk]` entirely** (the
  per-move elapsed time travels inside the cutechess token).
- Play-mode export ALWAYS passes `eval_history` (even when every
  entry is None) so output is uniformly cutechess-formatted: human
  plies get the time-only `{<time>s}` token, engine plies get the
  full `{<eval>/<depth> <time>s}` token. There is no fall-back to
  `[%clk]` on play-mode export.
- Play-mode `get_pgn_text()` passes `_eval_history`; view-mode
  rebuild branch passes `_view_eval_history`.

### Read-back

Importer handles both cutechess trailing tokens (full `<eval>/<depth>
<time>s` form and time-only `<time>s` form) and Lichess
`[%eval ...]` brackets, so saved files round-trip. The time-only
regex was added when the spec moved to "drop `[%clk]` entirely" --
prior to that, human plies kept their clocks via `[%clk]` and no
time-only cutechess token was ever emitted.

### Testing Plan

Unit (`pgn_build`):
- White-to-move ply with `{"cp": 34}` writes `+0.34/<depth>` (no flip).
- Black-to-move ply with `{"cp": 34}` writes `-0.34/<depth>` (POV flip).
- Mate scores: `{"mate": 5}` on white -> `M5`; on black -> `-M5`.
- Missing eval on a ply emits time-only token; no bare `/<depth>`.
- `eval_history=None` produces identical output to today (no token,
  no `[%clk]`).
- Length mismatch (eval_history shorter/longer than moves): defined
  behavior -- truncate or raise, pick one and assert it.

Round-trip (`pgn_build` -> `_parse_pgn_eval`):
- Build a game with mixed cp/mate/missing entries (white POV in
  memory), serialize, re-parse via `_parse_pgn_eval`, assert the
  re-parsed white-POV values equal the originals. Critical for the
  sign-flip path on black plies.

Play-mode capture (HVE):
- After N engine moves, `_eval_history` has N entries, white POV,
  shape matches `_view_eval_history`.
- Human ply with no engine search recorded -> `None` entry.
- `play_game_snapshot()` carries `eval_history`; `enter_view_mode()`
  populates `_view_eval_history` from it; values unchanged.

Transition + export:
- Play a few moves, click edit, export from view mode: PGN contains
  the captured evals. Compare to `_eval_history` snapshot pre-transition.
- Play -> finish -> export directly from play mode: same evals.

Imported PGN round-trip:
- Import a cutechess PGN with evals, export, re-import. Assert
  `_view_eval_history` equal across both imports.
- Same for a Lichess `[%eval]` PGN (mixed-format read, cutechess
  write).

Negative / edge:
- Empty game (no moves): export still succeeds, no eval tokens.
- FEN-only view: unchanged (409).
- `eval_history` present but all `None`: no eval tokens emitted.

## Auto-Save Finished Games to `recent_imports`

Status: **[DONE]**

### Behavior

On true game-end (natural outcome via `_finalize_game_locked`,
resignation, or time forfeit), the finished PGN is written to the
recent-imports store under `fmt="pgn"` with `summary["source"]="play"`.
The active session's `game_id` is bound to the row, so the row is
recoverable across reloads and surfaces in the existing recents
dropdown via the same `GET /game/recent-imports` path used by
imports.

### Resolved questions

- **Trigger**: game-end only. Per-move autosave (`result="*"`) is
  NOT written to recents to avoid hash churn.
- **Independence from `pgn_autosave`/`pgn_dir`**: orthogonal. Both
  features fire on game-end; one writes to user-chosen disk dir,
  the other to the in-app store.
- **Tag**: `summary["source"]="play"` (no schema bump). Imports
  omit the field. UI filtering can key on this when wanted.
- **Cap**: shares the existing 50-entry LRU. Pinned-by-active-session
  protection already exists. If finished-game accumulation becomes a
  pain, split caps later.
- **Empty games**: skipped (no moves -> nothing meaningful to save).

### Plumbing

- `HumanVsEngine.__init__` takes `recents: RecentImports | None`.
  `None` is a no-op (preserves test isolation; characterization
  tests don't trip the save path).
- `_stash_recents_payload(result, termination)` builds the PGN +
  summary under the lock and stashes on
  `_pending_recents_save`.
- `_flush_recents_save()` is awaited after the lock releases.
  Failures are logged and swallowed; game-end signaling must never
  block on the recents write.
- Wired into all three game-end paths: `_finalize_game_locked`,
  `resign()`, `_handle_flag_fall()`.
- `create_app` and the lazy `/game/new` HVE ctor both pass
  `recents=s.recent_imports`.
