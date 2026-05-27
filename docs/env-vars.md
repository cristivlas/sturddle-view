# Environment variables

Runtime configuration knobs and debug toggles. All recognized
variables use the `SV_` prefix. Defaults are sized for typical
tournaments; override only when telemetry justifies it.

## Core

Override CLI defaults or pin values for desktop-mode subprocesses.
Any field on `Settings` is also implicitly readable as
`SV_<UPPER_FIELD_NAME>` -- the table lists only those set explicitly
by the CLI or by `desktop.py`.

| Var | Source | Effect |
|---|---|---|
| `SV_HOST` | `--host` | Bind address. Default `127.0.0.1`. |
| `SV_PORT` | `--port` | Bind port. Default `8765`. |
| `SV_TOKEN` | random / `desktop.py` | Shared-secret auth token. |
| `SV_AUTH_DISABLED` | `--no-auth` | Disable token auth. |
| `SV_TLS_CERT` | `--cert` | TLS certificate path. Requires `SV_TLS_KEY`. |
| `SV_TLS_KEY` | `--key` | TLS private key path. Requires `SV_TLS_CERT`. |
| `SV_ENGINE_PATH` | `--engine` | Fallback engine when registry has no selection. |

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

## AI agent

Tunables and toggles for the AI analysis coordinator. See
[ai-analysis-spec.md](ai-analysis-spec.md) for the full design. UI
exposure for tunables is pending; until then these are ops-only knobs.
Numeric defaults are defined as named module constants in the listed
source; check the file when a precise value matters.

| Var | Default | Effect | Where |
|---|---|---|---|
| `SV_AI_API_KEY` | unset | Headless fallback for the active provider's API key; OS keyring takes precedence. | `server/sturddle_view/key_store.py` |
| `SV_AI_MAX_TOOL_ROUNDS` | `32` | Hard cap on agent loop rounds per turn. Hit emits `done.round_cap=true`. | `server/sturddle_view/play/ai_analysis.py` |
| `SV_AI_ANALYZE_MAX_TIME_MS` | module const | `analyze` tool per-call wall-clock cap; caller's `time_ms` clamped down. | `server/sturddle_view/play/tools_engine.py` |
| `SV_AI_ANALYZE_MAX_DEPTH` | module const | `analyze` tool per-call depth cap; caller's `depth` clamped down. | `server/sturddle_view/play/tools_engine.py` |
| `SV_AI_RECOMMEND_MARGIN` | module const | Centipawn dominance margin for `recommend_move` to accept the model's pick over the engine's top line. | `server/sturddle_view/play/tools_engine.py` |
| `SV_AI_TOP_MOVES_MAX_N` | module const | Hard cap on `top_moves` candidate count; over-large `n` clamped. | `server/sturddle_view/play/tools_engine.py` |
| `SV_AI_INLINE_TOOL_ID_LEN` | module const | Synthetic `tool_use_id` length for inline-tool-call recovery. | `server/sturddle_view/llm/inline_tool_calls.py` |

### AI debug flags

| Var | Default | Effect | Where |
|---|---|---|---|
| `SV_AI_TRANSCRIPT` | unset | Opt-in: write per-turn transcripts to disk. | `server/sturddle_view/llm/transcript.py` |
| `SV_AI_DEBUG` | `0` | Flip AI loggers to DEBUG when `--debug` is also on. | `server/sturddle_view/app.py` |
