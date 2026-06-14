# AI Analysis & Commentary - Implementation Progress

Companion to `ai-analysis-spec.md`. Tracks live work, open items, and
fresh decisions that have not yet earned a place in the spec. Spec
stays stable; this doc churns. When a decision proves load-bearing
across phases, lift it into the spec and remove it from here.

Shipped: walking skeleton, agent loop, inline tool-call recovery
(call-syntax and fenced JSON), single-slot dedup, Anthropic + Ollama
providers with extended thinking, Harmony marker stripping, tool
registry (`analyze`, `validate_move`, `piece_at`, `top_moves`,
`recommend_move` with centipawn dominance check, `report_line`,
`related_openings`, and a narrator/verifier `delegate` split) with lazy
tool cards, live `play` path end-to-end, dockable AI panel with round-interleaved
timeline, Settings UI with OS-keyring API-key storage and per-provider
model memory. The round-end prose validators + corrective rounds and
the revision banner were removed in favor of prompt + tool grounding
(see `git log main..HEAD`).
Remaining big items: token caps, and path 3 (post-game PGN annotations).
Path 2 (live `view` mode) ships -- commentator persona + full move context
in the initial user message; the open gap there is the per-ply eval array
(evals reach the model only via `analyze`). See §Open work. Past decisions
that proved load-bearing have been lifted into `ai-analysis-spec.md`.

## Status legend

`[ ]` not started · `[~]` in progress · `[x]` done · `[-]` dropped

## Open work

### Token caps (HIGH PRIORITY)

Spec §Guardrails calls per-move and per-game token caps non-negotiable;
only the round-cap (`SV_AI_MAX_TOOL_ROUNDS`) and `analyze` time/depth
caps ship today. With Anthropic live, an unbounded loop is a billing
risk.

Wire-up:
- Anthropic returns `usage` (`input_tokens`, `output_tokens`,
  `cache_*`) in `message_delta` / `message_stop` SSE events.
- Ollama returns `prompt_eval_count` + `eval_count` at stream end.
- Coordinator tracks per-turn + cumulative-per-game; loop stops when
  either cap is hit; surface `done.token_cap=true` to the UI.
- Settings UI exposure (flat in the Analysis tab).

### Settings tunables

Surface as flat fields in the Analysis tab when each proves needed.

- [x] `ai_max_tool_rounds` (and `ai_verifier_max_rounds`) -- promoted
      from module const to `Settings` field, env-overridable, surfaced in
      the Analysis tab.
- [ ] Token caps (depends on token-cap wire-up above).
- [ ] `analyze` max time_ms / max depth.
- [ ] Stop grace timeout.
- [ ] Effective-config display (env-derived vs user-set).

### Path 3 (post-game) + path 2 (live view)

- [~] Full PGN in initial user message (view mode) -- shipped via
      `Game moves`. Per-ply eval array NOT shipped; engine evals reach
      the model only through `analyze` tool calls.
- [ ] Structured per-ply annotations emitted via `ai_annotation`.
- [ ] PGN persistence via shared `apply_comment` helper +
      `[Annotator]` tag.
- [ ] Re-run confirm prompt (Overwrite / Cancel / Save backup).

Tests:
- [ ] Canned LLM responses produce expected annotations.
- [ ] PGN round-trip preserves annotations and `[Annotator]` metadata.
- [ ] Overwrite prompt path: backup file created, original preserved.

### Error UX

- [ ] Category mapping table (auth / rate-limit / network / refusal):
      today every error reaches the user as raw text. Categories would
      let the toast suggest a specific action ("Open Settings" for
      auth, "Retry" for rate limit). Park until evidence shows it
      matters.

### Other pending tools

- [ ] `tablebase_probe()` -- wrap `TablebaseProber`; live board only.
      Register conditionally on `engine_default_syzygy_path` being set
      (no tool listed = no missing-syzygy error envelope to model).
- [x] `opening_lookup()` -- shipped as `related_openings` (narrator-only
      dataset lookup; live board defaults to the position's opening).
- [ ] `get_position(ply)` / `get_pgn_range(from_ply, to_ply)` -- low
      priority: full PGN already in initial user message.

### Output format

Some models leak Markdown / LaTeX into prose. Prompt now says "plain
text only". If leakage persists, options are (a) client-side regex
strip (`**`, `*`, `_`, math wrappers), or (b) tiny renderer that
interprets MD. Park until evidence warrants.

### Architectural debt

- **`api/_ai_kick.py` boundary.** Today it reads HVE state via public
  accessors (`current_board()`, `start_fen()`,
  `view_full_moves_san()`, `pre_analysis_mode()`). Original plan was
  to fold into the coordinator when rolling sessions land; rolling
  sessions are indefinitely postponed (see spec §Live session model),
  so this boundary is permanent. Worth deciding whether the
  start-of-turn assembly stays at the API boundary or moves into the
  coordinator regardless.

- **`EngineSupervisor.spawn()` vs `spawn_throwaway()` consolidation.**
  Today they're honestly different (former binds to long-lived
  `_engine`/`_transport`; latter returns self-contained
  `(engine, cleanup)` so multiple concurrent one-shots from the same
  supervisor are safe). A shared `_spawn_impl(...)` returning
  `(engine, transport, cleanup)` is the obvious factor-out -- but
  premature at two callers. Worth ~30 lines of near-duplication until
  a third caller appears.

## Indefinitely postponed

- **Rolling session model.** Spec §Live session model and the per-game
  cap reasoning assumed an agent session that persists across Analyze
  clicks. Today every click sends the whole PGN as the initial user
  message and runs an independent turn -- equivalent context, no
  shared state to invalidate. A real session would unlock
  prompt-caching savings + the "session reset on takeback past
  annotated ply" trigger, but adds rebuild logic for diverging move
  stacks. Park until evidence (cost or coherence) warrants it.
