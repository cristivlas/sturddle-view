# Play a search line on the board

## Goal

See an engine line unfold on the board, then get the board back.
Play perspective, Search Lines window, play and view modes. Client
only: everything needed is already in the Search Lines table.

## Model

A row is a position plus a line. Playing a row shows that position,
plays the moves, and returns the board to what it showed before.

A row's `{ placement, pv_uci, frames }` is written as one unit, and
only by an info that carries `pv_uci` (`pvFrames` is pure and
cheap). Infos without a PV update depth, score, nodes and nps only,
so the triple never disagrees with itself. A row with fewer than two
frames has nothing to show: no hover, no tooltip, double-click
inert. The affordance never lies.

The show borrows the board and knows nothing about who else drives
it. At activation it records the outside state: position and its
last move, input enabled or not, and the arrows on the board (the
`{ type, from, to }` triples `getArrows()` returns, so an arrow
drawn before the show survives it). Of the outside drives, only a
position set ends the show, and it does so before it applies. Input
requests, arrow requests and arrow clears from outside during the
show update the record; the record is applied when the show's last
queue entry resolves (Record application, under Cancel). A queue
reset from outside is not a drive: the show continues on the fresh
queue.

Two abort signals, both promises:

- Generation: bumped by every show, retarget, cancel and destroy;
  its promise resolves on bump. Every await in the show loop races
  it. A stale generation exits the loop at once, so an old
  continuation can never run after a newer show started or after
  the board is gone.
- Queue epoch: bumped by every queue reset; its promise resolves on
  bump. Only awaits on a queue entry race it; a pause is not a queue
  entry and runs on. The entry the show was waiting on (frame,
  return or snap) either settled because the reset completed it in
  place, or never will because it was queued behind a foreign entry
  on the dead queue. The show re-issues that same entry on the fresh
  queue either way and carries on; a re-issue the reset already
  landed is a same-FEN no-op. A pending snap that an outside position
  set has retired is not re-issued (Record application). Nothing is
  lost; nothing stays locked.

## Trigger

Double-click a showable row. Showable rows get a hover highlight and
the tooltip "Double-click to play line"; other rows get neither. The
second press of a double-click is `preventDefault`ed so no SAN token
gets word-selected.

Refused while the board is in edit mode.

## Show

Activation snapshots the row: placement, `pv_uci`, text, frames.
Frames are [searched position, after ply 1, after ply 2, ...].

- Input is locked at activation. The recorded input state is applied
  only after the return animation has resolved.
- Arrows are hidden at activation.
- Frame 0 is skipped when it equals what the board shows (no dead
  beat). Otherwise the board animates to the searched position.
- Every other frame, in order: highlight its move in the row, start
  the animation, mark the move just played with the from/to frame
  markers, then pause `PV_PLAY_PLY_PAUSE_MS`. The last frame holds
  `PV_PLAY_END_HOLD_MS` instead.
- At most one show entry sits in the animation queue at any time:
  the show awaits each frame before enqueuing the next, and a snap or
  a retarget's frame 0 is the single entry queued behind the in-flight
  animation. Non-animated changes go through the queue too (the
  board.js snap-redraw patch enqueues them), so "behind" holds.
- Return: animate back to the recorded position; when that entry
  resolves, re-mark its last move, draw the recorded arrows, apply
  the recorded input state. Nothing of the outside state is drawn
  while a PV frame is still on screen.
- One show, no loop.

## Feedback

The playing row is highlighted. The move being shown is highlighted
inside it when its animation starts, and scrolled into view
(`{ inline: "nearest", block: "nearest" }`), as the move list does.
`release()` clears both highlights, row and move, so an ended show
of any kind leaves nothing lit.

While a row plays, infos for its depth that carry no PV are dropped
(the row is frozen whole). A conflict is a same-depth info that
carries `pv_uci`, or a table clear. One mechanism for both: the row
is unkeyed from the depth map and pinned in place; the incoming PV
info (if any) creates a fresh row at that depth; the pinned row is
removed on release. The table stays truthful (the superseded line
never outlives the show) and the show's text never diverges from its
frames.

Retarget on the same row: the old show's `release()` skips the
removal when the new show holds the same handle, so the line never
plays without its text.

Three CSS additions, all under `.wb-pvtable-tbl`: showable-row
hover, playing-row highlight, current-move token highlight. No
existing rule changes.

## Cancel

- Esc, or a pointerdown outside the Search Lines window, captured
  before anything else handles it. A press on the board only
  cancels: cm-chessboard picks pieces up on `mousedown`
  (ChessboardView.js:69), which fires after our `pointerdown`
  capture and always finds input still locked.
- A pointerdown inside the Search Lines window's current chrome
  (its WinBox root, dock slot, or inline slot, decided per event from
  the instance's live getters) never cancels: scrolling, column
  grips, moving or undocking the window all keep the show going.
- Double-click on another showable row (or the same) during a show
  retargets: the generation bumps (the old loop exits at its next
  await), the new show inherits the recorded outside state, and its
  frame 0 animates from the current frame. Reads as a rewind, no
  jump.
- A position set from outside (live move, ply jump, new game) cancels
  before it applies, so there is exactly one flight to the new
  position. From its notification on it owns the board's visuals;
  the record then carries only the input state (Record application).
- Closing the Search Lines window cancels (`dispose` calls
  `cancelLine`). Leaving the perspective detaches the body without
  disposing it (`closeForNav`); the board's `destroy` ends the show
  there and fires `onEnd`, so the revivable body holds no lit row.
- Cancel snaps through the queue: the generation bumps, a
  non-animated change to the recorded position is enqueued behind the
  in-flight animation. When that snap entry resolves: last move
  re-marked, recorded arrows drawn, recorded input state applied --
  never while a PV frame is still on screen. For a board press the
  input state is applied at the later of the snap resolving and that
  gesture's pointerup, pointercancel, or window blur, so the same
  press cannot pick up a piece and a lost pointerup cannot leave the
  board locked. Only the natural end animates.

Record application, every path:

- The record stays live from activation until it is applied. Outside
  input requests, arrow requests and arrow clears in that window
  update it, so the apply can never overwrite a later outside request
  (a `GAME_RESULT` `enableInput(false)` landing while the snap is
  still behind the in-flight frame, say).
- The lock is invisible to readers: while the record is live, the
  public `isInputEnabled()` reports the recorded state, not the lock
  (board.js's internal flag is untouched). Its one reader is the AI
  preview, which snapshots it to restore after the tool completes
  (game-view.js:926, restored at 663 and 935): a preview starting
  mid-show would otherwise snapshot the lock, and the tool's
  completion would hand back a locked board.
- It applies when the show's own snap or return entry resolves,
  never over a PV frame; for a board press, at the later of that and
  the gesture's pointerup, pointercancel or blur.
- If the board is editing at that moment, nothing is applied.
  `enterEditMode` drops the markers and hands move input to the
  position editor (board.js:325-352); a game-handler re-arm would
  clobber it. Edit start is `board_update`-driven, so it can land
  while the snap is pending: the Edit click cancels the show, the
  server answers, the snap is still behind the in-flight frame.
- An outside position set, whether it cancelled the show or arrived
  while an apply was pending, owns the visuals from its notification
  on: `setPosition` marks its own last move at call time
  (board.js:194-208) and its callers manage arrows (`board_update`
  clears them, game-view.js:685). The apply then carries no markers
  and no arrows, only the input state, and lands when the latest
  outside flight resolves (`board.js` hands the show that promise
  after enqueuing), never over a moving board. On a queue reset it
  lands at once: cm-chessboard updates `state.position` at
  `setPosition` call time (Chessboard.js:91-99), so the reset's own
  snap draws the latest target, the outside one. For the same reason
  an outside position set retires the show's pending snap, which is
  never re-issued after a reset; re-issuing it would drag the board
  back to the recorded position.

## Queue reset

`cancelAnimations` runs on the flag-fall `GAME_RESULT` (no
`board_update` precedes it), on window focus, and on visibility
regain (game-view.js:806, 1123-1126). Today it replaces the queue
but leaves the running `PositionsAnimation` alive; that orphan
repaints its own target on completion, over anything the fresh queue
painted meanwhile.

Fix in `board.js`, where the queue is already patched: the patch
takes over the animated branch of `enqueuePositionChange` (today
delegated to the library, board.js:62-64; same duration formula,
same exported `PositionsAnimation`) so it can keep the running
instance on the queue. `cancelAnimations` then completes that
instance in place, destroys and replaces the queue, and notifies the
show, all synchronously. Completing is `animationStep(Infinity)`,
the library's own end path: target pieces drawn, disappear elements
removed, `positionsAnimationTask` resolved, end event fired, entry
resolved. Not an rAF abort: the promotion dialog and the position
editor's piece dialog wait on that task (PromotionDialog.js:97,
SelectPieceDialog.js:41) and only a new animation replaces it, so an
abort would strand the next promotion. No orphan, so the fresh
queue's first paint is final -- for the show and for every other
caller (preview restore, hold release). Turn-board flips stay
delegated: a reset during a flip keeps that one orphan, and a flip
mid-show needs a switch-sides or new-game click, which cancels the
show first.

The show sees a reset as a queue-epoch bump and re-issues the entry
it was waiting on (unless an outside position set has retired it,
Record application). Alt-tab back never kills the line the user was
watching. When that entry was the in-flight one, the reset has
already landed it (and `cancelAnimations`' own synchronous snap to
the in-flight target goes through the internal helper, not the
public `setPosition`: not a drive, and a same-FEN no-op); the
re-issue is free and the loop moves on to markers and pause. When it
was queued behind a foreign entry, it plays now.

## Data

No server change. `engine_info` already carries `pv_uci`.

A row's position is sampled at `engine_info` arrival from the board:
its last externally set placement, never a show frame. Placement is
all `pvFrames` needs. Rows keep `{ placement, pv_uci, frames }`
beside the rendered text, written together and only by PV-carrying
infos.

`pvFrames` never throws. No placement (the board source not wired
yet, or the view unmounted) yields no frames. It truncates at the
first token that is malformed (`0000`, wrong length, non-square),
whose from-square is empty, or whose mover does not alternate colour
with the previous ply. Searches from different positions interleave
on one bus, so a row can be sampled against the wrong placement; a
truncated line is shown as far as it makes sense, never as ghosts.

## Components

- `pv-walk.js` (new, pure): `pvFrames(placement, pvUci) ->
  placements[]`. Uses the vendored cm-chessboard `Position`; handles
  promotion, castling rook hop, en passant; truncation and
  no-placement rules above.
- `line-show.js` (new): `createLineShow(primitives)` owns the state
  machine: generation and queue-epoch counters with their abort
  promises, recorded outside state, pending pause timer, and the
  Esc, pointerdown, pointerup, pointercancel and blur listeners
  (installed only during a show). Exposes `playLine(frames, { onStep,
  onEnd, isExempt })`, `cancelLine()`, `onOutsidePosition(fen,
  lastMove)`, `onOutsideFlight(promise)`, `onOutsideQueueReset()`,
  `onOutsideInput(enabled)`, `onOutsideArrows(type, pairs)` (one call
  per typed setter, `pairs` empty for that type's clear; the record
  is keyed by arrow type), `onOutsideArrowsClear()`, `destroy()`.
  Primitives injected from the board: position change returning the
  animation promise, markers, arrows get/set/clear, input
  enable/disable, current placement, editing flag. `playLine` refuses
  while editing; the record stays live until applied and the editing
  flag is read at apply time (Record application). Cancel never
  destroys the queue. `destroy` bumps the generation, drops listeners
  and pending pause, fires `onEnd`; `cancelLine` after `destroy` is a
  no-op.
- `board.js`: instantiates the line show with its primitives and
  exposes `playLine`, `cancelLine`, `currentPlacement`. Hooks only:
  `setPosition` notifies the show before applying and hands it the
  flight's promise after enqueuing;
  `cancelAnimations` completes the running animation in place,
  destroys and replaces the queue, then notifies the show
  (queue-epoch bump), synchronously in that order; `enableInput`, the
  arrow setters and `clearArrows` forward to the record during a
  show; `isInputEnabled` reads through the record while it is live;
  `destroy` destroys the show. The queue patch owns the
  animated branch of `enqueuePositionChange` (Queue reset above). The
  internal position helper returns the animation promise, which the
  public `setPosition` swallows today.
- `pv-table.js`: `update(info, pvText, placement)` writes the triple
  on PV-carrying infos only; double-click on a showable row calls
  `onActivate(handle)` with `handle = { frames, setPly(i),
  release() }`, keyed to the snapshot, not the `tr`. Drop pv-less
  infos for the playing row; unkey-and-pin on a PV-carrying conflict
  or clear; remove on release unless re-activated. `renderPvInto`
  wraps each SAN token in a span. Hover highlight, tooltip and
  double-click exist only on tables created with `onActivate`, and
  only on showable rows; showability is re-evaluated on every triple
  write. `dispose` cancels a running show.
- `game-view.js`: passthroughs `playLine`, `cancelLine`,
  `currentPlacement`.
- `play-dock-windows.js`: `setPvLineBoard({ currentPlacement,
  playLine, cancelLine })`; supplies `isExempt(target)` = target is
  inside `inst.wb`'s root, `inst.slot`, or `inst.inlineSlot`.
- `play.js`: wires `setPvLineBoard` before `restoreDebugWindows`
  (play.js:2581; the `/game/sync` right after it replays board then
  last info, and in view mode that replay is the only row); clears
  it on unmount after `setDockContainer(null)` (play.js:2698) so
  `dispose`'s `cancelLine` still reaches a live board.

## Constants

Named constants in `line-show.js`, no env plumbing (nothing in
`web/app` reads `SV_` vars):

- `PV_PLAY_PLY_PAUSE_MS`: the beat after each frame, 400ms on top of
  cm-chessboard's 300ms piece animation.
- `PV_PLAY_END_HOLD_MS`: the hold on the final position, 1200ms.

## Out of scope

Search Lines is desktop-only: no touch gesture. Tournament live-game
panels create tables without `onActivate`, so nothing there changes.
