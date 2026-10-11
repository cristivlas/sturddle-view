# Edit from play -- execution plan (working doc)

Spec: `docs/annotation-edit.md`, section "Edit from play (planned)".
Branch: `feat/annotations`, baseline a0ff777 (suite green at fc4e2ec).
Stage explicit paths only.

## Decisions already made (do not reopen)

- Leaving saves unconditionally (any live game with moves), after a confirm.
- The force-delete path (`close_view`, `?force=1`) stays; separate cleanup.
- The clone shares the live game's `game_id` and carries its Recents hash.
- The edit-commit leave confirm is server-driven: 409 unless `leave: true`.
- "Changed" = placement, side to move, castling. Server owns it, all views.
- `resumable` goes; `resume_human_white` stays.
- Return to live is today's plain restore; the clone commit's sync into
  the suspended snapshot is the only carrier of notes home.
- Last-ply rule: a clone never rests on its last ply. No origin flag.
- Importing the game in progress over itself: 409 `in_progress`; the
  dialog tags its row "playing".
- Opening book across a restart: out of scope.

## Approach

- Server: TDD. Each step starts with red tests at the HVE or TestClient
  level, then the change, then green. Harness: `hve` fixtures
  (`test_edit_mode.py`, `test_view_mode.py`), TestClient with the
  `_write_uci_stub` fake engine (`test_play_from_here_recents.py`).
- Client: implement, smoke the existing suite, live-test (script below),
  then write the e2e cases. e2e harness: `run_uvicorn_subprocess`,
  `PageObserver`, `/_test/hve/state`, `make_searching_fake_uci`.
- Rules that bite here: no sleeps or timeouts for sync in new tests;
  zero warnings; no schema bump on `GameState`; named constants for every
  new string and number; no slice/step references in code or comments;
  functions under 250 lines (HVE helpers stay small and separate).
- One commit per step, user approves each, one-line message, explicit
  paths, push after. Never commit red.
- Steps 1-7 keep every endpoint and payload field the shipped client
  uses (`/view/start` with `{}` / `suspend` / default landing,
  `/view/resume-play`, `resumable`), so each step stays green with the
  current client and its e2e. Step 8 moves the client; step 10 removes
  them.

## Steps

### 1. Crash-recovery snapshot carries comments, fork link, pre-analysis pause

Change
- `GameState` gains `play_comments`, `play_root_comment`,
  `parent_game_id`, `fork_ply` (defaults; no version bump).
- `_game_state_snapshot` fills them; `paused` records the pre-analysis
  mode when taken in ANALYZING.
- `restore_from` restores `_play_comments`, `_play_root_comment`,
  `_fork_link`.
- `GameStore.load()` builds `GameState` field by field: read the new keys
  there, with defaults, or the round-trip test goes red for the wrong
  reason.
Files: `play/game_store.py`, `play/human_vs_engine.py`.
Red tests (`test_game_persistence.py`)
- snapshot -> restore round-trips comments, root comment, fork link;
- a snapshot file without the new keys loads with defaults;
- snapshot taken in ANALYZING entered from PAUSED records `paused=True`;
- restart mid-game keeps comments (spec S) and the fork link (spec V):
  new HVE over the same store, then finish the game, row has both.

### 2. Live clone identity, status flag, `/view/start` contract

Change
- `_enter_live_clone(ply)`: snapshot (live clocks; carries the fork link
  since step 1), comments, summary, Recents hash via
  `hash_for_id(game_id)`, install the view at `ply` keeping the live
  `game_id`. Only builder of a clone.
- `enter_view_mode` splits around one view installer; the clone helper
  suspends then installs, and `suspend_play` goes. The clone gets its
  fork link from the snapshot; `/view/start`'s `{}` form keeps passing it
  (its annotate-to-promote path) until step 10.
- `is_live_clone` property (`_suspended_play is not None`).
- `in_progress` computed once: live board with moves, or a suspended
  game with moves. Shipped by GET /game/status and every board update.
- `/view/start` with `suspend` calls the helper; its other forms stay
  until step 10.
Files: `play/human_vs_engine.py`, `api/game.py`.
Red tests
- `test_view_mode.py`: clone `game_id` equals the live id; `_view_hash`
  equals the exported row's hash, None when no row; `in_progress` true on
  a clone with moves, false on a zero-move clone and on a plain view.
- `test_game_status_api.py`: same via HTTP; plain view stays false.
- `test_board_event_payload.py`: board update carries `in_progress` in
  play, view, edit; clone payload carries `resume_human_white`.
Update: tests calling `enter_view_mode(suspend_play=...)` move to the
clone helper; HTTP `/view/start` tests wait for step 10 (see "Existing
tests to change").

### 3. Return to live on the server

Change
- Return to live is `_restore_suspended_play` as it is today: no comment
  carry, no snapshot write (step 4's commit sync owns both).
- One helper applies the last-ply rule: a clone whose cursor lands on
  its last ply returns to live. `view_goto` calls it (`view_forward` and
  `view_last` delegate there); step 7's edit exit reuses it.
- No AI replay handling: navigation is refused in ANALYZING, and the
  client-driven exits from it already clear the buffer.
- `resume_play`, `/view/resume-play` and `resumable` stay for the shipped
  client (removed in step 10).
Files: `play/human_vs_engine.py`, `api/game.py`.
Red tests
- `test_view_mode.py`: each of the three ops on a clone at n-1 -> PLAY or
  PAUSED; `view_goto(n-1)` stays a clone; a plain view at the last ply
  stays a view; mode after return is PAUSED when suspended from your
  turn, PLAY from the engine's turn.
`test_e2e_play_scrub_resume.py` stays green: it tracks the resume-play
call but waits on board state, not on the call (rewritten in step 9).

### 4. Commit on a live clone

Change
- API `edit_commit`: branch on `hve.is_live_clone` before any Recents
  lookup; no `save`, no `replace_at`, no `clear_fork_link`.
- HVE: on a clone, `_apply_view_annotation` updates comments only (no
  `_view_hash` rewrite); the commit copies comments into
  `_suspended_play` and writes the snapshot through the store
  (`_persist` refuses in view). The only carrier of notes home.
- Plain view: unchanged here. Its promote path (insert, fork-link carry)
  still serves the shipped client's `{}` pencil; it goes in step 10.
Red tests (`test_view_start_and_edit_recents.py`, `test_edit_mode.py`,
`test_game_persistence.py`)
- commit on a clone: no new row; an exported row is byte-identical;
  `_view_hash` unchanged before and after;
- `GameStore.load()` has the note right after the commit, and still
  right after return, before any move; a fresh HVE restored from it keeps
  the note;
- scrub forward after a note at k: the live game has it;
- `close_view` on a clone after a note keeps the note;
- fork link survives the commit (reaches the later Leaving save).

### 5. "Changed" rule

Change: `commit_edit` compares placement, side, castling only.
Red tests (`test_edit_mode.py`)
- unchanged commit right after a double push with a legal ep capture:
  plain view keeps its moves; a clone at k keeps its moves and the note
  (the edit-from-play half is in step 7);
- side-to-move or castling change still counts as changed.

### 6. Leaving helper

Change
- `_leave_live_game()`: if on a clone, Return to live without publishing;
  then the finished-game pair (`_maybe_save_pgn`, Recents save via
  `_save_to_recents`: `replace_at` when a row exists); clear the store;
  drop `_suspended_play` and `_fork_link`. No moves -> nothing saved.
- The pending Recents save carries its fork link; the flush uses that
  copy and never reads `_fork_link` (the replacing op has reset it).
- Wired at the commit point of: `new_game` (after engine spawn, before
  the bundle swap), every view install except the clone helper's (leave,
  then install: import, FEN commit, and the shipped `{}` form of
  `/view/start`, whose discard confirm reads wrong until step 8),
  `play_from_here` (seed captured first, game-over check, spawn, then
  leave and swap).
- `/game/import` refuses content whose resolved `game_id` is the live
  game's (playing or on its clone): 409 `in_progress`, game untouched.
Files: `play/human_vs_engine.py`, `api/game.py`.
Red tests (`test_new_game.py`, `test_recents_play_save.py`,
`test_play_from_here_recents.py`, `test_pgn_save.py`)
- New Game and import from a live game with moves -> row with the
  comments; no moves -> nothing (spec R);
- game exported earlier -> the same row updated, exactly one row;
- from a clone -> the clone's note is in the row;
- FEN-commit from a clone -> old game in Recents, new view at the FEN;
- Play from here on a clone -> live game saved, fork's parent link
  resolves to it (spec H), fork has comments up to k;
- failed engine spawn on New Game and Play from here -> live game or
  clone untouched, no row (spec N);
- with PGN autosave on, the file of a left game carries its notes (spec O);
- store is empty after Leaving; a new HVE restores nothing (spec W);
- the left game's row keeps its fork link; on Play from here the parent
  row is not its own parent and the child keeps its link;
- importing the live game's exported row -> 409, game still running,
  playing and on a clone; a pasted older export imports as its own row.

### 7. Edit from play

Change
- `Op.ENTER_EDIT_MODE` allows PLAY, PAUSED, ANALYZING from play.
- `enter_edit_mode` from play: cancel analysis or think; pause if your
  turn; `_enter_live_clone(last ply)`; EDITING. No origin flag.
- `cancel_edit` and an unchanged `commit_edit` apply step 3's last-ply
  helper: a clone at its last ply returns to live; at k it stays.
- Entry and exit each run under one lock with one publish of the final
  state.
- API `edit_commit`: changed position on a live clone with moves and no
  `leave: true` -> 409 `would_leave`, mode stays EDITING. With it ->
  Leaving, then the FEN view. Zero-move clone: no 409, nothing saved.
Red tests (`test_edit_mode.py`, API)
- edit/start from PLAY on your turn -> clone at last ply, suspended game
  paused; from the engine's turn -> think cancelled, return re-kicks the
  engine (fake engine moves); from PAUSED; from ANALYZING from play ->
  analysis cancelled, returns PAUSED (spec B); from idle -> 400;
- exit table, both pencils: cancel -> live; unchanged + note -> live with
  note; changed -> 409 without `leave`, still EDITING (spec K); with
  `leave` -> Recents row and new view; from a scrubbed clone, unchanged
  -> clone at k;
- zero-move game: changed commit -> no 409, no row, new view; cancel
  -> back to play (clone at ply 0 is its last ply);
- edit from play right after a double push with a legal ep capture:
  unchanged commit returns to live with the note (spec J);
- entry and exit each publish exactly one board update.
Update: `test_e2e_ai_prose_annotation.py`
`test_play_pencil_cancel_leaves_game_resumable`: through the current
client, cancel now returns to play (the clone sat on its last ply).
Gap until step 8: the shipped AI pencil makes a clone, so a changed
commit gets a 409 that client cannot answer (it errors; cancel works).
Land step 8 right after.

### 8. Client

Change (`web/app/perspectives/play.js`, `web/app/tournament-live-game.js`)
- Pencil path: one call to `/game/edit/start`; drop the `/view/start` +
  `/game/sync` flip, `suppressCommentsForEditTransition`,
  `CONFIRM_EDIT_FROM_PLAY`. Keep the stop-analysis confirm.
- `_playInProgress` reads the board update's `in_progress`.
- Remove `resumeLivePlay`, `viewReachedNonLast`, `RESUME_FAILED`, the
  auto-resume branch in the view handler.
- Shared leave confirm (one helper, per-path verb constants, not
  destructive): New Game, import, Play from here, parent toast, tournament
  replay, commit on 409 (then resend with `leave: true`; restore the
  game-id filter on the 409 since the commit nulls it first).
- Import: replace-viewed prompt only when viewing and not in progress;
  toast on 409 `in_progress`.
- Import dialog: one matcher for the current game's row, by hash
  (viewing) or by the live `game_id` (playing); that row is disabled and
  tagged "viewing" or "playing".
- Comment commit: drop the x-game refetch after it.
- Scrub-back keeps sending `suspend: true` until step 10 removes the
  key (without it the server still takes the old no-suspend form).
Then: live-test script, then step 9.

### 9. e2e (after the live pass)

- New Game from a clone through the UI: confirm text, row with notes.
- Edit cancel from play lands on the live board (same id, no drop).
- Scrub forward lands on live; no resume-play call (rewrite
  `test_e2e_play_scrub_resume.py`).
- Reload mid-edit, then change the position: the commit still asks (409
  path).
- Commit 409 then cancel: board stays live, filter restored.
- Import from a clone asks once.
- Parent toast on a clone asks; parent opens; old game in Recents.
- Tournament replay over a live game: new wording, not destructive.
- AI pencil from play: prose prefilled, OK returns to play paused.
- Remount on a clone keeps POV (keep `test_e2e_view_flip_remount.py`).
- Cursor at last ply on entry (keep `test_e2e_edit_from_play_cursor.py`).
- Recents row of an exported game tagged viewing while on its clone,
  before and after a note, and tagged playing while playing (extend
  `test_e2e_import_recents_viewed.py`).

### 10. Docs and dead code

- `annotation-edit.md`: status line to implemented; drop "(planned)".
- `recent-imports.md`: the resume-play mention.
- Remove what the old client used: `/view/start`'s `{}` and `suspend`
  forms and default landing (ply now required, 1 <= ply < move count,
  else 400), `resume_play`, `/view/resume-play`, `resumable`; the
  client's scrub-back drops the `suspend` key.
- Remove the promote path: `replace_at`'s `old_hash=None` insert, the
  comment branch's fork-link carry, `clear_fork_link`, `enter_view_mode`'s
  `fork_link` parameter. A plain view always `replace_at`s its row by
  id; no row (test hooks) writes nothing. `replace_at` keeps its
  fork-link parameters (the finished-game save passes them).
Red tests: `/view/start` missing ply, 0, last ply -> 400 (spec P);
`/view/resume-play` -> 404; payload has no `resumable`; a comment commit
on a hook-seeded view with no row writes nothing.
- `docs/testing.md`: e2e list if files were added or renamed.

## Spec acceptance bullets -> steps

| Spec bullet                               | Step | Kind      |
|-------------------------------------------|------|-----------|
| exit table rows, both pencils             | 7, 9 | API, e2e  |
| paused on return from AI turn             | 1, 7 | unit      |
| leave confirm shows / does not            | 8, 9 | e2e       |
| tournament replay wording                 | 9    | e2e       |
| parent toast confirm + fork link          | 6, 9 | API, e2e  |
| in_progress values; New Game via UI       | 2, 9 | API, e2e  |
| scrub forward carries the note home       | 4    | unit      |
| Play from here on a clone                 | 6    | API       |
| clone never in Recents; hash stable       | 4    | API       |
| ep unchanged commit                       | 5, 7 | unit      |
| 409 without leave; reload still asks      | 7, 9 | API, e2e  |
| import from a clone asks once             | 9    | e2e       |
| failed spawn leaves things untouched      | 6    | API       |
| left game keeps fork link; no self-parent | 6    | API       |
| one board update per transition           | 7    | unit      |
| zero-move edit exits to play              | 7    | unit      |
| comment commit never inserts a row        | 10   | API       |
| import over the live game: 409; tag       | 6, 9 | API, e2e  |
| PGN autosave file carries notes           | 6    | unit      |
| /view/start ply validation                | 10   | API       |
| shared id; no dropped update              | 2, 9 | unit, e2e |
| New Game / import save or not             | 6    | API       |
| restart keeps comments                    | 1    | unit      |
| restart on a clone / after return         | 4    | unit      |
| restart keeps the fork link               | 1    | unit      |
| restart after leaving: no resurrection    | 6    | unit      |

## Tests beyond the spec list (why)

- Op matrix for edit/start from each mode: the spec names the legal set,
  the illegal ones need pinning (idle, EDITING, ANALYZING from view
  unchanged).
- Each navigation op separately: three entry points into the one rule in
  `view_goto`; a delegation slip would skip it.
- Engine's-turn entry and return: think cancel and re-kick are easy to
  drop; the fake engine proves the move arrives.
- `replace_at` versus `save` on Leaving: a duplicate row is the regression
  an exported game would hit.
- Snapshot file without the new keys: no schema bump means old files load.
- `close_view` on a clone keeps a committed note: the force path stays,
  and the commit-time sync must cover it too.
- Retired endpoint returns 404 and payload lacks `resumable`: proves the
  cleanup, catches a half-done removal.

## Existing tests to change or delete

- Step 2: `test_view_mode.py` callers of `suspend_play` (1085, 1230) use
  the clone helper; `test_board_event_payload.py` and
  `test_game_status_api.py` gain `in_progress` and the clone case (plain
  view stays false).
- Step 4: `test_play_from_here_recents.py:175`: its premise (a clone
  commit adds a row, force-delete resumes) is gone; rebuild on an
  exported live game (row exists, clone carries its hash), then
  force-delete.
- Step 10: `test_view_start_and_edit_recents.py` posts `/view/start` from
  idle (no moves), where no valid ply exists; rebuild on an import or a
  live game with moves, and drop the "no suspend leaves nothing to
  resume" case. `test_view_mode.py` no-fork-resume block (1082+) goes
  (step 3 covers navigation return); its `{}`-form simulation (878) and
  `test_pgn_eval_capture.py:96` move to the clone helper.
  `test_recents_play_save.py:359`:
  the link rides in the snapshot, no `fork_link` pass-through.
  `test_recent_imports.py`: `:371` (`old_hash=None` promote) goes; `:613`
  (explicit link) moves onto an existing row. `test_board_event_payload.py`
  drops `resumable`. `test_e2e_view_flip_remount.py` drops the `suspend`
  key.
- Step 8: `test_e2e_edit_from_play_cursor.py`: the pencil no longer posts
  view/start; assert the cursor via edit/start.
- Step 9: `test_e2e_play_scrub_resume.py`: rewrite (no resume-play).
- `test_e2e_ai_prose_annotation.py`:
  `test_play_pencil_cancel_leaves_game_resumable` -> cancel returns to
  play (step 7); `test_play_pencil_without_prose_keeps_discard_confirm`
  -> no confirm (step 8).

## Live-test script (one step per turn, clean slate)

1. New game, play three moves, click the pencil on your turn. Editor opens, no confirm.
2. Add a note, OK, confirm the edit. Back in play, paused; no note shown (play shows none).
3. Resume, play on. Pencil on the engine's turn, cancel. Engine resumes.
4. Click a past move. View pencil, add a note, OK, confirm. Scrub forward to the end: play resumes. Scrub back: both notes show.
5. Save PGN, open the import dialog: the row is tagged playing, no trash. Scrub back, reopen: tagged viewing. Add a note, reopen: still tagged.
6. On a clone: New Game. Confirm says saved to Recents. Row has the notes.
7. Pencil, drag a piece, OK. Leave confirm. New view at the FEN; old game in Recents.
8. Pencil, reload the page, then drag a piece and confirm. Still asks.
9. AI analysis to done, AI pencil. Prose prefilled, OK, confirm. Back in play, paused.
10. Restart the server mid-game. Notes still there. (Owner restarts; never the agent.)
11. Tournament replay with a live game. New wording, not red.
12. Export a Play-from-here game, scrub back, click the parent toast. Confirm, parent opens.
