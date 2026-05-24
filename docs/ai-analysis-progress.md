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

- [ ] Lift `LLMProvider` base from cluesmith; adapt to project conventions
- [ ] Anthropic provider (streaming + tool use loop)
- [ ] Ollama provider (OpenAI-compatible translation)
- [ ] Server-side config storage (deep-merge defaults + persisted JSON)
- [ ] API key handling: env var (server) + keyring/file fallback (desktop)
- [ ] New websocket event kinds: `ai_info` (prose stream) +
      `ai_annotation` (per-ply structured record)
- [ ] Concurrency lock (1 AI analysis at a time)
- [ ] Cancellation plumbing (abort LLM stream)

Tests:
- Provider abstraction with mocked HTTP responses
- Anthropic and Ollama produce identical events for identical canned
  inputs
- API key never echoed back to UI
- Cancel during stream aborts cleanly

### Phase 1: Agent loop + first tool (TDD against scripted provider)

Goal: replace the no-op `coord.run()` with a real agent loop. One real
tool (`analyze`) proves the abstraction boundary. No network -- all
TDD'd against an extended scripted provider. Per spec
`§Testing Principles`, the LLM mock lives at the provider boundary, not
HTTP.

Slices are vertical: each is one red-green-commit cycle that ends with
the system demonstrably more capable.

#### Slice A: ScriptedProvider (extend the test double)

- [ ] Extend `ProviderChunk` with `tool_use_id`, `tool_name`, `tool_input`
      (declared deferred in `llm/base.py`; promote now)
- [ ] New `ScriptedProvider` taking a list of "turns" (each turn = list
      of chunks: text and/or tool_use). After each turn, awaits
      tool_results back, then emits next turn.
- [ ] Keep `CannedProvider` as the no-tool subset (Phase 0 tests stay
      green untouched).

Tests:
- Multi-turn scripted dialog produces expected chunk sequence
- tool_result feedback unblocks the next turn
- Provider cancellation drops the script cleanly

#### Slice B: Tool registry + dispatch

- [ ] New `server/sturddle_view/llm/tools.py`: `ToolSpec` dataclass +
      registry
- [ ] Tool callable signature: `async def (input, *, cancel_token) -> dict`
      (per-call cancel token so day-1 design tolerates parallel tool use
      even though v1 runs sequentially -- spec §Triggers, "Forward-looking")
- [ ] Coordinator extended: loop until provider yields no tool_use,
      dispatch tool_use via registry, feed tool_result back through the
      provider as the next turn.
- [ ] Results keyed by `tool_use_id` end-to-end; no shared mutable
      scratch between tool calls.

Tests:
- Tool dispatched with correct input shape
- Result fed back into next provider turn
- Cancel mid-tool propagates through token + aborts the agent loop
- Unknown tool name yields a structured error tool_result (no crash)

#### Slice C: `analyze` tool (first real capability)

- [ ] New `server/sturddle_view/play/tools_engine.py`:
      `analyze(fen, time_ms=None, depth=None)` -- throwaway engine via
      the existing `_spawn_engine()` pattern (lifted from
      `human_vs_engine._run_analysis`).
- [ ] Hard caps as named consts + `SV_` env vars (no magic numbers):
      `SV_AI_ANALYZE_MAX_TIME_MS`, `SV_AI_ANALYZE_MAX_DEPTH`.
- [ ] Stop sequence matches existing `_cancel_analysis`:
      UCI `stop` -> grace timeout -> transport close / kill.
- [ ] Out-of-range requests clamped (not rejected) so the agent never
      stalls on a guardrail.

Tests:
- Reuse existing engine fixtures (`make_fake_uci`)
- Returns deterministic eval payload for a known position
- Honors caller's `time_ms` and the hard cap
- Cancel kills the process; no orphaned engine after the test
- Reentrancy: two `analyze` calls in one agent turn don't share state

Remaining tools (deferred to a follow-up cycle; same registry, no new
plumbing required):
- [ ] `get_position(ply)`
- [ ] `get_pgn_range(from_ply, to_ply)`
- [ ] `tablebase_probe(fen)` -- wrap existing TablebaseProber
- [ ] `opening_lookup(fen)` -- wrap existing OpeningBook
- [ ] `compare_moves(fen, [moves])` -- wrapper around `analyze`

#### Slice D: System prompt + coach addendum

- [ ] New `server/sturddle_view/llm/prompts.py`: `SYSTEM_PROMPT`,
      `COACH_ADDENDUM` (path 1 persona).
- [ ] Coordinator passes assembled prompt to provider's `stream()`.
- [ ] Commentator/analyst addendum stubbed but unused (path 2/3 wires it
      in Phase 3).

Tests:
- Prompt assembly is byte-stable (so the future cache-key strategy
  isn't perturbed by silent format drift)
- Wrong-mode addendum never leaks into path 1 calls

#### Out of Phase 1 (explicit)

- Other tools beyond `analyze` (one tool proves the boundary)
- Real Anthropic / Ollama providers
- Rolling session model, token caps, persona switching across modes
- PGN context injection

### Phase 2: Agent + live path (path 1, play mode)

Live-first spike. Streaming/cancel/transport is the hard part; building
it first prevents under-design that path 3 would later force a rewrite.

- [ ] System prompt + coach mode addendum
- [ ] Agent runner: loop until done or budget exhausted
- [ ] Rolling agent session for game lifetime; reset on new-game /
      takeback past annotated ply / mode swap
- [ ] Per-move and per-game token caps (both apply live); tool call cap
- [ ] Live streaming to AI panel via `ai_info` events
- [ ] `ai_annotation` event shape stubbed (no-op persistence) so path 3
      doesn't surprise the transport layer
- [ ] Ribbon button wired for play mode
- [ ] Engine info pinned, AI prose scrolls

Tests:
- Canned LLM responses produce expected prose stream
- Budget exhaustion stops cleanly
- Live cancel (hard-stop) aborts engine + LLM together; partial prose
  preserved in panel
- Engine output still renders correctly during AI streaming
- Session reset on takeback past annotated ply

### Phase 3: Path 3 (post-game) + path 2 (live view)

Reuse Phase 2 plumbing; add per-ply structured emission, PGN write, and
view-mode trigger.

- [ ] Commentator/analyst mode addendum
- [ ] Full PGN + per-ply eval array in initial user message
- [ ] Structured per-ply annotations emitted via `ai_annotation`
- [ ] PGN persistence via shared `apply_comment` helper + `[Annotator]`
      tag
- [ ] Re-run confirm prompt (Overwrite / Cancel / Save backup)
- [ ] Ribbon button wired for view mode + finished play mode
- [ ] Live-during-view (path 2) reuses Phase 2 streaming

Tests:
- Canned LLM responses produce expected annotations
- PGN round-trip preserves annotations and `[Annotator]` metadata
- Overwrite prompt path: backup file created and original preserved
- Cancel mid-game aborts engine + LLM together

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
- [ ] Decide AI panel exact placement (pin engine, scroll AI - revisit)
- [ ] Iterate prompt at end of Phase 2 and again after Phase 3
- [ ] Add `feedback_no_magic_numbers` consts for every new env var
- [ ] Surface `SV_AI_MAX_TOOL_ROUNDS` in Settings > Analysis > Advanced
      (Phase 4): the tool-call cap belongs alongside the other token /
      time caps. Server-side const + env override already in place
      (`ai_analysis.MAX_TOOL_ROUNDS`).

## Bugs

Pre-existing / side-quest (filed during Slice C+ wire-up review):

- **`#view-analyze` not gated on engine presence.** Play perspective's
  `#analyze` button is implicitly gated (no game without an engine, so
  the button is unreachable) but the view-mode `#view-analyze` stays
  clickable with zero engines registered; clicking it then crashes the
  server-side analysis start. Independent of the AI feature -- existing
  bug. Fix: track `noEngine` state and disable the button.
- **Settings "Analysis" tab master toggle ungated.** With no engine
  registered the user can still flip "Use AI analysis" on; the toggle
  does nothing useful (no analyze button can fire it) but the UI
  pretends it's live. Either disable the toggle or surface an inline
  hint pointing to Engines tab.

Slice-C-deferred:

- **`analyze` mid-search cancel is not unit-tested.** The current cancel
  test pre-flips the cancel token before calling `analyze`; that proves
  the marker is surfaced and the engine winds down cleanly, but does
  NOT exercise interruption of an in-flight search. A real mid-search
  test needs a non-timer-based sync between "engine has started search"
  and "test flips the token" (e.g., a hook on the analysis tool that
  signals first-info-received). Add when designing Phase 2 streaming
  hooks -- those will need the same observation seam.

Slice-B-deferred:

- **Thinking-chunk handling in `_assistant_message` is wrong.** When the
  Anthropic provider emits `kind="thinking"` chunks, the coordinator
  collapses their text into the same `{type:"text"}` block as normal
  text. Anthropic's wire shape expects a separate
  `{"type": "thinking", "thinking": "..."}` block. Fix when wiring real
  Anthropic provider. Note: extended thinking is opt-in (a request
  param) -- we can ship Anthropic without it and add later, in which
  case this code path stays dormant.
- **Round-cap signal not surfaced in UI.** Server emits
  `ai_info {done: true, round_cap: true}` when the loop terminates on
  the guardrail; client currently only honors `done` + `cancelled`. UI
  toast / panel marker = Phase 5 (Error UX).
- **Tool registry isn't wired into `app.py`.** `create_app` passes only
  `(bus, provider)`, so the production coordinator has an empty
  registry and can't dispatch any tools. Real wire-up + the first tool
  (`analyze`) is Slice C.

Skeleton-deferred (filed during Phase 0 walking-skeleton review; fine for
the canned-provider spike, must land before the feature is user-facing):

- **AI panel renders all prose in one `<p>` node.** `appendAiDelta` in
  `web/app/play-ai-window.js` appends every delta as a text node into a
  single paragraph -- `\n\n` paragraph breaks won't render. Mirror the
  `play-commentary-window.js` split-on-`\n{2,}` approach when real prose
  starts flowing.
- **`api/ai.py` reaches into `app.state.hve` for `game_id`.** Couples the
  AI router to the play perspective. Replace with coordinator-owned
  session state (game_id pinned on `new_game`, cleared on takeback past
  annotated ply / mode swap) when the rolling-session model lands.
- **Provider-selection guard missing.** `AnthropicProvider` and
  `OllamaProvider` are NotImplementedError stubs. Today the coordinator
  hardcodes `CannedProvider` so user selection is dormant -- but the
  cycle that wires `s.ai_provider` -> provider construction must guard
  against unimplemented providers (or land at least one real one first)
  to avoid a runtime crash from the settings UI.

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
