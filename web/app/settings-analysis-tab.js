import { apiErrorDetail } from "./dialogs.js";

// AI/analysis wire field-name constants + provider list. Mirror of the
// server contract in server/sturddle_view/api/settings.py.
const AI_ENABLED_KEY = "ai_enabled";
const AI_PROVIDER_KEY = "ai_provider";
const AI_MODEL_KEY = "ai_model";
const AI_BASE_URL_KEY = "ai_base_url";
const AI_API_KEY_KEY = "ai_api_key";
const AI_API_KEY_SET_KEY = "ai_api_key_set";
const AI_THINKING_ENABLED_KEY = "ai_thinking_enabled";
const AI_THINKING_BUDGET_TOKENS_KEY = "ai_thinking_budget_tokens";
const AI_THINKING_BUDGET_MIN = 1024;
// Server-resolved thinking wire shape (see /settings/ai/models).
const THINKING_MODE_ADAPTIVE = "adaptive";
const AI_MAX_TOOL_ROUNDS_KEY = "ai_max_tool_rounds";
const AI_VERIFIER_MAX_ROUNDS_KEY = "ai_verifier_max_rounds";
const AI_ANALYZE_MAX_DEPTH_KEY = "ai_analyze_max_depth";
const AI_VERIFICATION_DEPTH_KEY = "ai_verification_depth";
const AI_ROUNDS_MIN = 1;
const AI_DEPTH_MIN = 1;
const ANALYSIS_ENGINE_KEY = "analysis_engine_id";

// Sentinel provider value: not a real LLM. Selecting it means
// ai_enabled=false (plain engine analysis); the other entries are real LLM
// providers and set ai_enabled=true. When this is active the model dropdown
// is repurposed to pick the analysis engine.
const ENGINE_ONLY = "engine-only";
const AI_PROVIDER_OPTIONS = [
  [ENGINE_ONLY, "Engine"],
  ["anthropic", "Anthropic"],
  ["gemini", "Gemini"],
  ["ollama", "Ollama"],
];
const KEY_BASED_PROVIDERS = new Set(["anthropic", "gemini"]);

// Provider select: drives the whole tab. Displayed value reflects
// enabled-state (Engine only when AI is off) not just the stored LLM
// provider. .value is set AFTER options append -- wa-select drops a
// value with no matching option.
function buildAiProviderRow(initial) {
  const row = document.createElement("div");
  row.className = "settings-row";
  const label = document.createElement("label");
  label.textContent = "Provider";
  const select = document.createElement("wa-select");
  select.size = "small";
  select.setAttribute("distance", "4");
  for (const [val, text] of AI_PROVIDER_OPTIONS) {
    const opt = document.createElement("wa-option");
    opt.value = val;
    opt.textContent = text;
    select.append(opt);
  }
  const storedLlmProvider = initial[AI_PROVIDER_KEY] || "anthropic";
  select.value = initial[AI_ENABLED_KEY] ? storedLlmProvider : ENGINE_ONLY;
  row.append(label, select);
  const isAiOn = () => select.value !== ENGINE_ONLY;
  return { row, select, isAiOn };
}

// Inline hint when no engine is configured: analysis shares the play/view
// engine, so it can't function without one. Surfaced in-tab so the user
// can act without leaving. Returns null when an engine is present.
function buildAiNoEngineHint(noEngine) {
  if (!noEngine) return null;
  const row = document.createElement("div");
  row.className = "settings-row settings-row-hint";
  const hint = document.createElement("small");
  hint.textContent = "Register an engine in the Engines tab to enable analysis.";
  row.append(hint);
  return row;
}

// API-key row with a masking eye-toggle + deferred commit. Self-contained:
// touches only its own elements + injected deps. `onKeyCommitted` is the
// controller's post-commit hook (re-fetch models). Returns the row, the
// input, and the two sync helpers the controller drives.
function buildAiKeyRow({ initial, dialog, putSettings, onKeyCommitted }) {
  const row = document.createElement("div");
  row.className = "settings-row ai-row";
  const label = document.createElement("label");
  label.textContent = "API key";
  const input = document.createElement("wa-input");
  input.size = "small";
  // type="text" + CSS mask instead of type="password": browsers don't
  // offer to save a non-password field. Visual security is identical.
  input.type = "text";
  input.classList.add("ai-key-masked");
  input.setAttribute("autocomplete", "off");
  input.setAttribute("data-lpignore", "true");
  input.setAttribute("data-form-type", "other");
  input.setAttribute("spellcheck", "false");
  const toggleIcon = document.createElement("wa-icon");
  toggleIcon.setAttribute("name", "eye");
  toggleIcon.setAttribute("slot", "end");
  toggleIcon.classList.add("ai-key-toggle");
  toggleIcon.setAttribute("role", "button");
  toggleIcon.setAttribute("tabindex", "0");
  toggleIcon.setAttribute("aria-label", "Show/hide API key");
  let revealed = false;
  const syncMask = () => {
    // No mask/toggle when empty: the disc font would swap the
    // placeholder font and an eye on an empty field is meaningless.
    const hasInput = (input.value || "").trim().length > 0;
    toggleIcon.style.display = hasInput ? "" : "none";
    const shouldMask = hasInput && !revealed;
    input.classList.toggle("ai-key-masked", shouldMask);
    toggleIcon.setAttribute("name", shouldMask ? "eye" : "eye-slash");
  };
  toggleIcon.addEventListener("click", () => { revealed = !revealed; syncMask(); });
  const setKeyPlaceholder = () => {
    // Set on host AND shadow input: wa-input mirrors the host attr on
    // connect, but timing varies -- doing both covers every order.
    const text = initial[AI_API_KEY_SET_KEY] ? "Saved -- enter new to replace" : "";
    if (text) input.setAttribute("placeholder", text);
    else input.removeAttribute("placeholder");
    const inner = input.shadowRoot?.querySelector("input");
    if (inner) {
      if (text) inner.setAttribute("placeholder", text);
      else inner.removeAttribute("placeholder");
    }
  };
  setKeyPlaceholder();
  if (customElements.whenDefined) {
    customElements.whenDefined("wa-input").then(() => {
      requestAnimationFrame(() => { setKeyPlaceholder(); syncMask(); });
    });
  }
  // Commit on blur/Enter only -- mid-typing persists would spam the
  // provider's /models with partial keys (401 storm).
  let dirty = false;
  const commit = async () => {
    if (!dirty) return;
    dirty = false;
    const trimmed = (input.value || "").trim();
    await putSettings({ [AI_API_KEY_KEY]: trimmed });
    initial[AI_API_KEY_SET_KEY] = !!trimmed;
    setKeyPlaceholder();
    onKeyCommitted();
  };
  input.addEventListener("input", () => { dirty = true; syncMask(); });
  input.addEventListener("blur", commit);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") commit(); });
  // Esc/X close can fire before blur -- flush any pending edit.
  dialog.addEventListener("wa-hide", commit);
  syncMask();
  input.append(toggleIcon);
  row.append(label, input);
  return { row, input, setKeyPlaceholder, syncMask };
}

// Base-URL row (Ollama). Persists on a debounced input then re-fetches
// models via the injected hook.
function buildAiUrlRow({ initial, putSettings, debounce, onUrlCommitted }) {
  const row = document.createElement("div");
  row.className = "settings-row ai-row";
  const label = document.createElement("label");
  label.textContent = "Base URL";
  const input = document.createElement("wa-input");
  input.size = "small";
  input.setAttribute("autocomplete", "off");
  input.placeholder = "http://localhost:11434";
  input.value = initial[AI_BASE_URL_KEY] || "";
  const persist = debounce(async () => {
    await putSettings({ [AI_BASE_URL_KEY]: input.value });
    onUrlCommitted();
  }, 400);
  input.addEventListener("input", persist);
  row.append(label, input);
  return { row, input };
}

// Extended-thinking row: mode select (Off/On) + budget input. Budget
// visibility depends on provider + mode + adaptive-model capability, so
// `getAdaptive` is injected (the model-row interplay lives in the
// controller). Returns the divider, row, controls, and syncBudgetVisibility.
function buildAiThinkingRow({ initial, putSettings, debounce, getProvider, getAdaptive }) {
  const divider = document.createElement("hr");
  divider.className = "settings-divider";
  const row = document.createElement("div");
  row.className = "settings-row ai-row ai-thinking-row";
  const mode = document.createElement("wa-select");
  mode.size = "small";
  mode.setAttribute("label", "Extended thinking");
  mode.className = "ai-thinking-mode";
  const budget = document.createElement("wa-input");
  budget.type = "number";
  budget.size = "small";
  budget.setAttribute("label", "Budget (tokens)");
  budget.min = String(AI_THINKING_BUDGET_MIN);
  budget.step = "1024";
  budget.value = String(initial[AI_THINKING_BUDGET_TOKENS_KEY] || AI_THINKING_BUDGET_MIN);
  budget.className = "ai-thinking-budget";
  // Static options -- never replaced, so wa-select never loses its value.
  for (const [v, label] of [["off", "Off"], ["on", "On"]]) {
    const opt = document.createElement("wa-option");
    opt.value = v;
    opt.textContent = label;
    mode.append(opt);
  }
  mode.value = initial[AI_THINKING_ENABLED_KEY] ? "on" : "off";
  row.append(mode, budget);
  const syncBudgetVisibility = () => {
    const on = mode.value === "on";
    const isAnthropic = getProvider() === "anthropic";
    const adaptive = isAnthropic && getAdaptive();
    // Budget shows for Anthropic-on-non-adaptive only.
    const showBudget = on && isAnthropic && !adaptive;
    budget.style.display = showBudget ? "" : "none";
    budget.disabled = !showBudget;
    const onOpt = mode.querySelector("wa-option[value='on']");
    if (onOpt) onOpt.textContent = adaptive ? "On (adaptive)" : "On";
  };
  mode.addEventListener("change", () => {
    putSettings({ [AI_THINKING_ENABLED_KEY]: mode.value !== "off" });
    syncBudgetVisibility();
  });
  const persistBudget = debounce(() => {
    const n = Number(budget.value);
    if (!Number.isFinite(n) || n < AI_THINKING_BUDGET_MIN) return;
    putSettings({ [AI_THINKING_BUDGET_TOKENS_KEY]: n });
  }, 400);
  budget.addEventListener("input", persistBudget);
  return { divider, row, mode, budget, syncBudgetVisibility };
}

// Numeric agent-loop caps: round budgets (rounds row) + search-depth caps
// (depth row). Fully self-contained -- no forward refs. Returns both rows
// and the four inputs (for the lockout set).
function buildAiCapRows({ initial, debounce, putSettings }) {
  const makeIntInput = (key, label, min) => {
    const input = document.createElement("wa-input");
    input.type = "number";
    input.size = "small";
    input.setAttribute("label", label);
    input.setAttribute("autocomplete", "off");
    input.min = String(min);
    input.step = "1";
    input.value = String(initial[key] || min);
    const persist = debounce(() => {
      const n = Number(input.value);
      if (!Number.isFinite(n) || n < min) return;
      putSettings({ [key]: n });
    }, 400);
    input.addEventListener("input", persist);
    return input;
  };
  const roundsRow = document.createElement("div");
  roundsRow.className = "settings-row ai-row ai-rounds-row";
  const maxToolRounds = makeIntInput(AI_MAX_TOOL_ROUNDS_KEY, "Max rounds", AI_ROUNDS_MIN);
  maxToolRounds.className = "ai-max-tool-rounds";
  const verifierMaxRounds = makeIntInput(AI_VERIFIER_MAX_ROUNDS_KEY, "Max subagent rounds", AI_ROUNDS_MIN);
  verifierMaxRounds.className = "ai-verifier-max-rounds";
  roundsRow.append(maxToolRounds, verifierMaxRounds);

  const depthRow = document.createElement("div");
  depthRow.className = "settings-row ai-row ai-depth-row";
  const analyzeMaxDepth = makeIntInput(AI_ANALYZE_MAX_DEPTH_KEY, "Max search depth", AI_DEPTH_MIN);
  analyzeMaxDepth.className = "ai-analyze-max-depth";
  const verificationDepth = makeIntInput(AI_VERIFICATION_DEPTH_KEY, "Min verify depth", AI_DEPTH_MIN);
  verificationDepth.className = "ai-verification-depth";
  depthRow.append(analyzeMaxDepth, verificationDepth);

  return {
    roundsRow, depthRow,
    inputs: [maxToolRounds, verifierMaxRounds, analyzeMaxDepth, verificationDepth],
  };
}

// Builds the Analysis settings tab. Returns { tab, panel } for insertion
// into the settings dialog's tab list. All server I/O flows through the
// passed-in helpers so this module stays UI-only.
//
// Rows are built by per-section factories above; this function owns the
// reactive controller -- provider selection drives derived
// visibility/lockout/model-routing across rows (applyAiProviderVisibility,
// applyAiEnabledLockout, refreshModelRow, syncThinkingOptions).
export function buildAnalysisTab({ api, initial, dialog, noEngine, engineList, activeEngineId, putSettings, putSettingsDebounced, debounce }) {
  // Flat layout per spec: master toggle + provider + model + key/url, then
  // thinking + tunables (round/depth caps) as flat fields.
  const tab = document.createElement("wa-tab");
  tab.panel = "analysis";
  tab.textContent = "Analysis";
  const panel = document.createElement("wa-tab-panel");
  panel.name = "analysis";

  const { row: aiProviderRow, select: aiProvider, isAiOn } = buildAiProviderRow(initial);
  const aiNoEngineHint = buildAiNoEngineHint(noEngine);

  // Model row, dual-purpose by provider:
  //  - LLM provider: a dropdown from the provider's list_models API; the
  //    free-text input takes over on fetch failure.
  //  - "Engine only": relabeled "Engine", populated from registered engines;
  //    selection persists as analysis_engine_id.
  const aiModelRow = document.createElement("div");
  aiModelRow.className = "settings-row ai-row";
  const aiModelLabel = document.createElement("label");
  aiModelLabel.textContent = "Model";
  const aiModelSelect = document.createElement("wa-select");
  aiModelSelect.size = "small";
  aiModelSelect.setAttribute("distance", "4");
  const aiModelInput = document.createElement("wa-input");
  aiModelInput.size = "small";
  aiModelInput.setAttribute("autocomplete", "off");
  aiModelInput.value = initial[AI_MODEL_KEY] || "";
  aiModelInput.addEventListener("input", () => {
    putSettingsDebounced({ [AI_MODEL_KEY]: aiModelInput.value });
  });
  aiModelSelect.addEventListener("change", () => {
    if (!aiModelSelect.value) return;
    // Engine-only mode holds engine ids (analysis-engine pin); otherwise
    // LLM model ids.
    if (isAiOn()) putSettings({ [AI_MODEL_KEY]: aiModelSelect.value });
    else putSettings({ [ANALYSIS_ENGINE_KEY]: aiModelSelect.value });
  });
  aiModelRow.append(aiModelLabel, aiModelSelect, aiModelInput);

  const aiModelHint = document.createElement("div");
  aiModelHint.className = "settings-row settings-row-hint";
  const aiModelHintText = document.createElement("small");
  aiModelHint.append(aiModelHintText);

  // model id -> "adaptive" | "extended" | "none" (/settings/ai/models).
  // Unknown ids read as non-adaptive; cosmetic only -- the server
  // re-resolves authoritatively at stream time.
  let aiModelThinking = {};

  const isAdaptiveModel = () => {
    if (aiProvider.value !== "anthropic") return false;
    const live = aiModelSelect.style.display === "none"
      ? aiModelInput.value
      : aiModelSelect.value;
    const model = live || initial[AI_MODEL_KEY] || "";
    return aiModelThinking[model] === THINKING_MODE_ADAPTIVE;
  };

  // Forward-ref bridges: section builders wire these at construction, but
  // the controller fns are defined below. Thunks defer resolution.
  const aiKey = buildAiKeyRow({
    initial, dialog, putSettings,
    onKeyCommitted: () => refreshAiModels(),
  });
  const aiUrl = buildAiUrlRow({
    initial, putSettings, debounce,
    onUrlCommitted: () => refreshAiModels(),
  });
  const aiThinking = buildAiThinkingRow({
    initial, putSettings, debounce,
    getProvider: () => aiProvider.value,
    getAdaptive: isAdaptiveModel,
  });
  // Called on model/provider change to sync budget visibility + label.
  const syncThinkingOptions = () => aiThinking.syncBudgetVisibility();
  aiModelSelect.addEventListener("change", syncThinkingOptions);
  aiModelInput.addEventListener("input", syncThinkingOptions);

  const aiCaps = buildAiCapRows({ initial, debounce, putSettings });

  // Credential shape per provider: key-based shows the API key row;
  // URL-based (Ollama) shows the base URL row. "Engine only" hides both.
  function applyAiProviderVisibility() {
    const engineOnly = !isAiOn();
    const usesKey = KEY_BASED_PROVIDERS.has(aiProvider.value);
    aiKey.row.style.display = !engineOnly && usesKey ? "" : "none";
    aiUrl.row.style.display = !engineOnly && !usesKey ? "" : "none";
    // Budget visibility is owned by syncThinkingOptions (provider + mode +
    // adaptive-model interplay).
    syncThinkingOptions();
  }

  function showModelInput(reason) {
    // Fall back to free-text input when the provider can't be queried or
    // returns nothing. Hint slot is always reserved (CSS min-height); we
    // toggle text only -- no reflow on an appearing/disappearing error.
    aiModelLabel.textContent = "Model";
    aiModelSelect.style.display = "none";
    aiModelInput.style.display = "";
    aiModelHintText.textContent = reason || "";
    syncThinkingOptions();
  }

  function showModelSelect(models) {
    aiModelLabel.textContent = "Model";
    aiModelSelect.replaceChildren();
    const current = initial[AI_MODEL_KEY] || "";
    const list = models.slice();
    if (current && !list.includes(current)) list.unshift(current);
    for (const m of list) {
      const opt = document.createElement("wa-option");
      opt.value = m;
      opt.textContent = m;
      aiModelSelect.append(opt);
    }
    aiModelSelect.value = current || (list[0] || "");
    aiModelSelect.style.display = "";
    aiModelInput.style.display = "none";
    aiModelHintText.textContent = "";
    syncThinkingOptions();
  }

  // Engine-only mode: the model row becomes the analysis-engine picker.
  // Pre-selects the pinned engine (analysis_engine_id) or the active HvE
  // engine. No free-text fallback (engines are a closed set).
  function showEngineSelect() {
    aiModelLabel.textContent = "Engine";
    aiModelSelect.replaceChildren();
    for (const e of engineList) {
      const opt = document.createElement("wa-option");
      opt.value = e.id;
      opt.textContent = e.name;
      aiModelSelect.append(opt);
    }
    // Pinned id may name a deleted engine; ignore it unless it still
    // exists or wa-select drops the value and renders blank.
    const has = (id) => engineList.some((e) => e.id === id);
    const pinnedId = initial[ANALYSIS_ENGINE_KEY] || "";
    aiModelSelect.value = (has(pinnedId) && pinnedId)
      || (has(activeEngineId) && activeEngineId)
      || (engineList[0]?.id || "");
    aiModelSelect.style.display = "";
    aiModelInput.style.display = "none";
    aiModelHintText.textContent = "";
  }

  // Lazy fetch: on dialog open + provider/key/url changes. Failures
  // collapse to the free-text input with the server's error in the hint.
  let _modelsFetchSeq = 0;
  async function refreshAiModels() {
    const mySeq = ++_modelsFetchSeq;
    try {
      const r = await api("GET", "/settings/ai/models");
      if (mySeq !== _modelsFetchSeq) return;  // raced
      const models = (r && r.models) || [];
      aiModelThinking = (r && r.thinking) || {};
      if (!models.length) {
        showModelInput("Provider returned no models -- enter one manually.");
      } else {
        showModelSelect(models);
      }
    } catch (e) {
      if (mySeq !== _modelsFetchSeq) return;
      aiModelThinking = {};
      showModelInput(apiErrorDetail(e) || "Provider unavailable");
    }
  }

  // Routes the model row by mode: engine picker when "Engine only", else
  // the provider's LLM model list. Single entry point so every caller
  // stays mode-correct.
  function refreshModelRow() {
    if (isAiOn()) refreshAiModels();
    else showEngineSelect();
  }

  aiProvider.addEventListener("change", async () => {
    // Provider maps to two server fields: ai_enabled (off iff "Engine
    // only") and -- for a real provider -- ai_provider. "Engine only"
    // never overwrites the remembered LLM provider. After persisting,
    // re-read settings so model/key fields reflect THIS provider.
    if (isAiOn()) {
      await putSettings({ [AI_ENABLED_KEY]: true, [AI_PROVIDER_KEY]: aiProvider.value });
    } else {
      await putSettings({ [AI_ENABLED_KEY]: false });
    }
    try {
      const live = await api("GET", "/settings");
      initial[AI_MODEL_KEY] = live[AI_MODEL_KEY] || "";
      initial[AI_API_KEY_SET_KEY] = !!live[AI_API_KEY_SET_KEY];
      initial[ANALYSIS_ENGINE_KEY] = live[ANALYSIS_ENGINE_KEY] || "";
      aiModelInput.value = initial[AI_MODEL_KEY];
    } catch { /* refreshModelRow still runs */ }
    applyAiProviderVisibility();
    applyAiEnabledLockout();
    aiKey.setKeyPlaceholder();
    refreshModelRow();
  });
  applyAiProviderVisibility();

  // One Map drives BOTH the visual order (insertion order = render order)
  // and the lockout target set, so adding a row can't drift between the
  // two. Pair each row with the input(s) needing disabled when AI is off.
  // The provider select is the master and is never in this set. The model
  // row is the ONLY row that stays live when off (it becomes the engine
  // picker) so it carries no lockout inputs.
  const AI_ROWS = new Map([
    ["provider",         { row: aiProviderRow,         inputs: [] }],
    ["model",            { row: aiModelRow,            inputs: [] }],
    ["model-hint",       { row: aiModelHint,           inputs: [] }],
    ["api-key",          { row: aiKey.row,             inputs: [aiKey.input] }],
    ["base-url",         { row: aiUrl.row,             inputs: [aiUrl.input] }],
    ["thinking",         { row: aiThinking.row,        inputs: [aiThinking.mode, aiThinking.budget] }],
    ["thinking-divider", { row: aiThinking.divider,    inputs: [] }],
    ["rounds",           { row: aiCaps.roundsRow,      inputs: [aiCaps.inputs[0], aiCaps.inputs[1]] }],
    ["depth",            { row: aiCaps.depthRow,       inputs: [aiCaps.inputs[2], aiCaps.inputs[3]] }],
  ]);

  // LLM-only rows greyed out when provider is "Engine only". The model row
  // is excluded (engine picker in that mode). Values are retained
  // server-side; flipping back restores them.
  function applyAiEnabledLockout() {
    const off = !isAiOn();
    // Pull focus off the control before re-enabling so the user doesn't
    // see a focus-ring flash on an unrelated control.
    if (document.activeElement && typeof document.activeElement.blur === "function") {
      document.activeElement.blur();
    }
    for (const { inputs } of AI_ROWS.values()) {
      for (const el of inputs) {
        if (off) el.setAttribute("disabled", "");
        else el.removeAttribute("disabled");
      }
    }
  }

  if (aiNoEngineHint) panel.append(aiNoEngineHint);
  for (const { row } of AI_ROWS.values()) panel.append(row);

  // Initial state: hide the select until the first fetch/populate. Lock
  // fields by current provider, then route the model row.
  showModelInput("");
  applyAiEnabledLockout();
  refreshModelRow();

  return { tab, panel };
}
