# Annotation Edit -- Design Sketch

Status: implemented, including Edit from play (below).

## Problem

The viewer surfaces per-ply PGN comments (and a pre-game root comment),
but they're read-only. Users want to add, modify, and delete their own
annotations on the game they're reviewing.

We want:
- A single, simple UX for add/edit/delete (one modal, one commit).
- No collateral damage: annotating must not destroy moves, headers,
  eval history, clocks, or anything else about the game.
- The edited annotations must persist so they survive a session.
- Identity (`game_id`) stays stable across annotation edits even though
  content (and the content hash) changes.

## UX

Entry is gated by edit mode (which today is FEN-position editing). The
mental model becomes: edit mode is the "intent to mutate this viewed
game" container. It hosts two operations:

1. Position edits (existing) -- mutate the FEN. If FEN differs at commit,
   move history is dropped and a fresh view at the new position starts.
2. Annotation edits (new) -- mutate the comment at the current ply (or
   the root comment when cursor=0). Always preserves moves/history.

### Modal

Triggered from a new ribbon button while in edit mode (icon TBD; the
pencil is already taken by the Edit-position entry button -- candidates:
`comment`, `message`, `note-sticky`, `quote-right`, or a `T`/`A` glyph).
The modal contains:

- A `<textarea>` preloaded with the current ply's comment, if any.
- A "Clear" affordance (nicety; user can also select-all-delete).
- Cancel / OK buttons.

Commit semantics:
- Non-empty after `.strip()` -> set/replace the comment at the cursor.
- Empty after `.strip()` -> delete the comment at the cursor.

Add / edit / delete all converge to a single "apply this string"
operation. No separate destructive action.

No confirmation on delete: the "Clear" affordance is explicit, and the
user is in edit mode by intent. Reversibility is via re-typing before
commit; once committed, the change is durable (subject to a future undo
feature).

## Server: HVE-side mechanics

The view-mode game already carries:

- `_view_comments: list[str | None] | None` -- per-ply, index `i-1` is
  the comment after move `i`.
- `_view_root_comment: str | None` -- shown at cursor 0.

Mutation API (new):

```python
async def set_view_comment(self, ply: int, text: str | None) -> None:
    """ply==0 sets root_comment; ply>0 sets _view_comments[ply-1].
    text=None deletes. Recomputes _view_hash and _view_original_text."""
```

On commit:
1. Update the in-memory comment array (or root).
2. Regenerate `_view_original_text` via `build_pgn(...)` from the live
   view-mode state (start_fen, moves, clock_history, eval_history,
   the now-edited comments, root_comment, headers, result, termination).
3. Recompute `_view_hash` from the new raw text via `canonical_hash`.
4. `game_id` is unchanged.

## Server: recents store mechanics

Today the store keys rows by content hash (`_index: hash -> row`) with a
reverse `_by_id: game_id -> hash`. The reverse index enforces uniqueness:
one game_id can only map to one hash. So when content changes for a
preserved game_id, the old hash row MUST be evicted and the game_id
re-bound to the new hash.

### Atomic replace primitive

Add to `RecentImports`:

```python
async def replace_at(
    self,
    old_hash: str | None,
    fmt: str,
    text: str,
    summary: dict,
    game_id: str,
    precomputed_hash: str | None = None,
) -> str:
    """Atomically swap content for a preserved game_id.

    Under one lock acquisition:
    - If old_hash is given and present, evict that row + blob.
    - Insert/upsert the new (fmt, text, summary, game_id) row.
    - The reverse index points game_id at the new hash.

    Returns the new hash.

    old_hash=None is equivalent to save() with the same args.
    """
```

The single lock acquisition guarantees no observer ever sees the
`game_id` unmapped or pointed at a stale row.

### Exit-from-edit decision matrix

| FEN change | Comment change | Action on exit                                |
|------------|----------------|-----------------------------------------------|
| no         | no             | no-op (existing behavior preserved)           |
| yes        | n/a            | new game_id, new FEN-only recents row         |
| no         | yes            | same game_id, `replace_at(old_hash, ...)`     |

"Comment change" is detected by comparing the new text with the comment
at the entry ply (a no-op commit changes nothing). The FEN-change branch
keeps its existing behavior (history dropped, FEN-only row written) and
supersedes any in-flight comment edit -- the truncated game is a new
game by identity. On a live clone the FEN-change branch also leaves the
live game (see Edit from play).

### Annotation commit always replaces a row

Every view that is not a live clone has a Recents row: import and a
position-changed commit both write one, and play -> view is a live clone
(see Edit from play). So an annotation commit always
`replace_at`s the row found by `recents.hash_for_id(game_id)`; a view
with no row (test hooks only) writes nothing. The `old_hash=None` insert
in `replace_at`, the comment branch's fork-link carry, `clear_fork_link`
and the client's x-game refetch after a comment commit had one caller,
the promote-on-annotate flow of the old play -> view path, and go with
it. `replace_at` keeps its fork-link parameters: the finished-game save
passes them. A live clone never touches Recents: its comments go home to
the live game.

## Client mechanics

The annotation-edit ribbon button POSTs the new endpoint (e.g.
`/game/edit/annotate {ply, text}` or folded into `/edit/commit` with a
new optional field -- TBD during implementation).

Cache refresh on response:
- Update `_viewingHash` to the new hash from the response.
- Refetch `/game/recent-imports` to rebuild the dropdown cache. Simple
  and authoritative; can be revisited if it shows up as a perf issue.

## `_view_original_text`: freeze as the import artifact

Today `_view_original_text` is informally "the imported PGN bytes." There's
no mechanism preventing a future code path from reassigning it, and the
field name implies "live raw text" -- which it isn't.

Going forward, treat it as a write-once import artifact:

- Set exactly once in `enter_view_mode` from `params.view_original_text`.
- Declare with `Final` and document loudly that it is the **imported
  fossil**, never to be updated.
- No mutation code path (annotation edit, anything future) touches it.

Export decision becomes: if structured state still matches the import
(no edits since), returning `_view_original_text` is an optimization that
skips re-serialization. If state has diverged (post-edit), serialize
via `build_pgn`. Either way the raw remains a faithful copy of what
the user pasted -- which gives us a free **"revert to imported"**
affordance later.

This sidesteps the staleness risk entirely: there is no staleness
question because the raw is never claimed to reflect post-edit state.

## Prerequisite: `build_pgn` comment support (latent bug)

`build_pgn` was specified to never lose metadata on a round-trip. It
silently does today: the function signature has no `comments` or
`root_comment` parameters, and it never sets `node.comment` /
`game.comment`. The reason this hasn't been observed:

- Import -> export is **verbatim**. `get_pgn_text()` returns
  `self._view_original_text` (the original imported bytes) when present,
  bypassing `build_pgn` entirely. So comments survive that path purely
  by virtue of the cached raw text, not because `build_pgn` handles
  them.

- The two paths that DO go through `build_pgn` are:
  - `_build_play_game_pgn` -- live play game serialization
    (autosave / recents-on-end / download). No comments today (live
    play has no comment storage either, see below).
  - The view-mode regeneration branch in `get_pgn_text` (post line
    1766) when `_view_original_text` is None -- can happen after
    play_from_here folds a view-mode prefix into a play game, then the
    live game ends.

Both paths are silently dropping comments today.

### Required fix

Non-negotiable per the original spec. Three changes:

1. Extend `build_pgn` to accept:
   ```python
   comments: list[str | None] | None = None      # len == len(moves_uci)
   root_comment: str | None = None
   ```
   When provided, set `node.comment` on each mainline node (sanitized
   to coexist with the existing `[%clk]` / eval token emission) and
   `game.comment` on the root.

2. Add HVE play-side fields:
   ```python
   self._play_comments: list[str | None] | None = None
   self._play_root_comment: str | None = None
   ```
   Parallel to the view-side fields. Cleared by whatever resets the
   play game today; populated by `play_from_here` from
   `_view_comments[:cursor]` and `_view_root_comment`.

3. `_build_play_game_pgn` plumbs both into `build_pgn`.

### Carry-over on play-from-here (observable bug)

Today, `play_from_here` extracts moves, clocks, eval history, start_fen
-- but discards `_view_comments` and `_view_root_comment`. `_reset_view_state()`
then wipes the view-side fields, and `new_game(...)` has no comment
parameters. The resulting play game has no commentary.

Once `_play_comments` / `_play_root_comment` exist, `play_from_here`
should set them from `self._view_comments[:cursor]` and
`self._view_root_comment` before calling `new_game`. (Note: comments
are sliced parallel to the moves -- `_view_comments[i-1]` is the
comment after move `i`, so the seed slice is `[:cursor]`, same bound
as `seed_moves`.)

### Test coverage

This must be tested exhaustively. Minimum surface:

- `build_pgn` unit tests:
  - root_comment round-trips through both clock-history and eval-history
    emission paths.
  - per-ply comments round-trip in both modes (clk + eval).
  - sparse comments (mix of None and string) preserve positional
    alignment.
  - empty/whitespace comment values are handled per python-chess
    semantics (likely written as `{}` -- check and document).
  - length mismatch (`len(comments) != len(moves)`) raises like
    `eval_history` does.
- HVE integration tests:
  - Import PGN with root + per-ply comments -> play_from_here at
    mid-cursor -> end the play game -> exported PGN preserves the
    seeded comments at the correct plies.
  - Same, exported via the `_build_play_game_pgn` autosave path.
  - Round-trip parity: import a known PGN with comments, export via
    the non-verbatim path (force `_view_original_text=None` to simulate
    post-annotation-edit state), expect comments preserved.

## AI prose -> annotation shortcut (planned)

Front-end feature (plus the server comment round-trip fix below, a
standalone bug fix it depends on). Pencil while finished AI prose is
showing behaves as pencil + comment button pressed in succession: edit
mode opens and the annotation modal comes up prefilled with the AI
prose. Saved AI prose is the user's annotation from then on, fed to
later AI prompts like any other comment (no origin marker -- that would
need a non-standard PGN tag).

Requirement: cleanest implementation -- reuse the existing edit-entry
and annotate paths; no parallel copies of either.

### Trigger

Either pencil -- view ribbon (`#view-edit`) or play ribbon (`#edit-pos`)
-- with the AI turn done (`aiAnalysisDone(state)`) and visible AI prose
(see Prefill source). From play mode, entry is Edit from play (below):
the same entry as the plain pencil, plus the prefill.

### Flow

1. Read the AI prose into `state` before anything closes the AI panel.
2. Skip the "stop analysis?" confirm -- the turn is finished and its
   prose carries over.
3. Normal edit entry (`/game/edit/start`): analysis stops, the AI panel
   closes, the edit ribbon shows. On failure, clear the saved prose.
4. Once edit mode is live -- at the end of the board_update that turns
   it on, after `_onServerEditingStart` resets `pendingAnnotation` and
   the view state settles (event-driven, no timers) -- press the comment
   button programmatically, then clear the saved prose (single use).
   The ribbon ends up in exactly the state a real click leaves (no new
   styling); the button's handler opens the modal preloaded per Prefill
   text below. A later ordinary pencil press never sees
   stale prose.
5. OK -> staged as `pendingAnnotation`, exactly as a manual annotate;
   the user still confirms the edit to commit it.
6. Cancel -> modal closes, nothing staged, edit mode stays open.

Fallback: no visible prose -> normal pencil flow.

### Prefill text

Never silently replace a user comment:
- Ply has no comment -> the AI prose.
- Ply has a comment -> existing comment, blank line, AI prose.
- Existing comment already contains the AI prose (e.g. a second AI run
  after annotating) -> the existing comment unchanged. "Contains" is
  compared with whitespace runs collapsed on both sides (shared
  `collapseWhitespace` in `text-utils.js`): a reload re-imports the
  comment through `_sanitize_comment`, which collapses intra-paragraph
  whitespace.

### Prerequisite: comment round-trip fix (server)

`_sanitize_comment` (`play/import_position.py`) rewrites comment prose
on every import, so saved annotations (hand-typed today, AI prose with
this feature) come back altered on reload:
- every parenthesized span is stripped as an "inline variation" --
  "the knight (on f3) defends", "wins material (25.Rxd5 Qxd5)";
- the first letter is capitalized -- corrupts SAN leads ("e4" -> "E4").

Fix: drop capitalization (it only papered over a lowercase word exposed
by a stripped leading tag). Narrow the paren strip to its one real
source: cutechess-style comments carry an engine PV / book marker in
parens (`{(Ng5) 0.35/19 17}`, `{(Book)}` -- ~380 in the repo's PGN
fixtures; every group is SAN moves or `Book`). A remainder that is
exactly one parenthesized group of SAN moves (move numbers allowed) is
dropped only when machine tokens were stripped from that comment; the
`(Book)` marker is always dropped. A human `{(forced)}` stays even
beside the `[%clk]` tag `build_pgn` adds, as do parens anywhere in
prose. Accepted residual: a saved comment that is nothing but a
parenthesized move line reads exactly like an engine PV and is dropped.
SAN sub-patterns move from `llm/position_check.py` to a shared
`chess/san_patterns.py`. One rule for all sources: no app-saved marker, no second
import path.

Kept: `[%...]` tag stripping and the cutechess/fastchess trailing
tokens, with one tightening:
- Eval token (`_CUTECHESS_EVAL_RE`): require an engine-shaped eval --
  signed (`+0.35`, `-2`, `+M5`) or unsigned decimal (`0.00`). An
  unsigned integer (`1/2`) no longer matches, so prose ending "holds
  the draw, 1/2" survives. Matches every eval shape in the repo's PGN
  corpus (~175k tokens, none unsigned-integer); the same regex parses
  evals, which tightens consistently. The eval shape is one shared
  pattern, also used by the cutechess time parser.
- Trailing time (`_TRAILING_MACHINE_TIME_RE`, decimal `s` / integer
  `ms`): kept as is. Accepted risk: prose ending in such a duration
  ("... in 2.5 s") loses it.

Tests: replace `test_strips_parenthesized_variations` with keep-parens
and PV-only-remainder tests; drop the synthetic `(alternative line)`
from `test_strips_clk_and_eval_brackets`; update the lowercase-lead
expectations (`"took 7s" == "Took 7s"` etc. in `test_pgn_comments.py`
and `test_pgn_eval_parse.py`); add a round-trip test (prose with
parens, a variation, a SAN lead, a trailing "1/2" and a lone "(forced)"
survive save -> reload, both the [%clk] and the eval-token PGN paths).

### Prefill source

`play-ai-window.js` exports one accessor returning the prose of the
latest round whose prose is visible and non-empty, as `textContent`
(plain text; opening links flatten to their names). Earlier rounds are
searched back when the last one is hidden, folded or empty; none
qualifying -> null (the pencil falls back, see Flow). Client-side because the displayed prose is
client-derived (ack-lead scrub, strikes); a server copy would duplicate
that logic. The panel module owns its DOM, so the accessor is the only
reader.

Visibility is one shared helper, `isProseVisible(entry)`: para not
hidden (`hideProse`) and not folded into its revision. It replaces the
two inline variants today (`linkAiRecommendation`, the final-prose
divider in `markAiDone`) and backs the accessor. Every round panel has
a revision, so the dead guards go: the divider's `revision?.` and
`noteAiPosition`'s `!entry.revision`.

## Edit from play

Annotate a game in progress -- by hand or from AI prose -- and keep
playing the same game, with the notes on it.

### Problem

Edit runs on a view session. From play, the client first flips the live
game into an in-memory clone (`/game/view/start`), then enters edit on
the clone. What happens to the live game depends on how you got there:

- **Plain pencil:** asks to discard the game, clones without suspending.
  A saved comment lands on the clone, which is promoted to Recents; the
  live game is gone. The only way on is Play from here -- a fork. Every
  annotate-and-continue cycle adds a fork and a Recents copy.
- **AI pencil, and scrub-back (click a past move):** suspend the live
  game, so it can be resumed. But a saved comment still lands on the
  clone and its Recents copy; resuming brings the live game back without
  it. The notes end up in a side copy.
- **Resume from an AI turn restarts the clock.** The suspend snapshot is
  taken in ANALYZING (analysis runs only from a paused game) and records
  `paused=False`.
- **A restart mid-game loses comments.** The live game's crash-recovery
  snapshot (`GameState`) has no comment fields.

### Model

The clone stays: it is what protects the original while the position is
edited. But a clone of the live game is temporary and never has a row of
its own. The live game is the only live copy; a Recents row for it is
either a user export (Save PGN) or the save made when it is left or
finished. Never a clone copy.

- **Live clone.** A view session that suspended a live game. Every edit
  entered from play works on one, and so does edit entered from a
  scrubbed-back view (the view pencil while clicked back into a game in
  progress). The clone keeps the live game's `game_id` (today it mints
  its own): the client drops any board update whose id differs from the
  one it shows, and only the retired resume-play path cleared that
  filter. With one id, no operation ever switches ids. Consequences:
  edit commit decides "live clone" from session state, not from a Recents
  lookup by id (an exported live game has a row under that id); a forked
  live game's parent toast shows on the clone. On entry the clone takes
  the hash of its game's Recents row, if any (`/view/start` sets none
  today): the dialog matches "viewing" on hash, so without it the row
  keeps its trash, the server's by-id viewed check fires, and the
  leftover force-delete path (`close_view`) would resume the live game
  with the row already gone. Clone commits never touch Recents and never
  rewrite `_view_hash` (today `_apply_view_annotation` does; on a clone
  it updates the comments only), so the hash stays the row's. Save PGN
  on a clone downloads the clone's PGN, notes included, and writes no
  row, as on any view.
- **Live clones never go to Recents.** An annotation committed on one
  updates the clone, copies the comments into the suspended game, and
  writes its crash-recovery snapshot (through the store directly:
  `_persist` skips views). Views are never persisted, so
  without this a restart mid-clone would drop the note (today it survives
  in the clone's Recents copy).
- **Return to live:** today's restore, unchanged (`_restore_suspended_play`
  into `restore_from`). The comments arrive with the suspended snapshot,
  which the clone commit already updated and wrote; there is no second
  copy and no second write. The clone was seeded from the live game's
  comments, so the snapshot's set is the complete, current one; the moves
  are identical by construction (the position did not change).
- **A live clone never rests on its last ply.** Outside edit mode, any
  step that would leave a clone's cursor on the last ply returns to live
  instead: a navigation op (scrub forward), or an edit exit (cancel, or a
  commit that did not change the position). One rule covers both: edit
  from play opens its clone at the last ply, so its exit returns to play;
  scrub-back always lands earlier, so an edit from a scrubbed-back view
  exits back to that view. No "entered from play" flag.
- **Leaving the live game** for another one -- a position-changed
  commit, Play from here mid-game, New Game, or import -- whether you are
  on a live clone or playing: the live game (carrying the clone's
  comments, if on one) is saved under its own `game_id` by the same pair
  of saves as a finished game (disk PGN autosave and Recents), so both
  copies carry the notes, then dropped. The Recents save captures the
  fork link with its payload, as `export_to_recents` does: the
  finished-game flush reads the live field at write time, which the
  replacing operation has already reset (`new_game`, `enter_view_mode`),
  and on Play from here it would read the child's fresh link and write
  the parent as its own parent. The original is never lost.
  Leaving runs at the replacing operation's commit point, after
  everything that can fail (seed or FEN checks, engine spawn): a failure
  leaves the live game or clone untouched. A game with no moves is not saved, matching the rule that
  zero-move games never reach Recents. One server helper does this for
  every path that replaces a live game. On a live clone the helper first
  does Return to live without publishing (restore only: no tick, no
  engine kick, no board update, so the client never flashes the live
  board before the new game), then the normal save and drop. The PGN
  builder (`_build_play_game_pgn`) reads the live in-memory state, which
  on a clone is the view; restoring first keeps that builder the single
  one, and the clone's comments come along through Return to live rather
  than through a second builder over the snapshot. Dropping includes clearing the
  crash-recovery snapshot (`GameStore`): today import and FEN-commit
  leave it behind, so a restart would resurrect the abandoned game
  beside its Recents copy.
- **The game in progress cannot be imported over itself.** An import
  whose content resolves to the live game's `game_id` (its exported row,
  opened from the dialog or pasted verbatim, while playing or on its
  clone) is refused with 409 `in_progress`; the client toasts. Without
  the guard, `/game/import` resolves the id before the flip, Leaving
  rebinds that id to the current content, and the import's own Recents
  save then asserts on the stale hash. The dialog disables that row too,
  tagged "playing": one matcher for the current game's row, by hash
  (viewing) or by the live `game_id` (playing), so the row never
  dead-ends in the toast; the 409 stays as the guard for pasted text. A
  pasted older export is different content and imports as its own row,
  as any PGN would.

### Entry

`/game/edit/start` becomes legal from play (PLAY, PAUSED, and ANALYZING
entered from play). The server does the flip itself: suspend the live
game into a live clone at its last ply and enter edit. One call from the
client.

- Plain pencil: no discard confirm (nothing is discarded any more).
- AI pencil: the same call, plus the prefill. The "stop analysis?"
  confirm still applies to an unfinished AI turn, and runs before the
  call (today it runs after the flip, when analysis is already
  cancelled).
- The client's two-step flip goes: `/game/view/start` + `/game/sync` +
  `suppressCommentsForEditTransition` on the pencil path.
- Entry pauses the live game where pausing is allowed (your turn), so it
  comes back paused. On the engine's turn it comes back as it was.
- One server helper builds the live clone at a given ply: snapshot,
  comments, summary, Recents hash. Edit-from-play and `/view/start` both
  call it; the flip is not duplicated in the API layer (today
  `/view/start` assembles it inline). The fork link rides in the
  snapshot; `/view/start` stops passing it through and `enter_view_mode`
  loses its `fork_link` parameter (no other caller). `enter_view_mode`
  splits around one view installer: the clone helper suspends, then
  installs; import and a position-changed commit leave, then install. So
  `suspend_play` goes with no flag in its place.
- Each transition publishes one board update, its final state. Entry
  (clone, then edit) and exit (edit, then live) each run under one lock
  with one publish. Today each half publishes (`enter_view_mode`,
  `enter_edit_mode`, the unchanged commit, then `republish_state`); the
  client's suppress flag existed for the race between those updates.
- `/view/start` keeps scrub-back as its only caller, which always
  suspends and lands on a past ply. So it always suspends, the ply is
  required (1 to one less than the move count, else 400), and the
  `suspend` parameter goes; the no-suspend branch and
  the default last-ply landing (which would now return to live at once)
  are dead.

### Exit

| Entered from            | Position  | Lands on                                                        |
|-------------------------|-----------|-----------------------------------------------------------------|
| play (either pencil)    | unchanged | live game, comments carried; paused on your turn, else as it was |
| scrubbed-back view (k)  | unchanged | the live clone at ply k                                         |
| either                  | changed   | live game left (saved if it has moves); new view at the FEN     |

"Unchanged" covers cancel and an unchanged commit alike, with or without
a comment. The exit applies the last-ply rule: a clone at its last ply
(edit from play) returns to live; a clone at k (edit from a scrubbed-back
view) stays, and its comments go home when you scrub forward. Play shows
no comments, as today, and a clone never rests on the last ply: a note on
the current position shows once a later move lets you scrub back to it.

"Changed" means piece placement, side to move or castling rights differ
from the entry position; en passant and the move counters are ignored.
The editor always writes neither, while today's EPD compare keeps a
legal en passant square: an unchanged commit right after such a double
push reads as changed (on a plain view today, the moves are dropped).
The server owns this rule, for plain views and live clones alike.

### Scrub forward: return on the server

Today the client detects a resumable view reaching its last ply
(`resumable`, `viewReachedNonLast`) and calls `/game/view/resume-play`.
The last-ply rule moves this to the server: a navigation op that moves a
live clone's cursor onto the last ply returns to live. That retires
`resumable`, `viewReachedNonLast`, `resumeLivePlay` and the resume-play
endpoint. The AI replay buffer needs no handling here: navigation is
refused while analysis runs, and the exits the client drives
(`/analysis/stop`, `/edit/start`) and a failed turn already clear it.
`resume_human_white` stays: a live clone's view payload must carry the
player's side so a remount mid scrub-back keeps the board POV.

### Play from here on a live clone

A live clone is always at a ply k before the end (scrub-back lands
there; reaching the last ply returns to live). Play from here first
captures the seed at k as today (moves, clocks, evals and comments up to
k, plus the game-over check), since Leaving's restore wipes the view.
Then the engine spawns, and at the commit point the live game is left
(saved to Recents) and the fork swaps in. The fork's parent link points at the saved live game's
`game_id` (the clone's, since they share it), so x-game navigation back
to the parent works.

### Fixes riding along

- **Paused on return:** the suspend snapshot records the pre-analysis
  mode, so a game paused for an AI turn comes back paused.
- **Comments in the crash-recovery snapshot:** `GameState` gains
  `play_comments` and `play_root_comment` (defaults, no schema bump);
  `restore_from` restores them. Fixes restart loss on its own, and the
  suspend snapshot carries them for free.
- **Fork link in the crash-recovery snapshot:** `GameState` also gains
  the parent game id and fork ply (defaults, no schema bump), restored
  by `restore_from`. Today a Play-from-here game keeps its link in
  memory only; after a restart its Recents save (now the main path, via
  Leaving) writes no parent link, and x-game navigation is gone.

### Confirms

Leaving a live game no longer loses it, but it still ends it (reopening
from Recents gives a view, not the running game with its clocks and
engine). So every path that leaves a live game asks one shared confirm,
no longer styled destructive, with a per-path verb. One gate everywhere,
matching the save rule: a live game with moves would be left. The
server works out `in_progress` once, the same way for GET /game/status
and for every board update: today it is false whenever a view is open,
so it must also count a live clone whose suspended game has moves. The
play page's confirms read a client copy computed from moves, game-over
and viewing (`_playInProgress`), which stays false on a clone; that copy
now reads the board update's flag and stops computing its own. A
zero-move game (New Game, then the pencil to set up a position) is never
saved, so it asks nothing, as today.

- New Game: "Start a new game? The current game will be saved to
  Recents."
- Import: "Import and leave the current game? It will be saved to
  Recents."
- Edit commit, additionally only when the position changed. The server
  decides: a commit that would leave a live game returns 409 unless it
  carries `leave: true`; the client confirms and resends. The 409 keeps
  edit mode (today the commit switches to view mode before deciding).
  One rule, on the server, and it survives a reload mid-edit: "Apply the
  new position and leave the current game? It will be saved to Recents."
- Play from here: "Start a new game from here? The current game will be
  saved to Recents."
- Tournament replay (`tournament-live-game.js`, already gated on the
  status call's `in_progress`): "Review this tournament game and leave
  the current one? It will be saved to Recents." Replaces its "Discard
  your in-progress game" wording and destructive style.
- Fork-link toast (`openXgameTarget`, reachable on a live clone of an
  exported Play-from-here game thanks to the shared id; today it imports
  the parent with no confirm): "Open the parent game and leave the
  current one? It will be saved to Recents."

On a live clone the gate is true, so New Game and import take their
in-progress branch (the leave confirm), not the discard-viewed-game
prompt a plain view gets. Import's replace-viewed prompt runs only when a
view is open and `in_progress` is false, so a clone never asks twice.

The pencil's discard confirm (`CONFIRM_EDIT_FROM_PLAY`) goes: edit no
longer leaves the game. Without the commit confirm, a stray drag plus
OK would end the running game silently, which is worse than today's
warning at entry.

### Tests

One per exit-table row, both pencils, and:
- the paused-on-return case from an AI turn;
- the leave confirm shows on a changed-position commit from a live clone,
  on Play from here on one, and on New Game from one; not on an unchanged
  commit, not on a plain view, and not on a zero-move game;
- tournament replay over a live game asks the leave confirm, not the
  discard one;
- the parent toast on a live clone asks the leave confirm before opening
  the parent; the live game lands in Recents with its fork link;
- `in_progress` is true on a live clone whose game has moves, false on a
  plain view and on a zero-move clone, in the status call and in the
  board update alike; New Game from a live clone is driven through the
  real UI (e2e), not only the endpoint;
- scrub forward after an edit at ply k carries the comment home;
- Play from here on a live clone saves the live game to Recents and the
  fork's parent link resolves to it;
- a live clone never appears in Recents; a commit on a clone of an
  exported live game leaves that row untouched, and the clone carries
  that row's hash, before and after the commit, so the dialog shows it
  as viewing;
- an unchanged commit right after a double push with a legal en passant
  capture stays unchanged: on play, the live game is kept with the note;
  on a plain view, the moves are kept;
- a commit that would leave a live game returns 409 without `leave` and
  stays in edit mode; after a reload mid-edit, the commit still asks;
- import from a live clone asks once;
- a failed engine spawn on New Game or Play from here leaves the live
  game or clone untouched, with nothing saved;
- the left game's Recents row keeps its fork link; on Play from here the
  parent row is not its own parent and the child keeps its link;
- entering edit from play and exiting it each publish one board update;
- edit from play on a zero-move game exits back to play (its clone at
  ply 0 is at its last ply);
- a comment commit on a plain view never inserts a row;
- importing the exported row of the game in progress returns 409 and
  leaves the game running, while playing and on a clone; the dialog shows
  that row disabled and tagged "playing" while playing;
- with PGN autosave on, the file of a left game carries its notes;
- `/view/start` rejects a missing ply, ply 0 and the last ply;
- the clone's board updates carry the live game's id; edit cancel and
  scrub forward land the client on the live board (no dropped update);
- New Game and import from a live game with moves save it to Recents
  (comments included, also when on a live clone); with no moves, nothing
  is saved;
- a restart mid-game keeps the live game's comments;
- a restart while on a live clone keeps a note committed on it, and so
  does a restart right after Return to live (same write, no second one);
- after a restart, a forked game's Recents save still links to its parent;
- a restart after leaving a live game does not resurrect it.

## Out of scope (v1)

- Undo / redo within edit mode.
- Bulk annotation operations (e.g. clear all).
- NAG editing.
- Editing the root comment via a separate affordance (cursor=0 already
  covers it through the same flow).
- Editing the live game in place (no clone): see Edit from play -- the
  clone protects the original while the position is edited.
- Opening book across a restart: `GameState` carries no book, so a
  restart mid-opening plays on without it. Same mechanism as the comment
  and fork-link fields, separate change.
- Engine swap mid-analysis: selecting another engine (`swap_engine`)
  leaves ANALYZING without stopping an AI turn or clearing its replay,
  so navigation reopens while the turn still streams. Pre-existing,
  separate fix.
