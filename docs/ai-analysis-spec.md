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

### Initial context vs. tool-driven discovery

- Full PGN + per-ply eval array injected into the initial user message
  (chess games are small: ~80 plies SAN < 2k tokens typically)
- `get_position` / `get_pgn_range` remain available as tools but are
  expected to be rarely needed in path 3; primary value is live mode
  (game grows turn by turn) and heavily-annotated re-runs

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
  - **Storage backend (cross-platform):** Python `keyring` library
    (macOS Keychain, Windows Credential Manager, Linux Secret Service
    via libsecret/kwallet).
  - **Per-provider keys:** service name `sturddle-view`, account name
    `ai_api_key_<provider>` (e.g. `ai_api_key_anthropic`). Lets multiple
    providers coexist without a single shared slot.
  - **Read order:** keyring → `SV_AI_API_KEY` env var → empty.
  - **Headless / no backend:** Linux containers may lack a secret
    service daemon; fall back to env var (server mode). Surface the
    backend used in logs.
  - **UI:** never receives the full key; GET returns masked string
    when set, blank when unset. PUT writes through to keyring.
  - **Manual clear (when the user revokes a key out-of-band):**
    - **macOS:**
      `security delete-generic-password -s sturddle-view -a ai_api_key_anthropic`
    - **Windows:** Credential Manager UI (Control Panel ->
      Credential Manager -> Windows Credentials -> remove the
      `sturddle-view` / `ai_api_key_anthropic` entry) or
      `cmdkey /delete:sturddle-view`
    - **Linux:**
      `secret-tool clear service sturddle-view account ai_api_key_anthropic`
- UI never receives full API key back; masked status only
- All numeric tunables: named module constants, env-var override (SV_
  prefix), optional UI exposure

## Triggers & Modes

| Mode | Game state | Button action | Path |
|------|-----------|---------------|------|
| play | in progress | live coach commentary (rolling session) | 1 |
| play | finished   | post-game analysis | 3 |
| view | navigating | live commentator on current position | 2 |
| view | analyze-all | post-game analysis over full PGN | 3 |

- Same ribbon Analyze button across modes; semantics shift by state
- Manual trigger only (no auto-on-game-end in v1)
- Re-run allowed (overwrite); confirm prompt if prior AI annotation exists
- Cancel aborts both engine and LLM as a single user-facing task
- Cancel is hard-stop: kill current tool + LLM stream, drop agent loop;
  no cooperative wrap-up turn (UI shows partial prose as-is)
- Tool use is sequential only in v1 (no parallel tool calls); Anthropic
  provider sets `disable_parallel_tool_use: true`
- Forward-looking: parallel tool use is desirable later (latency + token
  savings). Design the tool dispatcher and cancellation to tolerate
  concurrent tool execution from day one even though v1 runs one at a
  time — i.e., per-call cancellation tokens, no shared mutable scratch
  between tools, results keyed by tool_use_id. Flip is then a config
  change, not a refactor.

### Live session model (paths 1, 2)

- Rolling agent session for the lifetime of the game (not per-click)
- System prompt + accumulated turns persist across Analyze invocations
- Prompt caching (Anthropic native) covers system + early turns
- Session reset on: new game, takeback past an annotated ply, mode swap
- On Analyze click: agent decides what to discuss given session history
  (coach judges relevance — recent move, position, strategic arc — no
  forced focus from the UI)

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

### PGN write path

- Single helper (e.g., `apply_comment(node, prose, *, machine_token=None)`)
  owns the "user prose first, machine token appended" composition rule;
  AI write path calls the same helper
- Re-run on path 3: if any target ply already has prose, prompt user —
  Overwrite / Cancel / Save backup (.bak sibling file, exact format TBD
  at impl)
- AI prose lands in the same per-ply comment slot as human prose
  (concatenated). Round-trip caveat: re-imported AI prose is
  indistinguishable from human prose to the sanitizer — accepted
  trade-off, mirrors the eval/time-token convention

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
- **Use AI analysis** toggle (label TBD) — master switch. Off: ribbon
  Analyze buttons run engine-only (today's behavior). On: ribbon
  Analyze buttons run engine + AI; mode/state selects path 1/2/3.
  No per-click AI toggle and no new ribbon buttons.
- Provider (Anthropic / Ollama)
- Model (free-form or dropdown TBD per provider)
- Provider-specific credentials:
  - Anthropic: API key
  - Ollama: base URL (no key)

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
- Live (paths 1, 2): per-move cap per invocation; per-game cap also
  applies (rolling session accumulates cost across clicks)
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

- Two event kinds on existing websocket bus:
  - `ai_info` - prose tokens during streaming (live + post-game)
  - `ai_annotation` - completed per-ply structured record (post-game)
- Not multiplexed onto `engine_info` (clean schema separation)
- Client renders prose in dedicated DOM area; annotations attach to
  their ply for PGN persistence

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
- Caching of post-game annotations (re-runs require explicit user
  Overwrite confirmation per the PGN write path)

## Open Items (decided at impl)

- Length target (try chess-magazine standard, iterate)
- Output structure (experiment, iterate)
- Exact prompt text
- Settings tab visuals
- AI panel exact placement / dimensions
- Cache key strategy if/when caching is added
- Model dropdown vs free-form input per provider
- Backup format on re-run overwrite (.bak sibling file vs.
  `[OriginalComments]` PGN header vs. other)

