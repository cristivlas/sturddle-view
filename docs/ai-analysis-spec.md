# AI Analysis & Commentary - Spec

Design spec for AI-driven analysis commentary that augments engine-based
analysis. Decisions captured from brainstorm; implementation details and
phase tracking live in `ai-analysis-progress.md`.

## Goal

Add an AI agent that produces prose commentary alongside existing engine
output. Agent consumes engine eval/PV/depth and authors human-readable
analysis. Engine numbers remain the source of truth; AI is the narration.

## Scope

Three trigger paths:

1. Live during `play` mode (interactive game vs engine)
2. Live during `view` mode (PGN replay; on-demand)
3. Post-game batch analysis of a finished PGN

Tournament mode is out of scope.

## Architecture

### Agent

- Tool-using LLM agent
- Multi-PV is optional capability, not assumed (engine support varies)
- Agent consumes engine output via tools, emits prose + structured
  annotations

### Tools (v1 list; refine during impl)

- `analyze(fen, time_ms=None, depth=None)` - engine search; spawns
  throwaway engine via existing `_spawn_engine()` pattern
- `get_position(ply)` - FEN/SAN accessor on game history
- `get_pgn_range(from_ply, to_ply)` - SAN move sequence
- `tablebase_probe(fen)` - wraps existing `TablebaseProber` (Syzygy WDL/DTZ)
- `opening_lookup(fen)` - wraps existing `OpeningBook.lookup()` (TSV-based)
- `compare_moves(fen, [moves])` - wrapper around `analyze`

### Engine lifecycle for `analyze` tool

- One throwaway engine per `analyze` call (pool later if perf demands)
- Same binary as active gameplay engine
- Stop sequence (matches existing `cancel_analysis` pattern):
  1. UCI `stop`
  2. Grace timeout (env-var configurable)
  3. Transport close / process kill
- Tool implementation enforces caller's limit - no runaway engines

### Providers

Two supported providers, both pluggable behind a common abstraction:

- Anthropic (native tool use)
- Ollama (OpenAI-compatible tool format; translation layer)

Common interface modeled on a `generate_thinking_stream_with_tools()`
shape: streaming response, tool-use loop, tool_result blocks fed back as
user messages. Anthropic format is canonical; Ollama provider translates
to OpenAI function-call format on the wire.

### Configuration

- All major settings editable from UI (no server-only requirement)
- API key handling:
  - Server mode: env var only
  - Desktop mode (PyInstaller): OS keyring, plaintext file fallback
- UI never receives full API key back; masked status only
- All numeric tunables: named module constants, env-var override (SV_
  prefix), optional UI exposure

## Triggers & Modes

| Mode | Game state | Button action |
|------|-----------|---------------|
| play | in progress | live analysis (engine + optional AI) |
| play | finished   | post-game analysis (path 3) |
| view | any        | post-game analysis (path 3) |

- Same ribbon Analyze button across modes; semantics shift by state
- Manual trigger only (no auto-on-game-end in v1)
- Re-run allowed (overwrite); confirm prompt if prior AI annotation exists
- Cancel aborts both engine and LLM as a single user-facing task

## Output Format

### Prose

- Streamed live to UI (token-by-token)
- Coach persona in live `play` mode
- Commentator/analyst persona in `view` and post-game

### Structured annotations

- Per-move records (ply, severity, theme, text)
- Persisted to PGN only for path 3 (post-game)
- Live commentary (paths 1, 2) is ephemeral - not persisted
- PGN format: standard `{}` comments; portable
- Metadata: provider/model captured in `[Annotator "..."]` PGN tag

### Citations

- Cite engine eval numbers explicitly in prose
- Engine named once per game:
  - Live: brief inline mention
  - View/post: PGN `[Annotator]` tag

## UI

### Layout

- Engine info pinned (existing PV panel)
- AI prose scrolls in adjacent/below section (revisit at impl)

### Settings tab "Analysis"

Flat:
- Provider (Anthropic / Ollama)
- Model
- API key (Anthropic) / Ollama URL (Ollama) - conditional on provider

Advanced collapsible:
- Per-move token cap
- Per-game token cap
- Tool call cap
- analyze max time_ms / max depth
- Stop grace timeout
- Debug log toggle
- Temperature
- (Other tunables as they emerge)

## Guardrails

- Per-move token cap (env + UI)
- Per-game token cap (env + UI)
- Live (paths 1, 2): per-move cap only
- View/post-game (path 3): min(per_move, per_game / remaining_plies)
- Tool call cap per agent turn (env)
- `analyze` per-call hard caps on time_ms/depth (env)
- Concurrency: 1 analysis at a time
- User cancel always available
- Wall-clock timeout deferred past v1 (testing complexity)

## Prompt Design

- Shared system prompt + mode-specific addendum
- Mode addendum sets persona (coach vs commentator-analyst)
- Tool guidance: explicit rules + budget-aware + mode-dependent hints
- Length target: chess-magazine style; experiment and tune
- Structure: experiment; no rigid template in v1
- Actual prompt text drafted at impl time (premature now)

## Streaming Protocol

- New event kind on existing websocket bus (e.g., `ai_info` /
  `ai_commentary`)
- Not multiplexed onto `engine_info` (clean schema separation)
- Client renders in dedicated DOM area

## Error Handling

- Non-negotiable: no silent failures
- Server: structured logs, full detail
- Client: toast per category:
  - Auth (bad/missing key) - actionable
  - Rate limit - actionable
  - Network/timeout - retry
  - Provider error (refusal, content filter) - surface message
  - Budget exhausted - informational, not error

## Testing Principles

Phase-level test strategy lives in `ai-analysis-progress.md`. Top-level
principles:

- Mock the LLM provider at the abstraction boundary (not HTTP), so
  Anthropic and Ollama paths share tests
- Reuse existing engine integration test infrastructure for `analyze` tool
- Deterministic agent tests: canned LLM responses + canned engine output
- Cancellation must be tested: tool implementation guarantees no runaway
- No sleeps/timeouts for synchronization (project rule)

## Out of Scope (v1)

- Tournament mode integration
- Auto-trigger on game end
- Multi-PV for engines that don't support it (agent uses `compare_moves`
  workaround)
- Engine pool for `analyze` calls
- Wall-clock timeout backstop
- Caching of post-game annotations (re-run always overwrites)

## Open Items (decided at impl)

- Length target (try chess-magazine standard, iterate)
- Output structure (experiment, iterate)
- Exact prompt text
- Settings tab visuals
- AI panel exact placement / dimensions
- Cache key strategy if/when caching is added
- Model dropdown vs free-form input per provider

