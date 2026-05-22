# X-Game Navigation — Design Sketch

Status: design, not implemented. Tracks decisions across chat sessions.

## Problem

"Play from here" forks a viewed game into a new play game. The fork
relationship is currently lost: the child has no link back to the parent,
and the parent has no awareness of its children. Users cannot navigate the
tree of related games.

We want to:
- Persist parent/child relationships on top of the existing
  `recent_imports` infra (see `recent-imports.md`).
- Surface the relationships during normal scrub navigation in view mode,
  so the user can step between parent and child games at the fork point.

## Model

- Tree. Parent has 0..N children. Child has 0..1 parent.
- Each child stores: `parent_game_id`, `fork_ply` (ply in parent at which
  the fork was taken). Both immutable for the lifetime of the child row.
- Fork at parent ply 0 is degenerate and is treated as a plain new game,
  not a fork. No parent link is recorded.
- Forks-of-forks (grandchildren) are allowed implicitly: any child can
  itself be a parent.
- Persisted via `recent_imports` so the tree survives reloads and is
  shared across browser profiles on the same install.

## Storage

Extends the existing `index.json` row (no migration; new optional fields):

- `parent_game_id`: opaque game_id of the parent, or absent.
- `fork_ply`: int >= 1, ply in parent at which fork was taken, or absent.

Population of the existing reserved `refs` field:

- On fork creation, append the child's `game_id` to the parent row's
  `refs`. Pins the parent row against eviction (eviction logic already
  honors non-empty `refs`).
- On child deletion, remove the child's id from the parent's `refs`.
- Orphaning policy (when parent is deleted with surviving children): TBD,
  see Open Questions.

## Navigation triggers

All navigation prompts fire only on **precise single-step landing** on the
trigger ply. "Jump to first" and "jump to last" that pass through a fork
ply do not trigger; the user must land on it precisely. A direct
move-list click counts as a precise landing.

### Child -> parent

- Trigger: current game has `parent_game_id` AND view cursor === 0.
- Reached via: one step back from ply 1, "jump to first", or
  move-list click on ply 0.
- Affordance: banner near view ribbon, e.g.
  "This game is a variation of `<parent summary>` at ply `<fork_ply>`.
  Open parent?"

### Parent -> child

- Trigger: current ply is a fork ply of one or more children.
- Reached via: single step forward or backward landing on the fork ply,
  or move-list click on the fork ply.
- Affordance: same area, e.g.
  "There is/are `<N>` variation(s) of this game from this position.
  Open ...?"
  - N = 1: direct link.
  - N > 1: list of children with short summaries.

### Inner node (child that is itself a parent)

If the cursor lands on ply 0 of a child AND ply 0 also happens to be a
fork ply for that same game's own children, both prompts are shown
stacked. Rare but possible.

## UX details

- Desktop only for v1. No mobile affordances.
- Direction-agnostic: same trigger for back-step and forward-step
  arrivals.
- Fork-ply glyphs in the move list (parent view): mark plies that have
  children. Click on the glyph or the move cell both land precisely on
  the ply and fire the trigger.
- "Open parent" / "Open child" performs the equivalent of selecting that
  game from recents: server loads the row, enters view mode, returns the
  child/parent at the appropriate cursor position (TBD: open at fork ply
  vs. ply 0 vs. last-visited; see Open Questions).

## Server changes (sketch)

- `recent_imports`: extend row schema with optional `parent_game_id`,
  `fork_ply`. Populate `refs` on the parent at fork-creation.
- `/game/view/play-from-here`: after creating the child play game, save
  it to recents with `parent_game_id` and `fork_ply` set. (Today it
  does not auto-save to recents.)
- New endpoint(s) likely needed:
  - List children of a game_id, or include them in the existing
    `GET /game/recent-imports/by-id/{game_id}` response.
  - Open parent/child by game_id and enter view mode (may reuse the
    existing by-id fetch path).

## Client changes (sketch)

- View ribbon: render parent/child banner when triggers fire.
- Move list: render fork-ply glyph in parent games.
- Trigger logic: react to cursor transitions; distinguish single-step
  arrival from jump-through.

## Resolved decisions

### Refs as ref-count + parent-side fork ply

`refs` on the parent row becomes a list of dicts, not a flat list of
ids:

```
refs: [{ "game_id": "<child_uuid>", "fork_ply": <int> }, ...]
```

- Non-empty `refs` pins the parent: blocks LRU eviction AND blocks
  DELETE.
- DELETE against a pinned row returns a non-200 status (e.g. 409) with
  an informational body listing the live children. No deletion occurs.
- The 50-entry cap becomes a soft limit; pinned rows can push the
  effective count above it.
- Storing `fork_ply` on the parent side as well means "children at ply
  X" rendering in the move list does not require N child fetches.

### Child-side parent pointer

Child row carries `parent_game_id` and `fork_ply` (same value as the
matching entry in the parent's `refs`; duplication is intentional so
either direction is cheap).

### Orphan / dangling handling

- Should not occur in normal operation (refs blocks deletion).
- If it occurs (file corruption, manual edit): soft fail. Log a warning.
  Optionally scrub the dangling `parent_game_id` from the child on next
  touch.
- No cascading deletes in either direction.
- Circular refs are not structurally possible: a child has at most one
  parent, set at fork creation, immutable thereafter.

### Atomicity

- Fork creation updates the parent (`refs.append`) AND writes the child
  row as a single atomic index save.
- Child delete removes the child's entry from the parent's `refs` AND
  removes the child row as a single atomic index save.
- Reuse the existing lock + atomic JSON replace path; extend to cover
  pair-writes.

### Logging

Info-log every relationship change, server-side:

- Fork creation (parent ref appended).
- Child delete (parent ref removed).
- DELETE blocked because refs non-empty.
- Dangling parent_game_id detected / scrubbed.

## Open questions

- Q1: When opening a child from parent: open at **child ply 0**
  (the fork FEN, showing the divergence point). Resolved.
- Q2: When opening a parent from child: open at the parent's
  **fork_ply** (the ply at which the child diverged). Resolved.
- Q4: Visual style of the fork-ply glyph. Picking a glyph that does not
  collide with existing move-list visual language.
- Q5: Should the parent->child trigger remember "user already declined
  this prompt" so it does not nag on every back-and-forth pass over the
  fork ply within a session?

## Out of scope (v1)

- Mobile UI.
- Reparenting / moving subtrees.
- Merging or diffing siblings.
- A dedicated tree view of related games.
