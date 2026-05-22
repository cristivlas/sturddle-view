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

- Trigger: current game has `parent_game_id` AND view cursor ===
  `fork_ply` (revised 2026-05-22; see Q1 note above).
- Reached via: precise single-step landing on the fork ply (forward,
  back, or move-list click).
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

If the cursor lands on the child's `fork_ply` (so the parent-link
fires) AND that same ply happens to be a `fork_ply` for one or more of
this game's own children, both prompts are shown stacked. (Both
triggers now share the same ply condition, so this is the natural
overlap case.)

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

## Resolved (originally open) questions

- Q1 (revised 2026-05-22 after impl discovery): When opening a child
  from parent: open at the child's **fork_ply**. Reason: ``play_from_here``
  builds the child by inheriting plies 0..cursor from the parent, then
  appending the user's new moves. So plies 0..fork_ply are identical in
  parent and child; the divergence appears at plies > fork_ply. Landing
  the child at fork_ply puts the user exactly on the shared boundary,
  symmetric to Q2.
- Q2: When opening a parent from child: open at the parent's
  **fork_ply** (the ply at which the child diverged).
- Q4: Fork-ply glyph rendered via the existing `<wa-icon>` element,
  `name="code-fork"`. Sits inline next to the move SAN. For plies
  with multiple children, a small count follows the icon.
- Q5: The parent->child banner is dismissible per game (single boolean
  state). Once the user dismisses, the banner stays hidden for the
  remainder of the time that game is open. The move-list fork glyph
  remains the manual reopen path -- clicking the glyph re-shows the
  banner anchored at that ply.

## Out of scope (v1)

- Mobile UI.
- Reparenting / moving subtrees.
- Merging or diffing siblings.
- A dedicated tree view of related games.
