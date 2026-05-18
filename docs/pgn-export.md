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
