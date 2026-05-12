# SPRT UX -- mini-spec

Status: implemented.

## 1. Settings dialog -- SPRT tab

Tab alongside the existing Tournament tab. Stores global SPRT defaults
used by the server to expand `sprt: true` in tournament templates into a
full params dict at create/edit time. Defaults do not override
per-tournament values that already include explicit params.

Fields:

| Field   | Type                          | Validation      | Default |
|---------|-------------------------------|-----------------|---------|
| `elo0`  | number                        | < elo1          | 0       |
| `elo1`  | number                        | > elo0          | 10      |
| `alpha` | number                        | 0 < x < 1       | 0.05    |
| `beta`  | number                        | 0 < x < 1       | 0.05    |
| `model` | select: pentanomial/logistic  | required        | pentanomial |

Validation runs on input/change. Invalid fields get a red outline
(`.sprt-invalid`); the debounced PUT is skipped while any field is
invalid. Last-valid persists. The dialog can be closed in any state --
invalid defaults simply don't reach the server.

The `model` select offers only what the server can compute locally:

- `pentanomial` (sent to fastchess as `normalized`): the per-pair
  normalized-Elo model. Default.
- `logistic`: the per-game W/D/L trinomial model with observed draw
  rate as the nuisance parameter.

fastchess also accepts `bayesian`, but the local server-side SPRT
computation does not implement it; it's intentionally omitted from the
select to avoid silent failure during recompute.

---

## 2. Tournament template form -- SPRT toggle

### Switch placement

A `wa-switch` labeled "SPRT" sits in the existing switch row (Affinity /
Oversubscribe / Ponder / SPRT). No inline SPRT param fields in the form.

### Availability

The switch is disabled unless the engine builder has exactly 2 engines.
Toggling the engine count to !=2 while SPRT is on flips the switch off
automatically.

### Toggle-on behavior

1. `tournament_type` is set to roundrobin and the type select is disabled.
2. The `rounds` input is disabled (value preserved, not cleared).
3. The template payload carries `sprt: true` (boolean) -- no popup.

### Toggle-off behavior

1. Type select and `rounds` re-enabled.
2. `sprt` removed from the template payload.

### `getValues()` output

When the switch is on:

```json
{
  "sprt": true,
  "tournament_type": "roundrobin"
}
```

`rounds` is omitted when SPRT is on (fastchess self-terminates on
conclusion via `-rounds 0` -> 500,000-round cap; see
`tournament/fastchess.py`).

### Per-tournament param overrides

Not exposed in the UI. Per-tournament params are resolved server-side
by `_resolve_sprt` in `api/tournaments.py`: it merges the incoming
template's `sprt` value (truthy or partial dict) with the global
Settings > SPRT defaults. To change params for a single tournament,
edit the global defaults in Settings before creating it. An earlier
draft of this spec proposed a per-tournament popup; that was prototyped
and dropped (poor UX -- adds a modal-in-modal to the create flow for a
case that's rare in practice).

---

## 3. Tournament workspace -- SPRT panel

The `.wb-sprt` block is a single text line:

```
SPRT <candidate> [elo0, elo1] * LLR=x.xx [lower, upper] * N pairs * status
```

- `<candidate>` is the first engine name (engine A in the SPRT sense).
- `N pairs` is `sprt.pairs` from the API response. For the logistic
  model this counts individual games rather than pairs -- the field name
  is kept for UI continuity.
- `status` is one of:
  - `continue`
  - `H1 (<candidate> is stronger)` -- block gets `.wb-sprt--h1` (green border)
  - `H0 (no significant difference)` -- block gets `.wb-sprt--h0` (red border)

An earlier draft of this spec called for a graphical LLR progress bar
between H0 and H1 with a thumb at the current LLR, plus a separate
conclusion banner above the standings table and a separate "N pairs
played" line. That was prototyped and dropped: the numeric text line
already conveys range, current LLR, pair count, and the colored verdict
in less space, and the bar didn't add enough at-a-glance value to
justify the extra surface.

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
- Bayesian SPRT (fastchess supports it; the local recompute path does not).

## Pause/Resume support

SPRT is designed for run-to-completion, but Pause/Resume is supported
with a small, bounded discrepancy between the workspace banner and
fastchess's own stdout LLR.

Each Pause/Resume cycle dedups partial pairs from the PGN and
subtracts the dropped game's W/L/D from fastchess's per-pair counters
in `config.json`. Pentanomial counters (`penta_*`) are not touched:
fastchess only writes a pentanomial entry when a pair's second game
completes, so partial pairs never made it into the pentanomial -- no
subtraction is needed. Zeroing them was tried (commit b221385) and
broke fastchess's internal SPRT by wiping the running pentanomial on
every Stop.

Consequences:

- The workspace LLR banner is recomputed from the PGN on every API
  read and is authoritative across any number of Pause/Resume cycles.
- fastchess's view of the tournament is missing one pair per dropped
  partial pair (one per Stop in the worst case). Its stdout LLR
  therefore converges slightly slower than the banner, in proportion
  to how many Stops happened.
- fastchess will still self-terminate at its bound; the gap just
  delays termination by the same fraction of pairs that were lost.
  Users in a hurry can Stop manually once the workspace banner
  concludes H1/H0.
