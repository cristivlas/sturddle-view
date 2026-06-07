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

### File placement

- **Server coordinator:** `server/sturddle_view/play/ai_analysis.py`
  holds `AIAnalysisCoordinator`. Composed by the play perspective
  alongside `HumanVsEngine` (not methods on it). Mirrors
  `TablebaseProber` / `OpeningBook` siblings.
- **Server providers:** `server/sturddle_view/llm/` package --
  `base.py`, `anthropic.py`, `ollama.py`, `gemini.py` (the last two
  share `openai_compat.py`), plus shared `tools.py`, `prompts.py`,
  `transcript.py`, `inline_tool_calls.py`, `harmony_strip.py`,
  `markdown_strip.py`.
- **Engine tools:** `server/sturddle_view/play/tools_engine.py`
  (`analyze`, `top_moves`, `recommend_move`, `validate_move`,
  `piece_at`); the narrator additionally has `delegate` (spawns a
  verifier sub-run) -- see §Planner + verifier subagents.
- **Events:** `ai_info` (prose stream), `ai_thinking`, `ai_tool_call`,
  `ai_tool_call_failed`, `ai_recommendation` on `events.py` `EventKind`.
- **Client panel:** `web/app/play-ai-window.js`; mirrors
  `play-commentary-window.js` (same `createDockableWindow` factory).
  `play.js` wires open/close + event subscription.

### Agent

- Tool-using LLM agent
- Multi-PV is optional capability, not assumed (engine support varies)
- Agent consumes engine output via tools, emits prose + structured
  annotations

### Agent loop

Implementation: `AIAnalysisCoordinator.run()` in
`server/sturddle_view/play/ai_analysis.py`.

One "turn" = one Analyze click. A turn runs N rounds, capped at the
"Max rounds" setting (`ai_max_tool_rounds`; env `SV_AI_MAX_TOOL_ROUNDS`,
default in `ai_analysis.MAX_TOOL_ROUNDS`).
Each round = one `provider.stream()` call. The coordinator owns
multi-turn assembly; the provider knows nothing about tool execution.

Round body, in order:
1. Stream chunks from the provider (`text`, `thinking`, `tool_use`).
2. Publish `text` deltas as `ai_info` events, `thinking` as
   `ai_thinking`. A `tool_use` chunk ends the round (v1 is sequential;
   downstream chunks would belong to the next round per Anthropic
   semantics).
3. Decide exit / continuation:
   - Clean round (no tool_use) -> break. Natural end of turn (a
     completeness nudge may run one more round if no `recommend_move`
     landed yet).
   - Tool_use pending -> append the assistant message; continue.
4. If a tool_use is pending: dispatch via the registry, append the
   `tool_result` user message (matching `tool_use_id`). Failures
   surface as `ai_tool_call_failed` events but do not break the loop
   -- the model can read the structured error and recover.

The loop terminates on: clean exit (no tool_use),
`MAX_TOOL_ROUNDS` reached (`done.round_cap=true`), provider error,
or user cancel. Every exit emits a terminal `ai_info` event with a
`done` payload so the UI never hangs.

### Planner + verifier subagents

The narration loop conflates two jobs -- narration (judgment, short prose
budget, voice rules) and verification (deep search, many tool calls, raw
eval). Splitting them keeps raw eval out of the narrator's context (a
structural fix for engine over-trust). Applies to both personas; only the
base system prompt differs.

**Split.** The narrator's registry holds `recommend_move`, `top_moves`
(rank its own candidate moves in one call), `report_line`, and
`delegate`. The verifier registry holds the search/inspection tools
(`analyze`, `top_moves`, `piece_at`, `validate_move`); both registries
share one per-turn `search_cache`, so a position searched once isn't
searched again across them.

**Flow per turn:** the narrator names the critical lines and calls
`delegate(question)` once per line; each spawns a verifier sub-run
(`AIAnalysisCoordinator._run_verifier`, verifier prompt + registry, no
`delegate` -- one level deep). The verifier must call a tool before
concluding (a no-tool verdict draws one nudge), and returns a one/two
sentence live-position conclusion that lands as the delegate tool_result.
The narrator synthesizes and calls `recommend_move`.

**Verifier sub-run is silent except tool calls.** Its `analyze`/`top_moves`
events forward to the UI (nested under the originating "Verifying line" row
via `parent_tool_use_id`); its prose/thinking are suppressed. Thinking is
forced off for the verifier (the engine does the reasoning; thinking only
adds latency x fan-out and risks Ollama `<think>` leaking into the verdict).

**Anti-hallucination.** Grounding is prompt-driven plus the tools the
model can consult: `piece_at` settles a square, `report_line` replays a
line's legality, `top_moves`/`recommend_move` score moves. Wrong tactical
judgment is caught by forcing a tool call before a verdict (the verifier
must call a tool before concluding). There is no post-hoc prose validator
layer (an earlier round-end legality/piece checker was removed -- see the
branch history).

Rejected: structured JSON verdicts (small Ollama models emit malformed
JSON -> json-repair dependency). Prose avoids it.

**Accepted limitation.** With the registry split, the narrator's only engine
gate on its pick is `recommend_move`'s A/B dominance check; it does not
separately validate the line behind the move. The dominance check rejects a
move the engine's best beats by margin -- the guard that matters; deeper
line-validation is the model's job via `delegate`.

**Settling on a move.** The narrator weighs its candidates with one
`top_moves` call (all candidates ranked best-first for the side to move),
then submits with a single `recommend_move`. When the submitted move is
meaningfully weaker than the best, `recommend_move` rejects it and names
the stronger move in the reason -- the narrator resubmits *that* move, so
a rejection resolves in one step rather than open-ended probing. The last
accepted `recommend_move` is the turn's pick and drives the on-board
arrow (via the end-of-turn `ai_recommendation` verifier search).

### Tools (v1)

- `analyze(fen, depth=None)` - engine search; spawns throwaway engine
  via existing `_spawn_engine()` pattern. Depth-only (no time limit):
  a timer firing before the depth completes makes the bestmove
  non-deterministic on near-equal candidates. SHIPPED.
- `validate_move(move)` - legality check for UCI or SAN move strings
  against the live position. Tool card asks the model to pre-check
  before naming a move as playable. SHIPPED.
- `piece_at(square)` - report the piece occupying a square in the
  live position (or null when empty). Tool card asks the model to
  pre-check before naming a piece on a specific square. SHIPPED.
- `top_moves(n=None, depth=None)` - rank top-N candidate
  moves in the live position (workaround for engines without native
  MultiPV). Operates on the live board via `board_provider` (no FEN
  input). Uses UCI `searchmoves` (python-chess `root_moves` kwarg).
  SHIPPED.
- `tablebase_probe()` - wraps existing `TablebaseProber` (Syzygy WDL/DTZ);
  pending. Register conditionally on `engine_default_syzygy_path` being
  set so the tool never appears for users without tablebases.
- `opening_lookup()` - wraps existing `OpeningBook.lookup()`; pending.
- `compare_moves(fen, [moves])` - same `searchmoves` blocker as
  `top_moves`; pending until engine supports it.
- `get_position(ply)` / `get_pgn_range(from_ply, to_ply)` - low
  priority; full PGN is already in the initial user message.

#### Tool-registry-as-source-of-truth

The system prompt's `Tools:` block is rendered by iterating
`ToolRegistry.specs()` -- each tool's `description` field flows into
both the wire schema sent to providers and the prose listed in the
prompt. Adding a tool requires no manual edit to the prompt; removing
one cannot drift.

Per-tool usage guidance (how to interpret results, edge cases) lives
in **tool cards** -- see §Skills layer below.

### Skills layer

A "skill" is any chunk of procedural guidance the agent reads on
demand. The umbrella keeps the cold system prompt small as the toolkit
grows; the current prompt already carries multi-paragraph usage rules
per tool, and adding more tools the same way dilutes every rule's
attention weight (the documented failure mode behind the chess
hallucinations the tools and prompt grounding aim to prevent).

Not to be confused with Anthropic Agent Skills (the product feature).
Same underlying principle (lazy-load on demand), different unit,
trigger, and loader.

#### Tool cards (v1)

A tool card is a multi-line string of post-call usage guidance attached
to a `ToolSpec`, distinct from the `description` field (which the
model sees every turn to decide whether to invoke).

| Lives in `description` (cold prompt, every turn) | Lives in card (lazy, on first call this turn) |
|---|---|
| One-sentence purpose | Output interpretation rules |
| Gating rule -- when to call, when not | Edge cases, common mistakes |
| Anything needed to decide whether to invoke | Worked examples, mode-specific nuance |

Descriptions may absorb gating rules from `SYSTEM_PROMPT_RULES`;
gating must stay in the cold prompt because the card never loads
until after a call.

**Storage.** Inline string on the `ToolSpec` (no separate files).
Tools are registered statically; one place reads the tool's full
contract.

**Injection.** On the first call to a tool in a given turn, the
coordinator appends the card as a separate `{type:"text"}` content
block *inside* the `tool_result` user message:

```
assistant: [tool_use validate_move ...]
user:      [
             {type:"tool_result", content: <data>},
             {type:"text", text: <card>},   # first use only
           ]
```

Subsequent calls to the same tool in the same turn do not re-inject.
A per-turn `cards_injected: set[str]` tracks this in the `run()`
scope (no cross-turn state). Rationale for the separate content block
(vs string-prefix on `tool_result`, vs a separate user message):
keeps `tool_result.content` as pure tool output (clean for
transcripts/replay), stays within one user message per assistant turn
(Anthropic alternation), composes cleanly when parallel tool use is
enabled later (N tool_results + N cards in one message), and Ollama
translation already splits mixed-content user messages correctly.

**Caching interaction.** Prompt caching is not wired up today (the
system block is sent with no `cache_control` markers). The card
placement is forward-compatible if it is added later: cards land deep
in the message list (after tool calls), so they would not invalidate a
cached system + initial-message prefix.

#### Future skills (not v1)

- **Position-type playbooks** -- procedural chess wisdom for known
  position types (IQP middlegame, Lucena/Philidor endgame, typical
  pawn structures), injected when a cheap server-side classifier
  matches the live position. More valuable for weaker local models
  (Ollama 7-14B) than Claude. Open cost: playbook content must be
  hand-authored / curated from chess literature.
- **Opening-repertoire hints** -- once a known opening is identified
  (we already pass `opening_name`/`opening_eco`), inject
  opening-specific motifs. Same authoring-cost caveat.
- **Mode-specific procedures** -- post-game commentator mode could
  load a card on critical-moment selection that coach mode does not.

These share the lazy-load principle but trigger on signals other than
tool invocation. The harness needs a more general "skill manager"
once the second axis lands; until then a `set[str]` inside `run()` is
enough.

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

Three supported providers, all pluggable behind a common abstraction:

- Anthropic (native tool use)
- Ollama (OpenAI-compatible tool format; translation layer)
- Gemini (OpenAI-compatible SSE against Google's endpoint; Bearer key)

Common interface modeled on a `generate_thinking_stream_with_tools()`
shape: streaming response, tool-use loop, tool_result blocks fed back as
user messages. Anthropic format is canonical; Ollama provider translates
to OpenAI function-call format on the wire.

**Canonical wire shape = Anthropic.** `ToolRegistry.schemas()` exports
`{name, description, input_schema}` (the Anthropic shape) and that is
what providers receive in `tools=[...]`. Ollama translates to OpenAI's
function-call shape on the wire (same translation applies to `tool_use`
chunks and `tool_result` blocks). Alternative considered and rejected:
a neutral internal shape each provider serializes -- extra code with
no payoff while Anthropic is the lead provider and we want zero
translation cost on the happy path.

**Loop ownership: coordinator, not provider.** Provider surface is
`stream(system, messages, tools=None) -> AsyncIterator[ProviderChunk]`
-- one HTTP round per call, no awareness of tool execution or
multi-turn assembly. The coordinator owns tool registry + dispatch,
message accumulation across rounds, cancellation, and budget
enforcement. Alternatives considered: *provider owns the loop*
(cluesmith-style) hides too much in the provider and every new provider
re-implements it; *bidirectional single-HTTP stream* saves connection
setup and unlocks parallel tool use naturally but bigger provider
surface, harder cancellation, and Ollama needs a translation shim.
Accepted cost: one HTTP/TLS connection per round (pool later if
warranted).

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
- **Per-provider model memory:** server stores `ai_models: dict[str, str]`
  keyed by provider name; the active `ai_model` is a computed property
  reading `ai_models[ai_provider]`. Flipping providers in the Settings
  dialog restores the previously-selected model for the new provider
  (or empty when never set). Adding a new provider does not bump the
  persistence schema -- the dict gains a key at runtime.
- All numeric tunables: named module constants, env-var override (SV_
  prefix), optional UI exposure

## Triggers & Modes

| Mode | Game state | Button action | Path |
|------|-----------|---------------|------|
| play | in progress | live coach commentary (one-shot per click) | 1 |
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

**Indefinitely postponed.** Each Analyze click is a fresh one-shot
turn (cold system prompt + initial user message, no carry-over). The
rolling-session design below is recorded for posterity; client/server
code MUST NOT assume any of it.

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

### Ribbon buttons

- **No new buttons.** Existing `#analyze` (play mode) and
  `#view-analyze` (view mode) in `web/app/perspectives/play.js` are
  reused as-is.
- **One Settings toggle** ("Use AI analysis") selects engine-only vs.
  engine+AI behavior. Off = today's behavior unchanged. On = engine +
  AI (engine remains source of truth).
- **Mode/state still picks the path** (1 = play in-progress, 2 = view
  navigating, 3 = post-game / analyze-all). Same button, different
  downstream pipeline per (mode, game_state).

### Settings tab "Analysis"

Flat layout (the master toggle is described in §Ribbon buttons above):
- Provider (Anthropic / Ollama / Gemini)
- Model (free-form or dropdown TBD per provider)
- Provider-specific credentials:
  - Anthropic: API key
  - Gemini: API key (Bearer; base URL fixed to Google's endpoint)
  - Ollama: base URL (no key)
- Tunables surfaced as they prove necessary (tool call cap, analyze
  max depth, temperature, etc.). No collapsible / Advanced grouping;
  each lives flat in the panel.

## Guardrails

- Per-move token cap (env + UI)
- Per-game token cap (env + UI)
- Live (paths 1, 2): per-move cap per invocation; per-game cap also
  applies (rolling session accumulates cost across clicks)
- View/post-game (path 3): min(per_move, per_game / remaining_plies)
- Tool call cap per agent turn (env)
- `analyze` per-call hard cap on depth (env)
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
- `compare_moves` -- same per-candidate `searchmoves` shape as
  `top_moves`; not built. `top_moves` is shipped.
- Engine pool for `analyze` calls
- Wall-clock timeout backstop
- Caching of post-game annotations (re-runs require explicit user
  Overwrite confirmation per the PGN write path)

## Open Items (decided at impl)

- Length target (try chess-magazine standard, iterate)
- Output structure (experiment, iterate)
- Cache key strategy if/when caching is added
- Backup format on re-run overwrite (.bak sibling file vs.
  `[OriginalComments]` PGN header vs. other)
- ~~AI panel exact placement / dimensions~~ -- docked alongside
  Search Lines / UCI Log via `createDockableWindow`.
- ~~Model dropdown vs free-form input per provider~~ -- dropdown via
  `/settings/ai/models`; free-text fallback on endpoint error.
- ~~Settings tab visuals~~ -- flat layout shipped; conditional reveal
  of API key vs Base URL by provider.
- ~~Exact prompt text~~ -- shipped (Tools block rendered from registry;
  STM hint, white-POV anchor, "use own knowledge", "plain text only").
  Iteration continues as evidence comes in.

