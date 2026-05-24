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
- [ ] New websocket event kind (`ai_info` or similar)
- [ ] Concurrency lock (1 AI analysis at a time)
- [ ] Cancellation plumbing (abort LLM stream)

Tests:
- Provider abstraction with mocked HTTP responses
- Anthropic and Ollama produce identical events for identical canned
  inputs
- API key never echoed back to UI
- Cancel during stream aborts cleanly

### Phase 1: Tools

Implement the tool layer. Agent not yet integrated.

- [ ] `analyze(fen, time_ms, depth)` - throwaway engine, stop/grace/kill
- [ ] `get_position(ply)`
- [ ] `get_pgn_range(from_ply, to_ply)`
- [ ] `tablebase_probe(fen)` - wrap existing TablebaseProber
- [ ] `opening_lookup(fen)` - wrap existing OpeningBook
- [ ] `compare_moves(fen, [moves])`
- [ ] Server-side hard caps (env-var) for analyze time/depth
- [ ] Tool registry exposed to provider abstraction

Tests:
- Each tool standalone with deterministic inputs
- `analyze` honors limit and aborts cleanly on cancel
- Hard caps clamp out-of-range requests

### Phase 2: Agent + path 3 (post-game)

End-to-end agent flow for the simplest path (no live streaming pressure).

- [ ] System prompt + commentator/analyst mode addendum
- [ ] Agent runner: loop until done or budget exhausted
- [ ] Per-move and per-game token caps; tool call cap
- [ ] Structured annotations emitted
- [ ] PGN persistence (`{}` comments + `[Annotator]` tag)
- [ ] Re-run confirm prompt
- [ ] Ribbon button wired for view mode

Tests:
- Canned LLM responses produce expected annotations
- Budget exhaustion stops cleanly
- PGN round-trip preserves annotations and metadata
- Cancel mid-game aborts engine + LLM together

### Phase 3: Live paths (1, 2)

Add live-during-play and live-during-view paths.

- [ ] Coach mode addendum
- [ ] Per-move cap only (no per-game derivation live)
- [ ] Live streaming to AI panel
- [ ] Ribbon button semantics shift by mode/state
- [ ] Engine info pinned, AI prose scrolls

Tests:
- Live cancel aborts cleanly mid-stream
- Engine output still renders correctly during AI streaming

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

## Todos (cross-cutting)

- [ ] Decide model dropdown vs free-form per provider
- [ ] Decide AI panel exact placement (pin engine, scroll AI - revisit)
- [ ] Iterate prompt at end of Phase 2 and again after Phase 3
- [ ] Add `feedback_no_magic_numbers` consts for every new env var

## Bugs

(none yet)

## Decisions taken during impl

(record decisions made or revised during impl that update or supersede
spec items)

## Notes

- Cluesmith reference: `C:\Users\crist\Projects\cluesmith\server\llm.py`
- Existing analysis pattern: `human_vs_engine.py:_run_analysis` (1673)
- Existing cancel pattern: `human_vs_engine.py:_cancel_analysis` (1464)
- Existing tablebase: `play/tablebase.py`
- Existing openings: `openings.py`
