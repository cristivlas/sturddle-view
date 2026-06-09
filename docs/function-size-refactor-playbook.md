# Function-size refactor playbook

How to break a giant factory/handler function under the 250-line cap
(`scripts/audit_fn_size.py`) and remove its `oversized-ok` marker, distilled
from decomposing `openTournamentWorkspace` (1517 -> 179 lines).

This is the method, the traps, and per-offender notes. Read it fully before
touching code. Tackle offenders in the order given at the end (largest first).

---

## The cap, exactly

`scripts/audit_fn_size.py::scan_js` (JS) / `scan_python` (PY) is the authority.

- Flag condition is `n >= 250`. A function must end at **<= 249 lines**
  (`end_line - start_line + 1`).
- **Size = the full brace span from the declaration to where depth returns to
  0** -- it INCLUDES every nested inner function, blank line, and comment
  inside. This is the ONLY reason a factory measures huge: its inner closures
  count against it. Moving an inner function to module scope removes its entire
  span from the factory's count.
- Every declaration is measured independently. After lifting, each NEW
  module-level function is checked on its own -- so a 130-line inner function
  lifted as-is is fine, but if it were ~210 it must also be split.
- Run the gate: `python scripts/audit_fn_size.py` (lists violations + exempt).
  `--exit-nonzero-on-violation` for CI. A file vanishes from BOTH lists when
  every function in it is under cap.

Use `.venv/Scripts/python.exe scripts/audit_fn_size.py` on Windows.

---

## The core technique: one `ctx` state object

A giant factory closes over N mutable variables shared by ~M inner closures.
You cannot lift the closures to module scope while they read factory-local
variables. The fix:

1. Create ONE plain object `ctx` early in the factory.
2. Every lifted helper becomes a module-level `function name(ctx, ...args)`.
3. Inner closures' variable reads/writes become `ctx.x`.

What goes on `ctx`:

- **Config** (constructor args, never reassigned): `api, token, top, left, ...`.
  Safe to put on ctx as plain fields.
- **Collections mutated in place** (Map/Set/Array/object, never reassigned to a
  new value -- only `.push/.set/.delete/.clear/[k]=`): capture by reference on
  ctx. No write-through ceremony needed. (e.g. `windows`, `eventLog`.)
- **Reassigned scalars** (`let x = ...; ... x = newValue`): these are the
  hazard. They MUST live on ctx and be written `ctx.x = v` / read `ctx.x` AT
  POINT OF USE. **Never destructure a reassigned scalar into a local**
  (`const { detail } = ctx`) -- that freezes a stale snapshot. Destructuring is
  fine ONLY for immutable config (`const { left, top } = ctx`).
- **Forward-reference function table is NOT needed if you lift everything.**
  Module-level `function` declarations hoist, so a lifted helper can call any
  other lifted helper directly: `tearDown(ctx)`, `openSystemWindow(ctx, k)`.
  (During the MIGRATION, while some callees are still inner, a temporary
  `ctx.fn.foo = (...) => foo(...)` indirection bridges the gap -- but DELETE it
  once both sides are module-level. It is dead ceremony at the end.)
- **Bound listener thunks**: if the factory does
  `addEventListener(EVT, handler)` and later `removeEventListener(EVT, handler)`,
  and `handler` is now a lifted `function onX(ctx, e)`, you MUST store ONE bound
  thunk on ctx and use it for both add and remove:
  `ctx.onX = (e) => onX(ctx, e)` then add/remove `ctx.onX`. A fresh
  `(e) => onX(ctx, e)` at each site is a DIFFERENT reference -> the remove
  silently no-ops and leaks the listener.

For Python (`_run_loop`-style), the analogue is a small `@dataclass` state
object (or a helper class) passed to extracted methods; the same
"reassigned-scalar must be a field, read at point of use" rule applies.

---

## Step order (smallest-risk first)

Do it in commits/steps, running the audit + a smoke test after each. Roughly:

1. **Pure leaves first.** Lift functions with ZERO closure coupling (DOM
   builders, formatters, pure algorithms). They take no ctx (or only immutable
   args). Biggest line reduction for least risk. (Layout algos, body builders.)
2. **Read-mostly helpers.** Lift functions that read collections + a couple
   scalars and write DOM. Convert their scalar reads to `ctx.*`.
3. **Migrate one reassigned scalar at a time, ATOMICALLY.** For each scalar:
   add it to the ctx literal, delete the `let`, and convert EVERY reader and
   writer in the SAME edit. A half-migrated scalar (some readers on `ctx.x`,
   some on bare `x`) is the classic aliasing bug. Migrate the body refs / detail
   / flags this way.
4. **Split any inner function that is itself near/over 250** as you lift it
   (e.g. a 130-line render fn with a self-contained 80-line sub-block -> pull
   the sub-block into its own module fn). Mechanical: cut the block into
   `function subPart(ctx, ...)` and call it.
5. **Lifecycle/listeners last** -- highest risk (the bound-thunk symmetry trap).
6. **Collapse the factory to a wiring shell**, delete the `oversized-ok` marker,
   run the audit to confirm the file is gone from both lists.

For large mechanical steps, delegating to a subagent with EXACT per-function
specs (signature, which vars -> ctx, which siblings it calls) works well -- but
ALWAYS review its diff yourself (`git diff --ignore-all-space`) and re-run the
behavioral tests. Don't trust the self-report.

---

## Traps that bit me (avoid these)

- **Blanket regex on a common word hits comments.** Converting `windows` ->
  `ctx.windows` with `s/\bwindows\b/ctx.windows/` rewrote the English word
  "windows" inside comments. Scope conversions to code lines, or hand-edit, and
  grep the result for `ctx.<word>` inside `//` comments afterward.
- **Two helpers, same name, different return shape, different files.** A
  `wbGeometry` returning px-strings in one file and raw numbers in another is a
  footgun. Rename for shape-clarity (`wbGeometryPx` vs `wbGeometryNum`) with a
  cross-reference comment; do NOT try to "DRY-merge" different contracts.
- **Ordering / temporal-dead-zone.** A lifted helper referenced by a callback
  created BEFORE `ctx` exists (e.g. `slotGrid`'s `getMaxRows` arrow created at
  line 1579 but reading `ctx.activeLayout`) is fine ONLY because the arrow runs
  later. Verify nothing reads `ctx` synchronously before the `const ctx = {...}`
  line executes.
- **Don't half-do a cleanup.** If you route `removeItem` through a helper but
  leave the paired `setItem` bare, you've created the exact asymmetry a reviewer
  will flag. Either fully unify (add `loadRaw/saveRaw` too) or leave it and say
  why.
- **`saveRaw(k, v || null)` set-or-clear trap.** When collapsing
  `if (v) setItem(k,v); else removeItem(k)` into a helper that removes on null,
  CONFIRM the value domain never includes a meaningful falsy value (`"0"`, `""`).
  If it can store `"0"`, `|| null` wrongly clears it.

---

## Tests: characterize BEHAVIOR before refactoring

This repo's e2e is Python-driven Playwright (`server/tests/test_e2e_*.py`,
`pytestmark = pytest.mark.e2e`, run with `-m e2e` to bypass the default
`-m "not e2e and not perf"` in pyproject.toml).

- For a UI controller, write/identify e2e that pin the OBSERVABLE contract
  (geometry, DOM state, event-log ordering), not internals. Every test should
  assert `page_errors == []` -- that alone catches a broken closure reference
  introduced by a lift (it throws -> shows up as a pageerror).
- The refactor itself is behavior-preserving motion: a logic break fails
  DETERMINISTICALLY in isolation. If a test fails only under heavy concurrent
  load but passes 100% isolated, that is a FLAKE/timing signal -- investigate it
  (see below), do NOT assume it's your refactor, and do NOT assume it's "just
  load."
- Project rule: we OWN pre-existing issues. A 30s Playwright timeout is almost
  never "too slow" -- it's "a state that never arrived." When `console/pageerror`
  is EMPTY at a `wait_for_function` timeout, something failed to happen, not
  threw. Instrument the actual DOM/state progression (e.g. sample the element
  count over 60 frames) to see if it reached the target then regressed
  (resurrection) vs never reached it. Reproduce on the PRE-REFACTOR code (git
  worktree at HEAD) to attribute the bug.
  - That investigation found a REAL pre-existing app bug here: closing all
    windows could resurrect them when a stray `requestAnimationFrame(reapply)`
    fired after teardown. Fix was a `ctx.finalized` guard in the single layout
    dispatcher. See `docs`-adjacent memory `project_workspace_teardown_race`.

Known harness flake (leave it): a Windows Proactor `ResourceWarning`
(unclosed-socket `__del__`) surfaces ~1/10 under load from the shared
`run_uvicorn` fixture. It is a GC-timing artifact, not app or test logic.

---

## Project conventions the refactor must honor

(From repo memory / CLAUDE rules -- the next agent MUST follow these.)

- ASCII only in code/comments (`--` not em-dash, `=>` only as JS arrow).
- Code comments <= 3 lines.
- No `oversized-ok` left behind; no slice/phase references in code/comments.
- No magic numbers: numeric literals -> named const, env-configurable with `SV_`
  prefix where it's a real tunable.
- Never inline a helper to save lines; DRY wins. No string-literal duplication.
- Tests must be warning-free AND contain no sleeps/timeouts-for-sync (wait on
  observable state via `wait_for_function`, never a fixed delay; default
  Playwright timeout as a failure ceiling is fine, an explicit `timeout=` used
  as a sync wait is not).
- Propose the decomposition design and get approval BEFORE writing code.
- Commit to the current branch; ask before committing; push after an approved
  commit; no Co-Authored-By trailer.

---

## Remaining offenders, WORST (largest) first -- work this order

Current audit (re-run `scripts/audit_fn_size.py` before starting -- counts
drift as files change). `[E]` = has an `oversized-ok` marker to remove; `[V]` =
plain violation (no marker). Difficulty (driven by shared-mutable-state
coupling, NOT size) is guidance only; the WORK ORDER is strictly
largest-to-smallest.

Done so far: `openTournamentWorkspace` (the method this playbook distills),
`mountTournaments`, `mountGameView`, `mountEngineList` (-> single `ctx` +
engines-list-layout.js; the col-resize/wrap-sizer extraction pattern there is a
good template for the other list controllers), `mount` (1869 -> 477; the bus
handler + AI bridge + x-game cluster lifted onto a single `state` object),
`buildAnalysisTab` (513 -> 239; per-section row factories + reactive core).

1. **`mount`** (477, perspectives/play.js:1645) `[V]` -- HARDEST, partially
   done. Already a wiring shell over a single `state` object; remaining lifts
   (setNoEngine/checkEngines, listener closures, lifecycle return) are small and
   low-value. The hard forward-reference web is gone. Bus-handler safety now
   depends on wiring order (documented at registration).

2. **`showImportPositionDialog`** (347, import-position-dialog.js:497) `[V]` --
   MED. State `format` (reassigned -> ctx), `submitting`, `recentsCache`. Recents
   dropdown + submit extractable; `format` threads through several handlers.

3. **`_run_loop`** (331, server/.../ai_analysis.py:870) `[E]` -- HARD (Python).
   18+ reassigned flags/counters in one agent-loop state machine. Extract pure
   decision helpers (`_should_nudge_*`) + a per-round state dataclass; the core
   loop stays largely intact.

4. **`openLiveGameWindow`** (319, tournament-live-game.js:382) `[E]` -- HARD.
   THREE intertwined state machines (position/animation coalescer,
   clock/timers, WebSocket lifecycle) over 10+ reassigned vars. Encapsulate each
   machine (PositionCoalescer/ClockManager-style helper), don't just lift.

5. **`mountTournamentTemplateForm`** (318, tournament-template-form.js:31) `[E]`
   -- EASY. Only shared state is one `inputs` map (in-place, never reassigned).
   `getValues`/`validate` already pure. Sections: grid, switch row, adjudication
   -> one builder each. (Easiest of the remaining -- a good warm-up, but NOT
   first in size order.)

6. **`createDockableWindow`** (263, play-dock-windows.js:387) `[V]` -- MED.
   Float/dock state machine; reassigned dock/geometry state. Some inner handlers
   extract; the dock-vs-float lifecycle is the coupled core.

7. **`createOpeningsPanel`** (257, import-position-dialog.js:209) `[V]` --
   EASY-MED. Simple 5-var state
   (`selectedPgn/selectedRow/rows/filterText/sortOrder`), mostly read-only or
   set-together. Independent search/sort/column-resize blocks; `filtered()`
   pure. The engines-list-layout.js col-resize helper is directly reusable here.

Now under the 250 cap (no longer offenders): `pickFile` (235), `mountBoard`
(229), `build_command` (219), `buildLiveGameBox` (207), `showEngineOptionsDialog`
(204).

11. **`build_command`** (219, server/.../tournament/fastchess.py:64) `[V]` --
    likely EASY (Python). Sequential CLI-arg builder; low coupling. Extract
    per-section arg helpers.

12. **`buildLiveGameBox`** (207, tournament-live-game.js:170) `[V]` -- read to
    assess; likely DOM construction for the live-game box, low-med coupling.

13. **`showEngineOptionsDialog`** (204, engine-options-dialog.js:320) `[V]` --
    just over cap; smallest. Likely a per-option-row builder + submit; extract
    the row builder.

---

## Definition of done (per offender)

- Function under 199 lines; any extracted helper also under cap.
- `oversized-ok` marker deleted (if it had one); file absent from
  `audit_fn_size.py` output.
- `node --check` passes on every touched file (JS); tests run for Python.
- Behavioral e2e green, isolated AND under a multi-file load sweep (run the
  relevant `test_e2e_*` a few times concurrently). Zero NEW warnings.
- Reviewed your own diff with `--ignore-all-space`; reverted any out-of-scope
  drift. Proposed design before coding; committed to current branch after
  approval; pushed.
