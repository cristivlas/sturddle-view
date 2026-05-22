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

### 1.7 Fork-link capture (not in play-from-here)

Status: `todo`

`POST /game/view/play-from-here` does NOT save the child to recents.
That stays unchanged.

Instead, at fork time the server records the prospective fork link
**without writing to recents yet**:

- Capture parent `game_id` and the cursor ply at the moment
  play-from-here is invoked.
- Stash `(parent_game_id, fork_ply)` on the new play-mode session
  state.
- If `fork_ply == 0`: do not stash; treat as a plain new game.

The stashed link is consumed later, when the active game transitions
into a viewed game via one of the existing paths (game-over auto-view,
save-pgn-then-view, etc. — exact set TBD during impl). At that
transition, `recents.save(...)` is called with `parent_game_id` and
`fork_ply` set.

Implication: a play game that never transitions to view never appears
in recents and never establishes the link. This matches today's
behavior for the play side of the app.

Open during impl:

- Exact list of "active -> viewed" transitions that should consume the
  stashed link.
- Where the stash lives (session object on the play game?). Constants
  for keys to be defined alongside the existing ROW_* / REF_* set.

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

Status: `todo`

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

Status: `wip` (recent_imports tests done; play-from-here + transition
tests pending the impl in 1.7).

Pinned:

- save-with-parent populates both rows; refs is dict-shaped.
- remove-with-live-refs blocked; refs untouched.
- remove-of-child removes parent's ref entry atomically.
- dangling parent_game_id scrubbed; warning logged.
- evict skips rows with non-empty refs (regression on dict-shape).
- replace_at preserves parent_game_id/fork_ply on child edits.
- replace_at preserves refs on parent edits.
- fork_ply must be supplied with parent_game_id (and vice versa).
- fork_ply == 0 rejected.

Still to pin (after 1.7):

- play-from-here at ply > 0 stashes (parent_id, fork_ply) on the new
  play game; recents NOT touched at this point.
- play-from-here at ply 0 does NOT stash; subsequent transition to
  view writes a plain (non-fork) row.
- active -> view transition consumes the stash and writes a fork row
  with parent_game_id + fork_ply; parent's refs is appended atomically.
- A play game that never transitions to view leaves no fork row and
  does not append to the parent's refs.

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
