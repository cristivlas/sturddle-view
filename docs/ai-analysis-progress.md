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
- [x] Anthropic provider (streaming + tool use loop) -- full SSE body
      shipped; `content_block_delta` -> text/thinking; tool_use accumulated
      across `input_json_delta`; mid-stream `error` events surfaced.
- [x] Ollama provider (OpenAI-compatible translation)
- [x] Server-side config storage (deep-merge defaults + persisted JSON)
- [x] API key handling: cross-platform OS keyring (Windows Credential
      Manager / macOS Keychain / Linux Secret Service) via `keyring`
      lib; per-provider account names; env-var fallback
      (`SV_AI_API_KEY`) for headless. See `key_store.py` and the
      manual-clear instructions in `ai-analysis-spec.md` §Configuration.
- [x] New websocket event kinds: `ai_info` (prose stream),
      `ai_thinking` (collapsible reasoning disclosure), and
      `ai_annotation` (per-ply structured record, unused until path 3).
- [x] Concurrency lock (1 AI analysis at a time)
- [x] Cancellation plumbing (abort LLM stream)

Tests:
- [x] Provider abstraction with mocked HTTP responses
      (`test_ollama_wire.py`, `test_anthropic_wire.py` -- shared shim).
- [x] Anthropic and Ollama produce equivalent chunks for the same
      canned inputs (text streaming, tool_use round-trip, thinking).
- [x] API key never echoed back to UI (GET returns masked sentinel).
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

Remaining tools (deferred; same registry, no new plumbing required):
- [~] `top_moves(n?, time_ms?, depth?)` -- BUILT but registration
      commented out in `app.py`. Per-candidate searches via
      `engine.analysis(..., root_moves=[m])` (UCI `searchmoves`);
      Sturddle ignores `searchmoves` so every candidate returns the
      same engine-best PV. Re-enable once the engine honors it.
- [ ] `get_position(ply)`  -- low priority: full PGN already in initial
      user message.
- [ ] `get_pgn_range(from_ply, to_ply)` -- low priority for same reason.
- [ ] `tablebase_probe()` -- wrap `TablebaseProber`; live board only.
      Register conditionally on `engine_default_syzygy_path` being set
      (no tool listed = no missing-syzygy error envelope to model).
- [ ] `opening_lookup()` -- wrap `OpeningBook.lookup()`; live board only.
- [ ] `compare_moves(fen, [moves])` -- wrapper around `analyze`; same
      `searchmoves` blocker as `top_moves`.

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
- ~~Real Anthropic provider~~ -- shipped (full streaming + tool use)
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

- [x] Analysis tab with provider/model/key/url (flat)
- [x] Conditional reveal for provider-specific fields
      (`applyAiProviderVisibility`: API key for Anthropic, Base URL
      for Ollama)
- [x] Master "Use AI analysis" toggle locks out child fields when off
      (`applyAiEnabledLockout`)
- [x] Per-provider model memory: server holds
      `ai_models: dict[str, str]` keyed by provider; flipping provider
      restores its last-selected model.
- [x] Model dropdown via `GET /settings/ai/models` (Anthropic + Ollama
      live endpoints); free-text fallback on provider error with the
      server's `detail` surfaced inline.
- [x] Masked key with replace-on-type flow (placeholder "Saved -- enter
      new to replace"; sentinel `***` on the wire means "no change").
- [ ] Advanced collapsible: caps + tunables
      (`SV_AI_MAX_TOOL_ROUNDS`, `SV_AI_ANALYZE_MAX_*`, token caps
      pending).
- [ ] Effective config display (env-derived vs user-set).

Tests:
- [x] Settings round-trip (save, reload, persist).
- [x] Per-provider model memory survives provider flips
      (`test_settings_api.py::test_ai_model_remembered_per_provider`).
- [x] Per-provider keyring storage isolated from real OS keyring in
      tests via autouse `_isolate_keyring` fixture.
- [x] API key never echoed in full; GET returns mask when set.
- [ ] Layout fits in existing dialog without overflow
      (manual; hint slot pinned to fixed height to avoid reflow).

### Phase 5: Error UX

- [x] Server attaches `error_detail` to the terminal `ai_info` event;
      uses upstream `{"error": {"message": ...}}` field when available
      (via `llm/_errors.py::extract_error_message`).
- [x] Client renders the failure inline in the AI panel
      (`play-ai-error` block) AND as a toast; user sees both the
      what-broke + the where-to-look.
- [x] Round-cap signal: `done.round_cap=true` -> "Stopped early at the
      tool-call cap" inline note.
- [x] Reasoning-only / no-text signal: `done.no_response=true` ->
      "Model produced no answer. Try a different model" inline note.
- [x] Settings dialog inline hint for AI-models endpoint failures
      (key-not-configured, network, 4xx upstream): server `detail`
      surfaced verbatim under the Model field; slot height pinned so
      hint appearance does not reflow the dialog.
- [ ] Category mapping table (auth / rate-limit / network / refusal):
      today every error reaches the user as raw text. Categories would
      let the toast suggest a specific action ("Open Settings" for
      auth, "Retry" for rate limit). Park until evidence shows it
      matters.

Tests:
- [x] `error_detail` truncation cap pinned
      (`test_agent_runner.py::test_error_detail_truncated_to_cap`).
- [x] Provider error with structured detail flows through
      (`test_provider_error_publishes_done_with_error_kind_and_detail`).
- [x] `no_response` flagged on reasoning-only round
      (`test_no_response_flagged_when_round_ends_without_text`).

## Future cleanups (no rush; capture so we don't forget)

- **Consolidate `EngineSupervisor.spawn()` and `spawn_throwaway()`** when
  a third caller appears. Today they're honestly different (the former
  binds to the supervisor's long-lived `_engine`/`_transport`; the
  latter returns a self-contained `(engine, cleanup)` so multiple
  concurrent one-shots from the same supervisor are safe). A shared
  `_spawn_impl(...)` returning `(engine, transport, cleanup)` is the
  obvious factor-out -- but doing it now is premature (two callers).
  Worth ~30 lines of near-duplication in `spawn_throwaway` until then.

- **`analyze` tool depth floor.** Models occasionally pass a low `depth`
  to the `analyze` tool (e.g. 8, 10) which produces noisy bestmoves
  that then surface in coach prose as bad recommendations. The default
  is now 20 (was 16), but the model can still override downward.
  Mitigations to consider, smallest to largest:
  1. **Prompt nudge** in the `analyze` tool's description or card:
     "minimum depth 20 for live coach use; lower values are noisy."
     Cheap; effectiveness varies by model.
  2. **Server-side floor** in `_clamp_limits`: `d = max(MIN_DEPTH, ...)`
     with `MIN_DEPTH=20`. Deterministic; takes the decision out of the
     model's hands. Risk: makes `top_moves` (when re-enabled) slow if
     it inherits the same floor; want a separate per-tool floor.
  3. **Both**: prompt nudge + server floor as defense in depth.
  Defer until we see a concrete failure-mode change post the recent
  default bump.

## Todos (cross-cutting)

- [x] Decide model dropdown vs free-form per provider -- dropdown via
      `/settings/ai/models` for both; free-text fallback on endpoint
      error with the server's `detail` surfaced inline.
- [x] Decide AI panel exact placement (docked alongside Search Lines /
      UCI Log via `createDockableWindow`).
- [x] Iterate prompt (STM hint, white-POV anchor, tools-from-registry,
      "use own knowledge", "plain text only"). Further iteration after
      path 3 lands.
- [x] `feedback_no_magic_numbers` consts for every new env var
      (`SV_AI_MAX_TOOL_ROUNDS`, `SV_AI_ANALYZE_MAX_TIME_MS`,
      `SV_AI_ANALYZE_MAX_DEPTH`, `SV_AI_TOP_MOVES_MAX_N`,
      `SV_AI_TRANSCRIPT`, `SV_AI_DEBUG`)
- [ ] Surface `SV_AI_MAX_TOOL_ROUNDS` in Settings > Analysis > Advanced
      (Phase 4): the tool-call cap belongs alongside the other token /
      time caps. Server-side const + env override already in place
      (`ai_analysis.MAX_TOOL_ROUNDS`).

## Bugs

Open:

- **`api/_ai_kick.py` will fold into the coordinator when rolling
  sessions land.** Today it reads HVE state via public accessors
  (`current_board()`, `start_fen()`, `view_full_moves_san()`,
  `pre_analysis_mode()`) -- no underscore reach-through -- but the
  start-of-turn assembly itself belongs in the session, not on the
  API boundary.
- **`top_moves` blocked on engine `searchmoves` support.** See
  `Open items (2026-05-25)` below. Registration is commented out.
- **MD / LaTeX leakage in some models' prose.** Prompt now says
  "plain text only". If leakage persists with that guidance,
  client-side regex strip (`**`, `*`, `_`, `$...$`) is a fallback.
  Park until evidence warrants.
Fixed (kept here as a record):

- ~~AI prose streams server-side but appears bursty in the UI~~ --
  spinner + thinking disclosure addressed the perceived "stuck panel"
  UX; remaining burstiness is a non-issue.
- ~~Thinking-chunk handling in `_assistant_message` is wrong~~ --
  Anthropic provider now emits separate thinking chunks; coordinator
  surfaces them as `ai_thinking` events; client renders them in a
  sticky-pref `<details>` disclosure.
- ~~Round-cap signal not surfaced in UI~~ -- inline `play-ai-roundcap`
  note + `done.round_cap` flow fully wired.
- ~~AI panel renders all prose in one `<p>` node, dropping line
  breaks~~ -- `.play-ai-prose` is now `white-space: pre-wrap`; model
  newlines render verbatim.
- ~~`analyze` mid-search cancel is not unit-tested~~ --
  `pump_engine_info` now races the analysis iterator against the cancel
  token's wait_cancelled event (no more "stuck waiting on next info"
  on an idle engine) and exposes `first_info_event` so a test can
  deterministically observe "search in flight" before flipping cancel.
  See `test_engine_info_pump.py::test_pump_cancel_after_first_info_returns_cancelled`.
- ~~`api/_ai_kick.py` reached into HVE underscore attributes~~ -- HVE
  now exposes `current_board()`, `start_fen()`, `view_full_moves_san()`,
  `pre_analysis_mode()`; _ai_kick uses those.
- ~~Tool registry isn't wired into `app.py`~~ -- `ANALYZE_TOOL_SPEC`
  registered in `create_app`. Prompt's Tools: section is now rendered
  by iterating the registry (no manual sync between schema and prompt
  text); see `prompts.py::_render_tools_block`.
- ~~Anthropic provider was a stub~~ -- full streaming body shipped:
  text + thinking + tool_use accumulation; SSE error events surfaced.
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
- ~~Provider switch wiped the model id~~ -- server now keeps
  per-provider model memory in `ai_models: dict[str, str]`; flipping
  back restores the previously selected model.
- ~~Settings dialog hint reflowed on appearance~~ -- pinned slot
  height + ellipsis on overflow.

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
    `agent_annotation` kind / `/agent/annotation` route were removed
    once AI work landed -- they had zero consumers.
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

## Open items (2026-05-25)

State of the work after the prompt-revision + per-provider memory pass:

- **Rolling session: tabled.** Spec §Live session model and the
  per-game cap reasoning assume an agent session that persists across
  Analyze clicks. Today every click sends the whole PGN as the
  initial user message and runs an independent turn -- equivalent
  context, no shared state to invalidate. Implementing a real session
  would unlock prompt-caching savings + the "session reset on
  takeback past annotated ply" trigger, but adds rebuild logic for
  diverging move stacks. Park until evidence (cost or coherence)
  warrants it.
- **[HIGH PRIORITY] Token caps unimplemented.** Spec §Guardrails calls
  per-move and per-game token caps non-negotiable, but only the
  round-cap (`SV_AI_MAX_TOOL_ROUNDS`) and `analyze` time/depth caps
  ship today. With Anthropic now live, an unbounded loop is a billing
  risk. Wire-up:
  - Anthropic returns `usage` (`input_tokens`, `output_tokens`,
    `cache_*`) in `message_delta` / `message_stop` SSE events.
  - Ollama returns `prompt_eval_count` + `eval_count` at stream end.
  - Coordinator tracks per-turn + cumulative-per-game; loop stops when
    either cap is hit; surface `done.token_cap=true` to the UI.
  - Settings UI exposure under Advanced (Phase 4).


- **top_moves disabled.** Built and tested in isolation, but registration
  is commented out in `app.py`. Relies on UCI `searchmoves` (python-chess
  `root_moves` kwarg) to restrict each per-candidate search; Sturddle
  appears to ignore it (every candidate returns the same engine-best PV).
  Re-enable once the engine honors `searchmoves` (engine-side change).
- **Output format.** Some models leak Markdown / LaTeX into prose. Prompt
  now says "plain text only"; if leakage persists, options are (a) strip
  client-side via small regex (`**`, `*`, `_`, math wrappers), or
  (b) ship a tiny renderer that interprets MD. Park until evidence
  warrants.
- **Spec-vs-code reconciliation.** Tools shipped today: `analyze` only
  (`top_moves` defined but disabled; `compare_moves`, `tablebase_probe`,
  `opening_lookup`, `get_position`, `get_pgn_range` not built). Spec
  §Tools lists six; this section now reflects reality.
- **Per-provider model memory.** Server stores `ai_models: dict[str, str]`
  keyed by provider; `ai_model` is a computed property. Adding a new
  provider does not bump the persistence schema.
