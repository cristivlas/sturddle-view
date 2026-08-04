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
| `SV_INSTANCE` | `--instance` | Instance suffix isolating config/data dirs (`SV_INSTANCE=2` -> `sturddle-view-2`). Empty = default. |

## Paths and storage

Override the default config/data file locations (chiefly for tests and
isolated instances). Each falls back to the platform default when unset.

| Var | Default | Effect |
|---|---|---|
| `SV_SETTINGS_FILE` | platform config dir | Path to the persisted user settings JSON. |
| `SV_ENGINE_REGISTRY_PATH` | platform config dir | Path to the persisted engine registry JSON. |
| `SV_GAME_STATE_PATH` | platform data dir | Path to the live game-state snapshot. |
| `SV_IMPORTS_DIR` | platform data dir | Directory for the imported PGN/FEN history store. |
| `SV_INSTANCE_LOCK_PATH` | platform data dir | Override the single-instance lock-file location (chiefly tests and isolated instances). |
| `SV_ENGINE_TMP_ROOT` | platform data dir | Root for per-spawn engine temp dirs (TMP/TEMP/TMPDIR injection; see [engine-temp-cleanup-spec.md](engine-temp-cleanup-spec.md)). |
| `SV_ENGINE_PROBE_TIMEOUT_SEC` | `3.0` | Timeout for the engine UCI handshake probe (floored at `0.05`). |
| `SV_MAX_IMPORT_BYTES` | `2097152` | Cap on `/game/import` payload size (2 MiB). |
| `SV_MAX_ANNOTATION_LENGTH` | `10000` | Cap on individual move-annotation text length. |
| `SV_BOOK_INDEX_MAX_PLIES` | `40` | Plies tokenized per line when indexing an HVE opening book; also the hard cap on book depth (the plies setting is clamped to it). |

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

## HvE difficulty

Sampling tunables for difficulty levels 1-9. See
[hve-difficulty-spec.md](hve-difficulty-spec.md). `hve_difficulty`
itself is a UI-managed persisted setting, not listed here; scoring has
no knobs (the reply is sampled from the one normal search).

| Var | Default | Effect | Where |
|---|---|---|---|
| `SV_HVE_TEMPERATURE_STEP_CP` | `25.0` | Softmax temperature per level below max: `step * (10 - level)` centipawns. | `server/sturddle_view/config.py` |
| `SV_HVE_DROP_CAP_STEP_CP` | `50.0` | Hard cost cap per level below max: candidates costing more than `step * (10 - level)` cp behind the final best are never sampled. | `server/sturddle_view/config.py` |
| `SV_HVE_DEPTH_PENALTY_CP` | `15.0` | Cost per depth of shallowness added to an iteration candidate, so stale candidates fade at high levels. | `server/sturddle_view/config.py` |
| `SV_HVE_SCORE_CLAMP_CP` | `1000.0` | Mates fold to ~+/-clamp and cp scores clip to the same range before softmax. | `server/sturddle_view/config.py` |

## Tournament engine proxy

Set on the proxy subprocess environment, not via the CLI.

| Var | Default | Effect | Where |
|---|---|---|---|
| `SV_PROXY_SECRET` | per-run random | Per-tournament secret for the engine-proxy stdio broadcast; passed via env (never argv) and popped on read. | [tournament-spec.md](tournament-spec.md) |
| `SV_BROADCAST_INFO` | `1` | Kill-switch: `0` suppresses UCI `info` lines from the broadcast tap. | [tournament-spec.md](tournament-spec.md) |

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
| `SV_AI_VERIFIER_MAX_ROUNDS` | `8` | Round cap for a verifier sub-run (one move, a tool call or two, a verdict). | `server/sturddle_view/play/ai_analysis.py` |
| `SV_AI_SEMANTIC_CHECK` | `1` | LLM judge that clears regex position-check flags the prose meant about a past/hypothetical/alternate position. Only drops flags, never adds; off reverts to regex-only. Accepts `1`/`true`/`yes`/`on`. | `server/sturddle_view/play/ai_analysis.py` |
| `SV_AI_THINKING_BUDGET_TOKENS` | `4096` | Default Anthropic extended-thinking budget; UI override persists per-settings. | `server/sturddle_view/config.py` |
| `SV_AI_MAX_RECOMMEND_FAILURES` | `2` | Consecutive failed `recommend_move` calls before the loop nudges the model to `top_moves`. | `server/sturddle_view/play/ai_analysis.py` |
| `SV_AI_ANALYZE_MAX_DEPTH` | `30` | `analyze`/`top_moves` per-call depth cap; caller's `depth` clamped down. Searches are depth-only (no time limit) for determinism. | `server/sturddle_view/play/tools_engine.py` |
| `SV_AI_MIN_DEPTH` | `10` | `analyze`/`top_moves` per-call depth floor; caller's `depth` clamped up (then capped at `SV_AI_ANALYZE_MAX_DEPTH` if lower). Shallow searches rank candidates poorly. | `server/sturddle_view/play/tools_engine.py` |
| `SV_AI_RECOMMEND_MARGIN` | `50` | Centipawn dominance margin for `recommend_move` to accept the model's pick over the engine's top line. | `server/sturddle_view/play/tools_engine.py` |
| `SV_AI_VERIFICATION_DEPTH` | `25` | Floor depth for the end-of-turn recommendation check; searches at least this deep (deeper if the model asked for more). | `server/sturddle_view/play/tools_engine.py` |
| `SV_AI_TOP_MOVES_MAX_N` | `5` | Hard cap on `top_moves` candidate count; over-large `n` clamped. | `server/sturddle_view/play/tools_engine.py` |
| `SV_AI_REPORT_LINE_MAX_PLIES` | `40` | Hard cap on `report_line` continuation length; bounds payload size (no engine search). | `server/sturddle_view/play/tools_engine.py` |
| `SV_AI_RELATED_OPENINGS_MAX_N` | `8` | Cap on sibling variations returned per `related_openings` call; a broad family would otherwise flood context. | `server/sturddle_view/play/tools_openings.py` |
| `SV_AI_OPENING_PHASE_SLACK_PLIES` | `12` | Plies of slack past the book line before the commentary opening-theory directive (mandating a `related_openings` call) switches off. | `server/sturddle_view/api/_ai_kick.py` |
| `SV_AI_ANNOTATION_PER_COMMENT_MAX` | `200` | Max characters per PGN annotation before truncation. | `server/sturddle_view/api/_ai_kick.py` |
| `SV_AI_ANNOTATION_TOTAL_MAX` | `1500` | Max total characters across annotations before trailing entries are dropped. | `server/sturddle_view/api/_ai_kick.py` |
| `SV_AI_INLINE_TOOL_ID_LEN` | module const | Synthetic `tool_use_id` length for inline-tool-call recovery. | `server/sturddle_view/llm/inline_tool_calls.py` |

### AI debug flags

| Var | Default | Effect | Where |
|---|---|---|---|
| `SV_AI_TRANSCRIPT` | unset | Opt-in: write per-turn transcripts to disk. | `server/sturddle_view/llm/transcript.py` |
| `SV_AI_DEBUG` | `0` | Flip AI loggers to DEBUG when `--debug` is also on. | `server/sturddle_view/app.py` |
| `SV_AI_FORCE_INLINE_CALLS` | `0` | Force the model to emit tool calls as inline text (exercises the inline-call recovery path). Accepts `1`/`true`/`yes`/`on`. | `server/sturddle_view/llm/prompts.py` |
