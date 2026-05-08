# Environment variables

Runtime configuration knobs and debug toggles. All recognized
variables use the `SV_` prefix. Defaults are sized for typical
tournaments; override only when telemetry justifies it.

## Debug flags

Boolean: `0` (default) or `1`. Output is gated on `--debug` (i.e. the
`sturddle_view` logger at DEBUG level) -- enabling these without
`--debug` does nothing.

| Var | Default | Effect | Where |
|---|---|---|---|
| `SV_DEBUG_RECONCILE` | `0` | Per-record / per-match reconciliation traces. | [pgn-reconciliation.md](pgn-reconciliation.md) |
| `SV_DEBUG_PAIRING` | `0` | Pairing-state invariant assertions and traces. | [tournament-spec.md](tournament-spec.md) |

## Runtime knobs

Numeric. Override when telemetry shows the default no longer fits
the workload.

| Var | Default | Effect | Where |
|---|---|---|---|
| `SV_RECONCILE_TIMEOUT_S` | `60.0` | Max wait for a pending pair to match a PGN record before drop. | [pgn-reconciliation.md](pgn-reconciliation.md) |
| `SV_RECONCILE_LATE_WARNING_S` | `5.0` | Above this match latency, emit `reconcile late` INFO log. | [pgn-reconciliation.md](pgn-reconciliation.md) |
| `SV_RECONCILE_QUEUE_MAX` | `256` | Per-side ring-buffer cap (pending dissolutions, parsed PGN records). | [pgn-reconciliation.md](pgn-reconciliation.md) |
| `SV_EVENT_HISTORY_MAX` | `200` | Per-tournament event ring depth used by workspace `/events` backfill. | [pgn-reconciliation.md](pgn-reconciliation.md) |
| `SV_PGN_TAIL_POLL_S` | `1.0` | PGN file poll interval. Lower for faster matching at higher syscall cost. | [pgn-reconciliation.md](pgn-reconciliation.md) |

Algorithm constants (e.g. `_MAX_CAPTURED_OVERRUN_PLIES`,
`MIN_PLIES_FOR_MATCH`) are not env-overridable; they encode
properties of the fastchess + UCI protocol, not operator tunables.

Invalid (non-numeric) overrides log a warning and fall back to the
default.
