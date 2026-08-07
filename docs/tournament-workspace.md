# Tournament Workspace -- Business Rules & Test Scenarios

## Storage

One entry per tournament in localStorage, keyed `sturddle:workspace:<id>`.
Cleared automatically when the tournament is deleted.

Shape:
```json
{
  "_closed": false,
  "standings": { "open": true,  "x": "...", "y": "...", "width": "...", "height": "...", "min": false, "max": false, "z": 10 },
  "schedule":  { "open": false, "x": "...", "z": 0 },
  "engines":   { "open": true,  "x": "...", "z": 12 },
  "log":       { "open": true,  "x": "...", "z": 11 }
}
```

`_closed`: true when the workspace was explicitly dismissed (Close All or last window X-closed).
Absent or false when saved via navigate-away.

---

## Business Rules

### Ribbon "Open Workspace" button

| Saved state | Action |
|---|---|
| Has at least one window with `open: true` | Restore those windows (ignore `_closed`) |
| All windows `open: false`, or no saved state | Default logic |

Default logic: open Standings; if tournament is RUNNING also open Live Games; if event log has entries or tournament is RUNNING also open Event Log.
Windows with no saved position/size open at their minimum size, centered.

### Navigation (clicking a tournament in the list)

| Saved state | `hadWorkspace` | Action |
|---|---|---|
| `_closed: false` + at least one `open: true` | any | Restore open windows |
| `_closed: true` (dismissed) | any | Do NOT open workspace |
| No saved state | true | Open with default logic |
| No saved state | false | Just select, do not open workspace |

`hadWorkspace`: a workspace for a *different* tournament was active at the time of navigation.

### Layout mode interaction (Tile / Snap)

When the active layout is Tile or Snap (not None or Tidy):
- Opening a live game window (Watch button) triggers an immediate layout reapply.
- Closing any window (system or live) triggers a layout reapply.
- Restoring a minimized live window triggers a layout reapply.
- Slot grid placement and overflow-minimize are disabled; all live windows open visible.

Tidy mode is excluded from auto-reapply on open/close because it also opens all
system windows, which would be disruptive.

The active layout mode is persisted **per tournament** in that tournament's
workspace state (`_layout`), not globally. Opening a workspace restores its own
saved layout (or None when it had no open windows); closing the active workspace
resets the in-memory layout to None so the next tournament starts clean.

### Status-driven auto-opens (inside an active workspace)

Fires only when `hasSavedState` is false (no open-window snapshot in effect):
- Tournament transitions to RUNNING and Live Games window is closed -> auto-open Live Games.

### Save points

| Event | What is saved | `_closed` |
|---|---|---|
| Navigate away (`workspace.close()`) | Snapshot of all open windows with position/size/min/max/z | absent (false) |
| Window > Close All (`closeAll()`) | Snapshot captured **before** windows are closed, including z | `true` |
| User X-closes last window (`finalize()`) | All-closed snapshot (all windows were already null) | `true` |

No continuous monitoring. State is captured only at the save points above.

### Live game windows (out of scope for state persistence)

Live game windows ("watch" buttons in Live Games / Engine Instances) are intentionally excluded
from saved state. Do not attempt to save or restore them.

Reasons:
- They are tied to an active proxy connection. The proxy is gone once the tournament is no
  longer running or the user navigates away; there is nothing to reconnect to on restore.
- On navigate-away (`workspace.close()`), stale live windows (proxy-id and unresolved
  game-id attaches) are closed immediately. Resolved game-id windows (final result banner)
  are left open for the user to review and are closed only by Window > Close All.
- The user must re-open live windows manually after returning to a tournament. This is
  expected and intentional -- the game state they were watching no longer exists.

---

## Test Scenarios

### Ribbon "Open Workspace"

**T-R1** -- Fresh tournament (no saved state), click Open Workspace
- Expect: Standings opens; Live Games + Event Log open if tournament is RUNNING or has log entries.

**T-R2** -- Tournament with saved state, all windows open, click Open Workspace
- Expect: Exact set of open windows restored with saved positions/sizes/min/max.

**T-R3** -- Close All, then click Open Workspace
- Expect: Same windows that were open before Close All are restored (snapshot was taken before close).

**T-R4** -- Close All, then Open Workspace, then Close All again, then Open Workspace again
- Expect: Second Open Workspace still restores the original snapshot (snapshot not corrupted by Close All).

**T-R5** -- Manually X-close all windows one by one, then click Open Workspace
- Expect: Default logic runs (no open windows in snapshot -- all were null when finalize saved).

**T-R6** -- Close one window with X, then Close All, then Open Workspace
- Expect: Only the windows that were open at the time of Close All are restored (X-closed window excluded).

### Navigation

**T-N1** -- No workspace open, click a fresh tournament (no saved state)
- Expect: Tournament selected, no workspace opens.

**T-N2** -- Workspace open for A, navigate to fresh tournament B (no saved state)
- Expect: B opens with default logic (hadWorkspace=true, no saved state for B).

**T-N3** -- Workspace open for A (3 windows open), navigate to B, navigate back to A
- Expect: A restores its 3 windows; B does not open workspace (no saved state, hadWorkspace was false when navigating from A->B... actually B did open via default, so navigating B->A: hadWorkspace=true for B, but A has saved state so restores).

**T-N4** -- Close All on A's workspace, navigate to B, navigate back to A
- Expect: A does NOT open workspace on return (_closed=true).

**T-N5** -- Workspace open for A, navigate to B (B has saved open windows, _closed=false)
- Expect: B's workspace opens with saved windows restored.

**T-N6** -- Workspace open for A, navigate to B (B previously had Close All -- _closed=true)
- Expect: B does NOT open workspace.

**T-N7** -- Manually X-close all windows on A, navigate away, navigate back to A
- Expect: A does NOT open workspace (_closed=true from finalize).

**T-N8** -- Workspace open for A with windows at custom positions, navigate away, navigate back
- Expect: Windows reopen at saved positions, sizes, and min/max states.

### State integrity

**T-S0** -- Open workspace with multiple windows, bring W2 to front, navigate away, navigate back
- Expect: W2 is the topmost (focused) window on restore; z-order of all other windows matches saved order.

**T-S1** -- Open workspace, minimize window W1, navigate away, navigate back
- Expect: W1 opens in minimized state.

**T-S2** -- Open workspace, maximize window W1, navigate away, navigate back
- Expect: W1 opens in maximized state.

**T-S3** -- Open workspace, resize and reposition windows, navigate away, navigate back
- Expect: Custom geometry (x, y, width, height) restored for all windows.

**T-S4** -- Open workspace with 4 windows, X-close W1, navigate away, navigate back
- Expect: 3 windows open (W1 absent); W1's last known position available for when it is reopened via Window menu.

**T-S5** -- Navigate away while tournament transitions to RUNNING (auto-open fired schedule)
- Navigate back.
- Expect: Schedule window is present in snapshot and restored; not double-opened.

### Regression guard (bugs previously observed)

**T-BUG1** -- X-close one window, then Close All, then Open Workspace
- Previously: X-closed window appeared; a visible window did not.
- Expect: Only the windows open at Close All time are restored.

**T-BUG2** -- Close All, navigate away, navigate back
- Previously: workspace re-opened.
- Expect: workspace does NOT open.

**T-BUG3** -- No prior save (first session), Close All, Open Workspace
- Previously: only Standings opened (defaults ran instead of restoring).
- Expect: all windows that were open before Close All are restored.
