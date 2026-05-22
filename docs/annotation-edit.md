# Annotation Edit -- Design Sketch

Status: implemented (phases 1-7 shipped).

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
    text=None deletes. Recomputes _view_hash and _view_raw_text."""
```

On commit:
1. Update the in-memory comment array (or root).
2. Regenerate `_view_raw_text` via `build_pgn(...)` from the live
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

"Comment change" is detected by comparing the regenerated PGN's hash
against `_view_hash`. The FEN-change branch keeps its existing behavior
(history dropped, FEN-only row written) and supersedes any in-flight
comment edit -- the truncated game is a new game by identity.

### Promote-to-recents on annotation edit

A game can be in view mode without being in recents (e.g. play -> view
via `/game/view/start` -> edit -> annotate). On annotation commit:

- If `recents.hash_for_id(game_id)` returns a hash -> `replace_at` it.
- Otherwise -> `replace_at(old_hash=None, ...)`, which inserts.

So the act of annotating *is* the act of promoting the game to recents.
This matches the semantic: the user cares enough to add content, so the
game is worth preserving for future recall.

## Client mechanics

The annotation-edit ribbon button POSTs the new endpoint (e.g.
`/game/edit/annotate {ply, text}` or folded into `/edit/commit` with a
new optional field -- TBD during implementation).

Cache refresh on response:
- Update `_viewingHash` to the new hash from the response.
- Refetch `/game/recent-imports` to rebuild the dropdown cache. Simple
  and authoritative; can be revisited if it shows up as a perf issue.

## `_view_raw_text`: freeze as the import artifact

Today `_view_raw_text` is informally "the imported PGN bytes." There's
no mechanism preventing a future code path from reassigning it, and the
field name implies "live raw text" -- which it isn't.

Going forward, treat it as a write-once import artifact:

- Set exactly once in `enter_view_mode` from `params.view_raw_text`.
- Declare with `Final` and document loudly that it is the **imported
  fossil**, never to be updated.
- No mutation code path (annotation edit, anything future) touches it.

Export decision becomes: if structured state still matches the import
(no edits since), returning `_view_raw_text` is an optimization that
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
  `self._view_raw_text` (the original imported bytes) when present,
  bypassing `build_pgn` entirely. So comments survive that path purely
  by virtue of the cached raw text, not because `build_pgn` handles
  them.

- The two paths that DO go through `build_pgn` are:
  - `_build_play_game_pgn` -- live play game serialization
    (autosave / recents-on-end / download). No comments today (live
    play has no comment storage either, see below).
  - The view-mode regeneration branch in `get_pgn_text` (post line
    1766) when `_view_raw_text` is None -- can happen after
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
    the non-verbatim path (force `_view_raw_text=None` to simulate
    post-annotation-edit state), expect comments preserved.

## Out of scope (v1)

- Undo / redo within edit mode.
- Bulk annotation operations (e.g. clear all).
- NAG editing.
- Editing the root comment via a separate affordance (cursor=0 already
  covers it through the same flow).
- Comment editing during live play (would require live-game PGN
  autosave hooks; view-mode-only for v1).
