# SPRT UX -- mini-spec

Status: design complete, not yet implemented.

## 1. Settings dialog -- new SPRT tab

New tab alongside the existing Tournament tab. Stores global SPRT defaults
used to pre-populate new tournament SPRT params dialogs. Does not override
per-tournament values once set.

Fields:

| Field   | Type                          | Validation      | Default |
|---------|-------------------------------|-----------------|---------|
| `elo0`  | number                        | < elo1          | 0       |
| `elo1`  | number                        | > elo0          | 10      |
| `alpha` | number                        | 0 < x < 1       | 0.05    |
| `beta`  | number                        | 0 < x < 1       | 0.05    |
| `model` | select: trinomial/pentanomial | required        | pentanomial |

Persisted to global settings (same mechanism as Tournament tab).

---

## 2. Tournament template form -- SPRT toggle

### Switch placement

Add a `wa-switch` labeled "SPRT" to the existing switch row (Affinity /
Oversubscribe / Ponder). No inline param fields in the main form.

### Toggle-on behavior

1. `tournament_type` coerced to roundrobin; type select disabled.
2. `rounds` input disabled (value preserved, not cleared).
3. A per-tournament SPRT params popup opens (see section 2a), pre-filled
   from Settings > SPRT defaults.

### Toggle-off behavior

1. Type select and `rounds` re-enabled.
2. Stored SPRT params removed from the template payload.

### `getValues()` output

When SPRT switch is on and popup was confirmed:
```json
{
  "sprt": { "elo0": 0, "elo1": 10, "alpha": 0.05, "beta": 0.05, "model": "pentanomial" },
  "tournament_type": "roundrobin"
}
```
`rounds` is omitted when SPRT is on (fastchess self-terminates on conclusion).

### 2a. Per-tournament SPRT params popup

- Small modal dialog, not a settings page.
- Pre-filled from Settings > SPRT defaults on first open; from the
  tournament's stored values on subsequent opens (e.g. edit flow).
- Same five fields as Settings > SPRT tab.
- Confirm button disabled until validation passes.
- Validation: `elo0 < elo1`; `0 < alpha < 1`; `0 < beta < 1`.
- Cancelling leaves the SPRT switch in whatever state it was before the
  popup opened (i.e. if user toggled on then cancelled, switch reverts off).
- Always reachable via a small "Edit..." link next to the SPRT switch when
  the switch is on, so params can be revised without re-toggling.

---

## 3. Tournament workspace -- SPRT panel

Current state: `.wb-sprt` is a single text line:
```
SPRT [elo0, elo1] * LLR=x.xx [lower, upper] * status
```

### 3a. LLR progress bar

Replace the text line with a structured block:

```
H0  [====|=========*============|====]  H1
        lower     llr          upper
```

- Bar spans the full `[lower_bound, upper_bound]` range.
- Marker (`*` / thumb) positioned proportionally at current `llr`.
- Color scheme:
  - Running: neutral (gray/blue)
  - `status === "H1"`: green (engine is stronger)
  - `status === "H0"`: red (no significant difference)
- Tooltip on hover: "LLR = x.xx (lower_bound, upper_bound)"
- Labels: "H0" left, "H1" right; `elo0`/`elo1` values shown as
  subscripts or in the tooltip, not inline (keeps bar compact).

### 3b. Conclusion banner

When `sprt.status !== "continue"`, render a one-line banner above the
standings table (below the progress bar):

- `H1`: "H1 accepted -- [EngineA] is stronger (Elo +{elo1})"
- `H0`: "H0 accepted -- no significant difference at Elo +{elo1}"

Banner persists for the lifetime of the tournament view (not dismissible).
Color matches the bar: green for H1, red/amber for H0.

### 3c. Pairs count label

The existing `G` column in the standings table counts individual games.
For SPRT tournaments, add a secondary line below the progress bar:
"N pairs played" (where N = `sprt.pairs` from the API response).
This makes it clear the test operates on pairs, not individual games.

---

## API response -- no changes needed

`sprt` object already returned by `GET /api/tournaments/{id}` when
`template.sprt` is present. Fields used by the workspace:

```
sprt.elo0, sprt.elo1, sprt.llr, sprt.lower_bound, sprt.upper_bound,
sprt.status ("continue" | "H0" | "H1"), sprt.pairs
```

---

## Out of scope

- SPRT for gauntlet tournaments.
- Path B resume completion for partial SPRT pairs (already deferred in
  pgn-stats-audit.md).
- Per-engine SPRT (only 2-engine tournaments supported).
