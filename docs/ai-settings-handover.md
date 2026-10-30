# AI Settings - Handover

Status: analysis only. Nothing designed, approved, or coded. Start by
proposing a design to the user and wait for approval.

## The ask (user, 2026-10-29)

- Are the AI settings surfaced in Settings > Analysis still relevant
  under the skin-in-the-game design?
- Should more (or all) of them be exposed?
- Space in the dialog is tight: consider collapsible areas, like the
  tournament settings form.
- Bundled with it: an Ollama context-length setting (see below).

## Context

- Branch `feat/ai-analysis-plus`. The AI design in force is
  skin-in-the-game: `docs/ai-analysis-skin-in-the-game.md`, commits
  96cccb9 and 321a1af.
- Later model-behavior patches (idea caps, depth tweaks, prompt changes)
  were rejected as regressions and thrown out. This work must expose
  settings only: no changed defaults, no changed behavior.
- **Decided (user):** collapsible sections, in the same idiom as the
  tournaments dialog and the Settings Tournament/Common tabs.
  - The line "No collapsible / Advanced grouping; each lives flat in the
    panel." in `docs/ai-analysis-spec.md` "Settings tab Analysis" is
    stale. Replace it in the same change.

## Current Analysis tab

Built in `web/app/settings-analysis-tab.js`. One Map, `AI_ROWS`, sets
both the render order and which inputs are disabled when the provider is
"Engine". Rows in order:

1. Provider (Engine / Anthropic / Gemini / Ollama).
2. Model, plus "Lines" (MultiPV). In Engine mode this row becomes the
   engine picker.
3. Model hint.
4. API key (key-based providers) or Base URL (Ollama).
5. Extended thinking (Off/On) plus "Budget (tokens)". The budget shows
   only for Anthropic models that are not adaptive.
6. Divider.
7. Rounds row: "Max rounds", "Max subagent rounds".
8. Depth row: "Max search depth", "Min verify depth".

Row CSS: `.ai-model-row`, `.ai-thinking-row`, `.ai-rounds-row`,
`.ai-depth-row` in `web/styles.css` (around line 3150).

## The four surfaced caps still matter

Checked against the code on this branch:

| UI label | Setting | Default | What it does now |
|---|---|---|---|
| Max rounds | `ai_max_tool_rounds` | 32 | Caps rounds per turn. The fallback nudge fires `SV_AI_FALLBACK_ROUNDS_LEFT` (2) rounds before this cap. |
| Max subagent rounds | `ai_verifier_max_rounds` | 8 | Caps the `delegate` verifier sub-run. |
| Max search depth | `ai_analyze_max_depth` | 30 | Caps `analyze`/`top_moves`, and also `play_out`/`test_aim`: their depth is `min(PLAY_OUT_DEPTH, this)` (`play/tools_engine.py` ~1866, ~2108). |
| Min verify depth | `ai_verification_depth` | 25 | Depth floor for `recommend_move`'s end-of-turn check only. `test_aim` uses `PLAY_OUT_DEPTH`. |

## Candidates to expose (env-only today)

The earlier recommendation was these four:

| Env var | Default | Meaning | Defined in |
|---|---|---|---|
| `SV_AI_PLAY_OUT_DEPTH` | 14 | Depth of every `play_out`/`test_aim` search | `play/tools_engine.py` |
| `SV_AI_MAX_IDEAS` | 3 | Ideas `play_out` takes per turn | `play/play_out_caps.py` |
| `SV_AI_PLAY_OUT_MAX_MOVES` | 2 | The model's own moves per idea | `play/play_out_caps.py` |
| `SV_AI_VERIFIER_MAX_REPLIES` | 2 | Opponent replies one `delegate` may test | `play/tools_engine.py` |

The user also floated "all". The full list of knobs is in
`docs/env-vars.md` (the `SV_AI_*` rows). Ask which set the user wants; do
not decide it alone.

## Ollama context length (`num_ctx`)

- **Problem:** Ollama's default context is 4096 tokens, and the
  skin-in-the-game prompt outgrew it. Ollama cut the input silently.
  - Since 05acda4 a full context ends the turn with a `ContextFull`
    error, and the error names the host fix.
  - The user's host "dragon" now runs with `OLLAMA_CONTEXT_LENGTH=16384`.
- **Agreed idea:** an Ollama-only "Context (tokens)" setting, shown when
  extended thinking is On.
- **Where it can be set:** sent as `options.num_ctx`, which only the
  native `/api/chat` path accepts.
  - We use that path only when thinking is on and the request is not a
    forced tool call (`llm/ollama.py` `stream()`).
  - The OpenAI-compatible `/v1/chat/completions` path cannot set it, so
    there the host's `OLLAMA_CONTEXT_LENGTH` governs.
- **Cost:** a value that differs from the loaded model's context makes
  Ollama reload the model.
- **Layout options:**
  - (a) In the Advanced section, as noted at the time.
  - (b) In the thinking row's budget slot. It is empty for Ollama, so
    this adds no row.

  Present both to the user.

## Plumbing per new setting

Follow the existing caps (`ai_analyze_max_depth` is the model):

- `server/sturddle_view/config.py`:
  - a `Settings` field with a `_DEFAULT_*` constant;
  - the env var remains the default.
- `server/sturddle_view/api/settings.py`: add it to the GET dict and add
  an `_int_field` validator for PUT.
- **Tools:** read the setting per call through `settings_provider`, as
  `_max_depth` does, instead of the module constant.
  - Round caps go through `coord.run(...)` from `api/_ai_kick.py`.
- `web/app/settings-analysis-tab.js`: a `makeIntInput` plus an `AI_ROWS`
  entry, so the input joins the lockout set.
- **Docs:**
  - `docs/env-vars.md` row: note the UI override.
  - `docs/ai-analysis-spec.md` settings section.
- **Tests:**
  - unit tests for PUT validation and the setting reaching the tool;
  - e2e tests for row visibility and lockout.

## Layout

- **Idiom to match:** `collapsedDisclosure()` and `enforceOneOpen()` in
  `web/app/tournament-template-form.js` (lines 38-78).
  - These are `wa-details` sections with the chevron at the start, in an
    accordion where exactly one is open.
  - The tournaments dialog and the Settings Tournament/Common tabs use
    them through that form.
- **Reuse, don't copy:** move the helpers to a shared module and call
  them from both places.
- **Hard rule:** the Settings dialog must never scroll.
  - Check with Advanced expanded on desktop and on phone widths, with the
    tallest provider (key or URL row shown, thinking On).
  - Ask the user before any change that risks the layout, and keep
    labels short.
