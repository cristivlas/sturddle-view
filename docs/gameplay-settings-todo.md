# Gameplay settings + view enrichment -- TODO

Standalone scratchpad for related items that surfaced during PGN
reconciliation work but are out of scope for that branch. Delete this
file once all items ship.

## 1. Auto-claim draws

Play / view-mode currently does not auto-claim 3-fold repetition or
50-move draws. Effect: a tournament game that fastchess called as
3-fold draws fine in fastchess, but `play-from-here` from the same
position lets the engine play on (claimable, not automatic).

**Setting:** Gameplay > "Auto-claim draws" toggle (3-fold + 50-move).
Default on. Applies to any human-vs-engine game, including
play-from-here from imported PGNs.

**Implementation hook:** wherever `human_vs_engine` checks game-over,
pass `claim_draw=True` to `chess.Board.outcome` /
`is_game_over` when the setting is enabled.

## 2. play-from-here TC selection

Tournament TCs (e.g. 120+2) are unplayable for humans. Currently
`play-from-here` uses the engine's configured TC; we never surface a
choice.

**Setting:** Gameplay > "When importing a game with a time control"
> radio:
- Use the imported game's TC (and remaining clocks if present)
- Use my Gameplay default TC

Applies to any PGN import that carries `[TimeControl]` / clock
annotations. Independent of #3 below; if "no TC" is selected as the
Gameplay default and the user picks "use my default," result is
no-clock play.

## 3. No-TC / casual mode

Gameplay TC selector currently always picks a real TC. Add a "No time
control" option for casual play.

**UI:** TC dropdown gains a "None (casual)" entry. When selected:
- No clock display in the play UI (or clocks show "--").
- Engine doesn't get `wtime/btime`; goes infinite or to a configured
  movetime (decide during impl).
- Resign/draw still available.

Interacts with #2: choosing "No TC" as the Gameplay default makes
"use my default" import-mode produce no-clock games regardless of the
imported game's TC.

## 4. View-mode eval surface

The PGN parser captures `[%clk ...]` / move-time comments but not the
engine evals embedded by fastchess (`{+0.49/16 5.705s}` style). View
mode shows clocks but no eval row.

**Work:** extend the PGN comment parser to recognize fastchess's
`{cp/depth time}` shorthand; pipe through to the view-mode UI as the
same eval row that lives during a real game.

**Source:** the `pgn_tail.py` parser (or a sibling) already walks the
movetext; fastchess's eval comments appear adjacent to `[%clk ...]`
in the same `{ ... }` block, so this is a comment-tokenizer extension,
not a new parse pass.

Out of scope: rewriting the PGN format. Read-only enrichment.

## 5. Single-game engine-vs-engine mode

Today the only way to watch two engines play is to spin up a full
tournament (one round, parallelism 1). Heavy for a "just show me one
game" workflow. A casual single-game mode would let the user pick
two engines, a TC (or no TC -- see #3), and watch one game in the
existing Live game window without standings/SPRT/Schedule overhead.

**Likely shape:** sits on top of the existing tournament machinery
(reuse fastchess runner + proxy + reconciliation), but exposed as a
distinct UI entry point with a stripped-down workspace -- only the
Live window opens, no Standings / no Schedule / no Event log
(optional). On done, offer Replay (already in slice 4) and a "rerun
same matchup" button.

**Open questions to resolve at design time:**
- Is this a new perspective, a Play-perspective sub-mode, or a
  workspace variant of Tournaments?
- Persistence: do single games go to the per-tournament PGN dir, or
  a separate single-games archive?
- Does it count toward standings of any prior tournament? (No.)
- TC source: Gameplay default? Per-launch picker? Both?

Out of scope of this TODO -- spec and design pending.
