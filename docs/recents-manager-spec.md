# Recents Manager - Spec

Design for managing the recent-imports history: a tree view of recents
in the Settings dialog, reachable from the import dialog's Recent
dropdown. Decisions captured from brainstorm (branch `feat/recents`).

## Problem

A recents row with forked children (non-empty `refs`) cannot be deleted.
Today the dropdown offers a trash button anyway; clicking it removes the
row optimistically, the server answers 409 `has_children`, the row comes
back, and a toast says "Cannot delete: this game has N forked
variations." The user has no way to act on that:

- The dropdown shows only the 10 newest rows, so the forks are often
  out of sight.
- Nothing marks which rows are the forks.

## Already shipped on this branch

- Eviction now detaches an evicted child from its parent's `refs`
  (shared `_drop_row_locked` with `remove`), and rescans so a parent
  unpinned by its child's eviction drops in the same pass.
- `scripts/chkrefs.py` lists dead refs left by the old eviction bug;
  `--apply` removes them (server stopped). Run by hand; no auto-clean
  at load, so future ref bugs stay visible.

## Overview

1. **Server:** cascade delete; `/game/status` reports the in-play game id.
2. **Import dropdown:** rows with forks show a gear instead of the trash
   button; the gear deep-links to the new Settings tab.
3. **Settings "Recents" tab:** tree view with per-row Open and Delete.
4. **Open from the tab:** reuses the tournament Review flow.

## 1. Server

### Cascade delete

- `RecentImports.remove(h, cascade=False)`.
  - `cascade=False`: unchanged (409 `has_children` when refs non-empty).
  - `cascade=True`: collect the row and all descendants through `refs`
    (resolving ids via the reverse index; dead refs skipped), drop them
    deepest first via `_drop_row_locked`, single `_persist_locked`.
- Guard: if any row in the subtree is the active session's game
  (`active_game_id` getter: the in-play game, or the viewed one),
  refuse; nothing is deleted.
- Route: `DELETE /game/recent-imports/{hash}?cascade=1`. Refusal maps to
  409 with an error code distinct from `has_children`.
- `docs/x-game-navigation.md` says "No cascading deletes in either
  direction"; update it to describe the explicit, confirmed cascade.

### Status: in-play game id

- `GET /game/status` gains `play_game_id`: the in-progress game's
  `game_id` (null when no game is in progress). `view_hash` already identifies the viewed
  game. The Settings tab reads both to mark the current rows.

## 2. Import dropdown

File: `web/app/import-position-dialog.js`.

- Data: `GET /recent-imports` already returns each row's `refs`. No new
  endpoint.
- Cache: `saveRecentsCache` keeps a fork count per row (one number).
  Entries cached before this change have no count and render as
  deletable until the server refresh redraws them; a stale count is
  corrected the same way.
- A row with forks shows a gear (the `gear` icon already used by the
  Settings deep-link toast actions) instead of the trash button.
  Tooltip: "Has N forks; manage in Settings".
- Gear click:
  1. If the user typed or pasted text in the dialog, confirm before
     discarding it. No text: no prompt.
  2. Close the import dialog.
  3. `openSettings(SETTINGS_TAB_RECENTS, ...)` carrying the row's hash.
- The 409 `has_children` toast stays as a fallback for a stale list.
- The viewing / playing tags are unchanged.

## 3. Settings "Recents" tab

New builder `web/app/settings-recents-tab.js`, wired into
`settings-dialog.js` like the other tabs.

### Layout

- The dialog never scrolls; the tree area scrolls inside the tab (same
  model as the Engines tab).
- Mounts on first show (like Engines), fetching `GET /recent-imports`
  and `GET /game/status`. Settings is rebuilt on every open, so each
  open starts fresh.
- While open, `APP_EVT.RECENTS_CHANGED` refetches both and redraws,
  keeping the selected row (by hash) and the expanded rows. If the
  selected row is gone, nothing is selected.
- A game can finish while Settings is open (the server saves it to
  recents). play.js dispatches `RECENTS_CHANGED` on `game_result`;
  recents is flushed before that event fires.

```
v Carlsen - Nepo, 1-0          Oct 9   [open][del]
    > Me vs Engine (move 12)   Oct 10  [open][del]
        Me vs Engine (move 20) Oct 11  [open][del]
  Kasparov - Topalov           Oct 3   viewing
```

- Tree built client-side from `parent_game_id` / `refs`. Roots and
  siblings sorted by `ts` desc. A row whose `parent_game_id` doesn't
  resolve to a listed row renders as a root.
- Indentation shows nesting; arrows expand and collapse. The tree starts
  collapsed. A selection hidden under a collapsed parent is kept.
- The tab sits right below Gameplay.
- Fork rows show the move they branched at.
- The viewed or in-play game's row shows a tag ("viewing" / "playing")
  instead of buttons, as in the dropdown.

### Actions

- The tab always sends `cascade=1`. On a leaf that is a plain delete;
  it also covers a parent whose only refs are dead (the tree shows it
  as a leaf, and a non-cascade delete would 409).
- **Delete** on a leaf: no confirm beyond the button.
- **Delete** on a parent: confirm "Delete with N forks?", where N is
  the size of the whole subtree below the row (grandchildren count).
  A subtree containing the viewed or in-play game cannot be cascaded:
  the button is disabled with a tooltip saying why. The server refuses
  both cases too, as a backstop for a stale tree.
- **Open**: see section 4.
- After a delete, dispatch `APP_EVT.RECENTS_CHANGED` (as the dropdown
  does) so Play refreshes its x-game state. The tab does not remove rows
  locally; its own `RECENTS_CHANGED` refetch redraws the tree.

### Deep link

- `openSettings` / `APP_EVT.OPEN_SETTINGS` gain a `select` field next to
  `focus`, carrying a recents hash.
- On mount with `select`: expand the row's ancestors and the row itself
  (the gear exists to show its forks), select the row,
  scroll it into view. If the hash isn't in the fetched list, nothing
  is selected and no error is shown.

## 4. Open from the tab

Same flow as the tournament Review button
(`replayTournamentGame` in `web/app/tournament-live-game.js`), which
runs outside play.js:

1. `GET /game/status`.
2. In progress: confirm leaving the game. Viewing a different game:
   `confirmReplaceViewedGame`. Same game already viewed: skip the import.
3. Fetch the row's text, `POST /game/import`.
4. Close Settings, dispatch `APP_EVT.ACTIVATE_PERSPECTIVE` for Play with
   `remount`: a Play already on screen drops board updates for a game id
   it didn't switch to itself, so after an import it remounts and resyncs
   from the server. Review sends `remount` too.

Factor the shared steps out of `replayTournamentGame` rather than
copying them.

## Tests

- Server unit: cascade on leaf, subtree, grandchildren; dead refs in a
  subtree; a row whose only refs are dead; active-game guard refuses and deletes nothing; status
  carries the in-play game id.
- Playwright e2e (seed via the `/_test` hooks):
  - Dropdown: forked row shows the gear, not the trash button.
  - Gear: confirm only with typed text; lands on the Recents tab with
    the row selected.
  - Tree: nesting, fork move, viewing/playing tags.
  - Game ends while Settings is open: the new row appears and the
    selection survives the refresh.
  - Cascade delete with confirm; disabled when the subtree holds the
    current game.
  - Open: confirms and lands in Play on the picked game.

## Tabled

- Preview pane (mini board + headers) for the selected row. On a phone
  there is no room beside the tree, and below it the preview squeezes
  the list. Try later.
