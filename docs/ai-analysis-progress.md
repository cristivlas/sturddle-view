# AI Analysis & Commentary - Implementation Progress

Companion to `ai-analysis-spec.md`. Tracks phases, todos, status, bugs,
and decisions taken during implementation. Spec stays stable; this doc
churns.

## Status Legend

- [ ] not started
- [~] in progress
- [x] done
- [!] blocked
- [-] dropped

## Implementation Methodology

TDD with a walking skeleton.

- **Walking skeleton first.** Day one: every layer end-to-end with the
  shallowest possible stand-in at each — canned-response provider, no-op
  agent loop, real websocket transport, real UI panel rendering a
  hardcoded stream. No real LLM, no tools, no PGN context. The goal is
  to exercise contracts between layers before any layer has substance.
- **Then deepen layer-by-layer.** Each subsequent change replaces one
  stand-in with the real thing (or adds one capability behind the same
  contract): red test -> impl -> green -> commit. One Phase 0/1/2 item
  per cycle.
- **Stand-ins are first-class, not throwaway.** A canned provider, a
  no-op tool, a stub event handler all stay in the repo as test
  doubles. They harden the abstraction boundary and keep tests fast and
  offline.
- **Boundaries align with spec §Testing Principles.** Mock at the
  provider abstraction (not HTTP); reuse engine test infra for the
  `analyze` tool; canned LLM + canned engine for agent tests.

## Phasing Proposal

Phases are roughly independent vertical slices. Each ends with something
demonstrable. Refine as we go.

### Phase 0: Foundation

Wire the abstraction layer without any UI or agent intelligence. Goal: a
canned response can flow end-to-end.

- [x] Lift `LLMProvider` base from cluesmith; adapt to project conventions
- [!] Anthropic provider (streaming + tool use loop) -- stub only;
      constructor signature locked, body raises NotImplementedError
- [x] Ollama provider (OpenAI-compatible translation)
- [x] Server-side config storage (deep-merge defaults + persisted JSON)
- [x] API key handling: env var (server) + keyring/file fallback (desktop)
- [x] New websocket event kinds: `ai_info` (prose stream) +
      `ai_annotation` (per-ply structured record) -- `ai_info` shipped;
      `ai_annotation` declared but unused until path 3 lands
- [x] Concurrency lock (1 AI analysis at a time)
- [x] Cancellation plumbing (abort LLM stream)

Tests:
- [x] Provider abstraction with mocked HTTP responses
- [-] Anthropic and Ollama produce identical events for identical canned
      inputs -- moot until Anthropic body lands
- [x] API key never echoed back to UI
- [x] Cancel during stream aborts cleanly

### Phase 1: Agent loop + first tool (TDD against scripted provider)

Goal: replace the no-op `coord.run()` with a real agent loop. One real
tool (`analyze`) proves the abstraction boundary. No network -- all
TDD'd against an extended scripted provider. Per spec
`§Testing Principles`, the LLM mock lives at the provider boundary, not
HTTP.

Slices are vertical: each is one red-green-commit cycle that ends with
the system demonstrably more capable.

#### Slice A: ScriptedProvider (extend the test double)

- [x] Extend `ProviderChunk` with `tool_use_id`, `tool_name`, `tool_input`
- [x] New `ScriptedProvider` taking a list of "turns" (each turn = list
      of chunks: text and/or tool_use). After each turn, awaits
      tool_results back, then emits next turn.
- [x] Keep `CannedProvider` as the no-tool subset.

Tests:
- [x] Multi-turn scripted dialog produces expected chunk sequence
- [x] tool_result feedback unblocks the next turn
- [x] Provider cancellation drops the script cleanly

#### Slice B: Tool registry + dispatch

- [x] New `server/sturddle_view/llm/tools.py`: `ToolSpec` dataclass +
      registry
- [x] Tool callable signature: `async def (input, *, cancel_token) -> dict`
- [x] Coordinator extended: loop until provider yields no tool_use,
      dispatch tool_use via registry, feed tool_result back through the
      provider as the next turn.
- [x] Results keyed by `tool_use_id` end-to-end; no shared mutable
      scratch between tool calls.

Tests:
- [x] Tool dispatched with correct input shape
- [x] Result fed back into next provider turn
- [x] Cancel mid-tool propagates through token + aborts the agent loop
- [x] Unknown tool name yields a structured error tool_result (no crash)

#### Slice C: `analyze` tool (first real capability)

- [x] New `server/sturddle_view/play/tools_engine.py`:
      `analyze(fen, time_ms=None, depth=None)` -- throwaway engine via
      the existing `_spawn_engine()` pattern.
- [x] Hard caps as named consts + `SV_` env vars:
      `SV_AI_ANALYZE_MAX_TIME_MS`, `SV_AI_ANALYZE_MAX_DEPTH`.
- [x] Stop sequence matches existing `_cancel_analysis`:
      UCI `stop` -> grace timeout -> transport close / kill.
- [x] Out-of-range requests clamped (not rejected) so the agent never
      stalls on a guardrail.
- [x] Tool publishes `engine_info` + `engine_search_start` events to the
      bus via the shared `pump_engine_info` helper -- PV table + board
      arrow fill bursty during tool calls. (Refinement landed after
      Slice C: AI mode was running a parallel go-infinite engine; that
      gate now lives in HVE's start_analysis.)
- [x] `score_pawns` + `score_text` accompany `score_cp` so the LLM can
      drop in a presentation string without doing centipawn math.

Tests:
- [x] Reuse existing engine fixtures (`make_searching_fake_uci`)
- [x] Returns deterministic eval payload for a known position
- [x] Honors caller's `time_ms` and the hard cap
- [x] Cancel kills the process; no orphaned engine after the test
- [x] Tool publishes engine_info to the bus (PV table feeder)
- [x] `_score_to_cp` wire-shape branches pinned

Remaining tools (deferred to a follow-up cycle; same registry, no new
plumbing required):
- [ ] `get_position(ply)`
- [ ] `get_pgn_range(from_ply, to_ply)`
- [ ] `tablebase_probe(fen)` -- wrap existing TablebaseProber
- [ ] `opening_lookup(fen)` -- wrap existing OpeningBook
- [ ] `compare_moves(fen, [moves])` -- wrapper around `analyze`

#### Slice D: System prompt + coach addendum

- [x] New `server/sturddle_view/llm/prompts.py`: `SYSTEM_PROMPT`,
      `COACH_ADDENDUM` (path 1 persona), `COMMENTATOR_ADDENDUM` (path 2/3).
- [x] Coordinator passes assembled prompt to provider's `stream()`.
- [x] Commentator addendum WIRED (Refinement 3): view-mode analysis
      flips persona via `_pre_analysis_mode` read in `_ai_kick.py`.
- [x] User message carries `Current position (FEN)` + `Game moves`
      (full game in view mode, played-so-far in play mode).

Tests:
- [x] Prompt assembly is byte-stable
- [x] Wrong-mode addendum never leaks into path 1 calls
- [x] User-message build pulls FEN + SAN from HVE board / view state
- [x] Persona selection (coach vs commentator) routes by origin mode

#### Out of Phase 1 (explicit)

- Other tools beyond `analyze` (one tool proves the boundary)
- ~~Real Ollama provider~~ -- shipped
- ~~Real Anthropic provider~~ -- still pending (stub only)
- Rolling session model, token caps
- Per-ply eval array injection (path 3 only)

### Phase 2: Agent + live path (path 1, play mode)

Live-first spike. Streaming/cancel/transport is the hard part; building
it first prevents under-design that path 3 would later force a rewrite.

- [x] System prompt + coach mode addendum
- [x] Agent runner: loop until done or budget exhausted (round cap only;
      token caps pending)
- [~] Rolling agent session for game lifetime -- not shipped; today is
      one click = one turn. Session reset triggers (new-game, takeback
      past annotated ply, mode swap) wait on rolling-session work.
- [ ] Per-move and per-game token caps (both apply live); tool call cap
      (tool call cap exists via `SV_AI_MAX_TOOL_ROUNDS`; token caps not
      wired)
- [x] Live streaming to AI panel via `ai_info` events
- [~] `ai_annotation` event shape declared; persistence is no-op until
      path 3 lands
- [x] Ribbon button wired for play mode (no new button -- existing
      Analyze button branches server-side on `settings.ai_enabled`)
- [x] Engine info pinned, AI prose scrolls (PV table + arrow fill
      bursty from `analyze` tool calls via shared `pump_engine_info`)

Tests:
- [x] Canned LLM responses produce expected prose stream
- [-] Budget exhaustion stops cleanly (round cap tested; token caps
      pending)
- [~] Live cancel (hard-stop) aborts LLM; engine winds down via tool's
      cancel_token. No parallel go-infinite to abort in AI mode.
- [x] Engine output still renders correctly during AI streaming
- [ ] Session reset on takeback past annotated ply

### Phase 3: Path 3 (post-game) + path 2 (live view)

Reuse Phase 2 plumbing; add per-ply structured emission, PGN write, and
view-mode trigger.

- [x] Commentator/analyst mode addendum
- [~] Full PGN in initial user message (view mode) -- shipped via
      `Game moves` field. Per-ply eval array NOT shipped; engine evals
      reach the model only through `analyze` tool calls.
- [ ] Structured per-ply annotations emitted via `ai_annotation`
- [ ] PGN persistence via shared `apply_comment` helper + `[Annotator]`
      tag
- [ ] Re-run confirm prompt (Overwrite / Cancel / Save backup)
- [x] Ribbon button wired for view mode (existing view-Analyze button
      now routes through the same server-side branch)
- [x] Live-during-view (path 2) reuses path-1 streaming

Tests:
- [ ] Canned LLM responses produce expected annotations
- [ ] PGN round-trip preserves annotations and `[Annotator]` metadata
- [ ] Overwrite prompt path: backup file created and original preserved
- [x] Cancel mid-turn aborts cleanly (engine via tool cancel_token,
      LLM via coordinator.cancel())

### Phase 4: Settings UI

- [ ] Analysis tab with provider/model/key/url (flat)
- [ ] Conditional reveal for provider-specific fields
- [ ] Advanced collapsible: caps + tunables
- [ ] Masked key with "Update" flow
- [ ] Effective config display (env-derived vs user-set)

Tests:
- Settings round-trip (save, reload, persist)
- Key update flow doesn't expose existing key
- Layout fits in existing dialog without overflow

### Phase 5: Error UX

- [ ] Error categorization on server
- [ ] Toast mapping on client
- [ ] No silent failure audit (every error path tested)

Tests:
- Each error category produces correct toast
- Server logs contain full detail when toast is generic

## Future cleanups (no rush; capture so we don't forget)

- **Consolidate `EngineSupervisor.spawn()` and `spawn_throwaway()`** when
  a third caller appears. Today they're honestly different (the former
  binds to the supervisor's long-lived `_engine`/`_transport`; the
  latter returns a self-contained `(engine, cleanup)` so multiple
  concurrent one-shots from the same supervisor are safe). A shared
  `_spawn_impl(...)` returning `(engine, transport, cleanup)` is the
  obvious factor-out -- but doing it now is premature (two callers).
  Worth ~30 lines of near-duplication in `spawn_throwaway` until then.

## Todos (cross-cutting)

- [ ] Decide model dropdown vs free-form per provider
- [x] Decide AI panel exact placement (docked alongside Search Lines /
      UCI Log via `createDockableWindow`)
- [~] Iterate prompt (Slice D first cut shipped; iterate after path 3
      lands)
- [x] `feedback_no_magic_numbers` consts for every new env var
      (`SV_AI_MAX_TOOL_ROUNDS`, `SV_AI_ANALYZE_MAX_TIME_MS`,
      `SV_AI_ANALYZE_MAX_DEPTH`, `SV_AI_TRANSCRIPT`, `SV_AI_DEBUG`)
- [ ] Surface `SV_AI_MAX_TOOL_ROUNDS` in Settings > Analysis > Advanced
      (Phase 4): the tool-call cap belongs alongside the other token /
      time caps. Server-side const + env override already in place
      (`ai_analysis.MAX_TOOL_ROUNDS`).

## Bugs

Open:

- **AI prose streams server-side but appears bursty in the UI.** The
  transcript file shows real per-token streaming on the bus (~10ms
  between chunks). Downstream of `Event.publish`, either the WebSocket
  layer or `appendAiDelta` is coalescing. Parked refinement;
  diagnose with Chrome devtools WS tab and inspect
  `web/app/play-ai-window.js` `appendAiDelta`.
- **`analyze` mid-search cancel is not unit-tested.** The current
  cancel test pre-flips the cancel token before calling `analyze`;
  that proves the marker is surfaced and the engine winds down cleanly,
  but does NOT exercise interruption of an in-flight search. A real
  mid-search test needs a non-timer-based sync between "engine has
  started search" and "test flips the token" (e.g., a hook on the
  analysis tool that signals first-info-received).
- **Thinking-chunk handling in `_assistant_message` is wrong.** When a
  provider emits `kind="thinking"` chunks, the coordinator collapses
  their text into the same `{type:"text"}` block as normal text.
  Anthropic's wire shape expects a separate `{"type": "thinking",
  "thinking": "..."}` block. Fix when wiring real Anthropic provider.
  Extended thinking is opt-in -- can ship Anthropic without it and
  add later, in which case this code path stays dormant.
- **Round-cap signal not surfaced in UI.** Server emits
  `ai_info {done: true, round_cap: true}` when the loop terminates on
  the guardrail; client currently only honors `done` + `cancelled`. UI
  toast / panel marker = Phase 5 (Error UX).
- **AI panel renders all prose in one `<p>` node.** `appendAiDelta` in
  `web/app/play-ai-window.js` appends every delta as a text node into a
  single paragraph -- `\n\n` paragraph breaks won't render. Mirror the
  `play-commentary-window.js` split-on-`\n{2,}` approach.
- **`api/_ai_kick.py` reaches into `hve._board` / `_start_fen` /
  `_view_full_moves` / `_pre_analysis_mode` directly.** Couples the AI
  start path to play-perspective internals. Replace with
  coordinator-owned session state when the rolling-session model
  lands. (Was `api/ai.py`; same shortcut, renamed file.)

Fixed (kept here as a record):

- ~~Tool registry isn't wired into `app.py`~~ -- fixed; `ANALYZE_TOOL_SPEC`
  registered in `create_app`.
- ~~Provider-selection guard missing~~ -- Ollama provider shipped;
  Anthropic still a stub (selecting it from Settings raises
  NotImplementedError, which falls out to a coordinator error event
  rather than a runtime crash).
- ~~Two divergent engine_info loops~~ -- HVE's `_pump_engine_info` and
  the `analyze` tool both walk an analysis stream; both now go through
  `pump_engine_info` in `play/engine_info_pump.py`. PV table + arrow
  fill correctly in AI mode.
- ~~Always-on `go infinite` competes with AI's tool engine~~ -- HVE's
  `start_analysis` skips `_run_analysis` when `settings.ai_enabled`.
- ~~`/ai/start` + `/ai/cancel` were separate client calls~~ -- folded
  into `/game/analysis/start` + `/game/analysis/stop`; server picks the
  path. Client posts to one endpoint.
- ~~View-mode AI gated client-side~~ -- view mode now opens the AI
  panel + the server picks commentator persona via
  `_pre_analysis_mode`.
- ~~LLM treated `score_cp` as pawns (100x misread)~~ -- tool result
  carries `score_cp` + `score_pawns` + `score_text` for unambiguous
  consumption.
- ~~Ollama silently swallowed malformed JSON~~ -- SSE payloads and
  tool-call arguments now raise (with raw bytes in transcript).

## Decisions taken during impl

NOTE: these decisions live here while they're still fresh / negotiable.
Once a feature ships and a decision proves load-bearing across multiple
phases, lift it into `ai-analysis-spec.md` so the spec stays the
authoritative architecture record.

- **Canonical tool wire shape = Anthropic** (decided 2026-05-23, Phase 1
  Slice B; ratifies spec §Providers).
  - `ToolRegistry.schemas()` exports
    `{name, description, input_schema}` -- the Anthropic shape -- and
    that's what providers receive in `tools=[...]`.
  - Ollama provider is responsible for translating to OpenAI's
    function-call shape
    (`{"type": "function", "function": {name, description, parameters}}`)
    on the wire. Same translation applies in both directions to
    `tool_use` chunks and `tool_result` blocks.
  - Alternative (neutral internal shape, each provider serializes) was
    rejected: extra code with no payoff while Anthropic is the lead
    provider and we want zero translation cost on the happy path.
- **Multi-round agent loop lives in the runner, not the provider**
  (decided 2026-05-23, Phase 1 Slice A).
  - Provider surface: `stream(system, messages, tools=None) -> AsyncIterator[ProviderChunk]`.
    One HTTP round per call. Provider knows nothing about tool execution
    or multi-turn assembly.
  - Coordinator (runner) owns: tool registry + dispatch, message
    accumulation across rounds, cancellation, budget enforcement.
  - Alternatives considered:
    - *Provider owns the loop* (cluesmith-style): hides too much in the
      provider, harder to test agent logic without a real provider, and
      every new provider re-implements the loop.
    - *Bidirectional stream (one open HTTP request across the loop)*:
      saves connection setup per round and unlocks parallel tool use
      naturally, but bigger provider surface, harder cancellation, and
      Ollama would need a translation layer to fake it. Reconsider if
      latency or token-overhead measurements push us there.
  - Accepted cost: one HTTP/TLS connection per round (mitigated later
    with connection pooling if needed).
- Spike order: live (path 1) first, then post-game/view (paths 3, 2).
  Streaming + cancel + session model are the hardest pieces and want
  early iteration. Path 3 layers per-ply emission and PGN write on top.
- `apply_comment` helper extraction may land on `main` first and rebase
  into this branch; treat as a soft dependency, not a blocker.
- File placement (locked):
  - **Server coordinator:** new `server/sturddle_view/play/ai_analysis.py`
    holds the `AIAnalysisCoordinator` class; composed by the play
    perspective alongside `HumanVsEngine` (not methods on it). Mirrors
    `TablebaseProber` / `OpeningBook` siblings.
  - **Server providers:** new package `server/sturddle_view/llm/` with
    `base.py`, `anthropic.py`, `ollama.py`. Lifted/adapted from
    cluesmith.
  - **Events:** add `ai_info` (prose stream) and `ai_annotation`
    (per-ply record) to `events.py` `EventKind`. The pre-existing
    `agent_annotation` kind is *not* reused — it serves external REST
    agents (`api/agent.py`) with a different lifecycle.
  - **Client panel:** new `web/app/play-ai-window.js`, mirrors
    `play-commentary-window.js` (same `createDockableWindow` factory).
    `play.js` wires open/close + event subscription.
- Ribbon buttons (locked):
  - **No new buttons.** Existing `#analyze` (play mode) and
    `#view-analyze` (view mode) in `web/app/perspectives/play.js` are
    reused as-is.
  - **One Settings toggle** ("Use AI analysis" — exact label TBD)
    selects engine-only vs. engine+AI behavior. Off = today's behavior
    unchanged. On = engine + AI (engine remains source of truth).
  - **Mode/state still picks the path** (1 = play in-progress,
    2 = view navigating, 3 = post-game / analyze-all). Same button,
    different downstream pipeline per (mode, game_state).

## Notes

- Cluesmith reference: `C:\Users\crist\Projects\cluesmith\server\llm.py`
- Existing analysis pattern: `human_vs_engine.py:_run_analysis` (1673)
- Existing cancel pattern: `human_vs_engine.py:_cancel_analysis` (1464)
- Existing tablebase: `play/tablebase.py`
- Existing openings: `openings.py`
