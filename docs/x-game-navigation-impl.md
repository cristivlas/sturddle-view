# X-Game Navigation — Implementation Plan & Status

Companion to `x-game-navigation.md` (spec). This doc tracks the
server-then-client implementation, current status, and any
course-corrections taken along the way.

Status legend: `todo` / `wip` / `done` / `blocked` / `revised`.

## Phase 1 — Server (recent_imports + API)

### 1.1 Schema extension (`recent_imports.py`)

Status: `done`

Add to each row in `index.json`:

- `parent_game_id`: str | absent.
- `fork_ply`: int | absent. Matches the entry in parent's `refs`.

Change shape of existing `refs` field:

- Was: `list[str]` (reserved, never populated).
- Now: `list[dict]` of `{"game_id": str, "fork_ply": int}`.

No backwards-compat migration (project policy). Existing rows have
`refs: []` so the shape change is invisible until first fork.

Module-level constants for keys (no string literals):

```python
REF_GAME_ID = "game_id"
REF_FORK_PLY = "fork_ply"
ROW_PARENT_GAME_ID = "parent_game_id"
ROW_FORK_PLY = "fork_ply"
ROW_REFS = "refs"
```

### 1.2 `save()` extension

Status: `done`

Signature:

```python
async def save(fmt, text, summary, game_id=None, precomputed_hash=None,
               parent_game_id=None, fork_ply=None) -> str
```

Rules:

- `parent_game_id` and `fork_ply` must be supplied together (assert).
- When parent is supplied, look up parent row by id, append
  `{REF_GAME_ID: child_id, REF_FORK_PLY: fork_ply}` to its `refs`.
- Pair-write parent + child in a single `_persist()` under existing
  `self._lock`.
- Info-log `xgame.fork_created parent=<id> child=<id> ply=<n>`.

### 1.3 `remove()` extension

Status: `done`

Rules:

- If row has non-empty `refs`: do NOT delete. Return sentinel
  `RemoveResult.BLOCKED_BY_REFS` carrying the live children list.
  Info-log `xgame.delete_blocked`.
- If row has `parent_game_id`: look up parent, drop the matching ref
  entry, persist both changes atomically.
- If parent lookup fails (dangling): warn-log
  `xgame.dangling_parent`, proceed with child deletion.
- Info-log `xgame.child_deleted` on successful removal of a child.

### 1.4 `children_of(game_id)` helper

Status: `done`

```python
def children_of(self, game_id: str) -> list[dict]:
    """Returns [{game_id, fork_ply, summary, ts}, ...] for live children."""
```

- Reads parent's `refs`, resolves each child row by id.
- Dangling entries are dropped from the returned list and warn-logged.
  Lazy scrub of the parent's `refs` is acceptable but not required at
  this call site.

### 1.5 `scrub_dangling_parent(game_id)` helper

Status: `done`

- Called lazily on `get_by_id` of a child whose `parent_game_id` does
  not resolve.
- Drops `parent_game_id` and `fork_ply` from the child row, persists,
  warn-logs.

### 1.6 Eviction sanity check

Status: `done`

Existing eviction logic skips rows with non-empty `refs`. Confirm the
dict-shape change does not break the truthiness check; add a regression
test.

### 1.7 Fork-link capture (lazy commit on finalization)

Status: `done`

`POST /game/view/play-from-here` does NOT save the child to recents.
That stays unchanged.

Instead, at fork time the server stashes the prospective fork link on
the HVE session without writing to recents:

- Inside `play_from_here`, before `_reset_view_state()`, capture
  `(self._game_id, self._view_cursor)` -- the parent's game_id and the
  fork ply.
- Store on a new HVE attribute, e.g.
  `self._fork_link: tuple[str, int] | None`.
- If cursor (= fork_ply) is 0: do not stash; treat as a plain new
  game.

The stashed link is consumed only when the child play game itself
becomes a saved/viewed game via one of the paths that *already* write
to recents today:

- `_flush_recents_save` -- finalization (mate / stalemate / draw /
  resign / time-forfeit). Picks up the stash and passes
  `parent_game_id` + `fork_ply` to `recents.save`.
- `commit_edit` -- edit-commit (FEN or annotation-only). Likewise
  picks up the stash and forwards to `recents.save` /
  `recents.replace_at`.

Stash is cleared in all of: consumption, plain `new_game`, abandonment
via another import-on-top, and `play_from_here` resetting it from the
new parent's perspective (i.e. fork-of-fork chains correctly).

NOT consumed (link drops on the floor by design):

- `/game/view/start` -- transient state-flip used only as a step into
  edit mode; today no recents write, leave alone.
- `/game/import` on top of an active play game (user discards).
- `play_from_here` -- the new fork resets the stash to point at the
  NEW parent, not the old one (sibling forks share the same parent).
- Edit-cancel -- no commit, no save.

Rationale: a fork is "lazily committed" only when the child itself is
durable. A play game that the user abandons mid-stream leaves no fork
row -- matching today's behavior that abandoned play games leave no
recents trace.

Constants for the stash carrier go alongside the existing
ROW_* / REF_* set in `recent_imports.py` if they need module-level
visibility; otherwise the HVE attribute is self-contained.

### 1.8 API: `DELETE /game/recent-imports/{hash}`

Status: `done`

- If remove returns BLOCKED_BY_REFS: respond 409 with body:
  ```json
  {
    "error": "has_children",
    "children": [{"game_id": "...", "fork_ply": N, "summary": {...}}, ...]
  }
  ```
- Otherwise 200 as today.

### 1.9 API: `GET /game/recent-imports/by-id/{game_id}`

Status: `done` (and the same extension applied to the by-hash route
for symmetry)

Extend response with:

- `parent_game_id`, `fork_ply` (if set on this row).
- `children`: result of `children_of(game_id)`.

Lets the client render both nav directions without extra round-trips.

### 1.10 Logging

Status: `done`

Named logger, info level, structured fields:

- `xgame.fork_created parent=<id> child=<id> ply=<n>`
- `xgame.child_deleted parent=<id> child=<id> ply=<n>`
- `xgame.delete_blocked hash=<h> game_id=<id> refs=<n>`
- `xgame.dangling_parent child=<id> missing_parent=<id>`

### 1.11 Tests

Status: `done` (Phase 1).

Pinned in `tests/test_recent_imports.py`:

- save-with-parent populates both rows; refs is dict-shaped.
- remove-with-live-refs blocked; refs untouched.
- remove-of-child removes parent's ref entry atomically.
- dangling parent_game_id scrubbed; warning logged.
- evict skips rows with non-empty refs (regression on dict-shape).
- replace_at preserves parent_game_id/fork_ply on child edits.
- replace_at preserves refs on parent edits.
- replace_at explicit fork-link promotes an unsaved child into recents.
- replace_at explicit link does NOT double-append parent's refs.
- fork_ply must be supplied with parent_game_id (and vice versa).
- fork_ply == 0 rejected.

Pinned in `tests/test_recents_play_save.py`:

- play_from_here at ply > 0 stashes (parent_id, fork_ply); resign
  finalizes and writes the fork row.
- play_from_here at ply 0 does NOT stash; subsequent finalization
  writes a plain non-fork row.
- plain new_game clears a stale stash.
- enter_view_mode (default) drops the stash.
- enter_view_mode (fork_link= passed) preserves the stash.

Pinned in `tests/test_recent_imports_api.py`:

- GET /game/recent-imports/by-id includes parent_game_id, fork_ply,
  children for both parent and child rows.
- DELETE returns 409 with `{"error": "has_children", "children": [...]}`
  when the row is pinned.
- DELETE of the child unpins the parent.

## Phase 2 — Client (view ribbon + move list)

Status: `todo` (gated on Phase 1 + remaining open questions Q1/Q2/Q4/Q5
from the spec doc).

Sketches to be added when Phase 1 lands.

## Course corrections

- 2026-05-22: Plan originally had `play-from-here` save the child to
  recents immediately. Corrected: do not save at fork time. Stash
  `(parent_game_id, fork_ply)` on the new play session and write the
  fork row only when the active game later transitions into view via
  existing paths. Rationale: a play game that never becomes "viewed"
  should not pollute recents, matching today's behavior.
- 2026-05-22: Code review showed `/view/start` is purely transient
  (only used as a step into edit mode -- no standalone "view my live
  play game" UX). Removed it from the list of consuming transitions.
  Final list: `_flush_recents_save` (mate/resign/timeout) and
  `commit_edit` (edit-mode commit). Edit-cancel and import-on-top
  drop the stash by design.
