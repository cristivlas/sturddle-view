# AI Analysis - Skills

Companion to `ai-analysis-spec.md`. This doc covers the "skills layer":
mechanisms for giving the agent procedural knowledge without inflating
the system prompt. First piece is **tool cards** (per-tool usage
guidance, lazy-loaded). Future pieces (position-type playbooks,
opening-repertoire hints, etc.) will live here too.

## Terminology

We use "skill" as an internal umbrella term for any chunk of procedural
guidance the agent reads on demand. Not to be confused with Anthropic
Agent Skills (the product feature) -- different unit, different trigger,
different loader. The only similarity is the underlying principle:
**don't load everything upfront; expand on demand.**

## Goal

Keep the cold system prompt small as the toolkit grows. The current
prompt carries multi-paragraph usage rules for each tool (e.g.
`Move validation`, `Piece verification`). Adding more tools the same
way would dilute every rule's attention weight -- the documented
failure mode behind the chess hallucinations we're trying to suppress.

Skills move the bulk of per-tool guidance out of the cold prompt and
inject it only when the model actually uses the tool.

## Tool cards

### Definition

A **tool card** is a multi-line string of post-call usage guidance
attached to a `ToolSpec`. It explains how to read the tool's output,
edge cases, and interpretation rules. Distinct from the tool's
`description`, which the model sees every turn and uses to decide
whether to call the tool.

### Split of responsibilities

| Lives in `description` (cold prompt, every turn) | Lives in card (lazy, on first call this turn) |
|---|---|
| One-sentence purpose | Output interpretation rules |
| Gating rule -- when to call, when not | Edge cases, common mistakes |
| Anything the model needs to decide whether to invoke | Worked examples, mode-specific nuance |

Descriptions may grow slightly to absorb gating rules currently in
`SYSTEM_PROMPT_RULES` (e.g. "Non-negotiable; confirm before naming a
move in prose"). That trade is intentional: gating rules must stay in
the cold prompt because the card never loads until after a call.

### Storage

Inline string on the `ToolSpec` (Python source, alongside the tool
definition). No separate files in v1. Rationale:
- Tools are registered statically; cards are not dynamic content.
- One place to read the tool's full contract: schema + description +
  card, all in the same module.
- File-based loading can come later if cards grow long enough to be
  awkward inline, or if dynamic registration ever lands.

### Injection

When the agent calls a tool for the first time in a given turn, the
coordinator appends the card as a **separate `{type:"text"}` content
block inside the tool_result user message**, alongside the tool_result
block:

```
assistant: [tool_use validate_move ...]
user:      [
             {type:"tool_result", content: <data>},
             {type:"text", text: <card>},   # injected on first use only
           ]
assistant: ...
```

Subsequent calls to the same tool in the same turn do not re-inject.
A per-turn `cards_injected: set[str]` tracks this; lives in the
`run()` scope (no cross-turn state).

Rationale for separate content block (not a string-prefix on the
tool_result, not a separate user message):
- Keeps `tool_result.content` as pure tool output -- unambiguous for
  transcripts, log parsers, replay tools.
- Stays inside one user message per assistant turn (Anthropic's
  expected message-alternation shape).
- Composes cleanly when parallel tool use is enabled later (per the
  forward-looking note in `ai-analysis-spec.md`): the same user
  message can carry N tool_result blocks + N card text blocks.
- Ollama translation already splits mixed-content user messages
  correctly (tool_result -> `tool` role, text -> residual `user`
  message).

### Caching interaction

Anthropic prompt caching keys on the system + initial message prefix.
Cards land deep in the message list (after tool calls), so they do
not invalidate the cached prefix. Subsequent rounds within the same
turn benefit from the cache up to the point of first card injection;
beyond that, the prefix grows but the head stays cached.

## Tools the migration covers (v1)

The current `SYSTEM_PROMPT_RULES` block contains two non-negotiable
rules that map 1:1 onto cards:

- `validate_move`: move-validation rule -> card.
- `piece_at`: piece-verification rule -> card.

The `analyze` tool currently has only the score_cp / score_text
guidance, which is generic across all engine-returning tools and stays
in `SYSTEM_PROMPT_RULES` (no card in v1).

## Testing

Coordinator-level (see `test_agent_runner.py`):
- Card injected as a `{type:"text"}` content block inside the
  tool_result user message, on first call.
- Same tool called twice in one turn: card present on first
  tool_result message, absent on second.
- Two distinct tools called: each carries its own card, in order.
- Tool with `card=None`: tool_result message has no text block.

No byte-stable tripwires on card content (lesson from the prompt
tests we dropped).

## Future skills (forward-looking; not v1)

- **Position-type playbooks** -- procedural chess wisdom for known
  position types (IQP middlegame, Lucena/Philidor endgame, typical
  pawn structures), injected when a cheap server-side classifier
  matches the live position. More valuable when targeting weaker
  local models (Ollama 7-14B) than Claude, since small models lack
  reliable recall of named techniques. **Open cost:** playbook content
  has to be hand-authored or curated from chess literature -- real
  chess work, not free. Online generation (ask a strong model to
  write playbooks on demand) was considered and rejected for v1:
  couples local-model users' quality to a paid API bill, adds a
  validation pass, non-deterministic.
- **Opening-repertoire hints** -- once a known opening is identified
  (we already pass `opening_name`/`opening_eco`), inject opening-
  specific motifs. Same authoring-cost caveat.
- **Mode-specific procedures** -- post-game commentator mode could
  load a card on critical-moment selection that coach mode does not.

These share the lazy-load principle but trigger on signals other
than tool invocation. The harness will need a more general
"skill manager" once we add the second axis; until then a `set[str]`
inside `run()` is enough.

## Out of scope (v1)

- File-based card storage.
- Dynamic tool / card registration.
- Cross-turn skill state (each `run()` starts fresh).
- Anthropic Agent Skills integration (the product feature).
