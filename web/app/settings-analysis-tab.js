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
  [ENGINE_ONLY, "Engine only"],
  ["anthropic", "Anthropic"],
  ["gemini", "Gemini"],
  ["ollama", "Ollama"],
];

// Builds the Analysis settings tab. Returns { tab, panel } for insertion
// into the settings dialog's tab list. All server I/O flows through the
// passed-in helpers so this module stays UI-only.
export function buildAnalysisTab({ api, initial, dialog, noEngine, engineList, activeEngineId, putSettings, putSettingsDebounced, debounce }) {
  // --- AI Analysis tab ---
  // Flat layout per spec: master toggle + provider + model + key/url.
  // Tunables (tool-call cap, analyze caps, etc.) surface as flat
  // fields when each proves necessary.
  const analysisTab = document.createElement("wa-tab");
  analysisTab.panel = "analysis";
  analysisTab.textContent = "Analysis";
  const analysisPanel = document.createElement("wa-tab-panel");
  analysisPanel.name = "analysis";

  // Provider drives the whole tab. "Engine only" (ai_enabled=false) disables
  // every LLM field and repurposes the model dropdown to pick the analysis
  // engine; a real LLM provider (ai_enabled=true) restores the LLM fields.
  // The last-used LLM provider is remembered server-side even while "Engine
  // only" is showing, so flipping back restores it.
  const aiProviderRow = document.createElement("div");
  aiProviderRow.className = "settings-row";
  const aiProviderLabel = document.createElement("label");
  aiProviderLabel.textContent = "Provider";
  const aiProvider = document.createElement("wa-select");
  aiProvider.size = "small";
  aiProvider.setAttribute("distance", "4");
  for (const [val, label] of AI_PROVIDER_OPTIONS) {
    const opt = document.createElement("wa-option");
    opt.value = val;
    opt.textContent = label;
    aiProvider.append(opt);
  }
  // Displayed provider reflects enabled-state, not just the stored LLM
  // provider: when AI is off we show "Engine only" regardless of which LLM
  // is remembered (the server keeps ai_provider untouched in that mode, so
  // flipping back restores it). .value must be set AFTER options are
  // appended -- wa-select drops a value with no matching option.
  const storedLlmProvider = initial[AI_PROVIDER_KEY] || "anthropic";
  aiProvider.value = initial[AI_ENABLED_KEY] ? storedLlmProvider : ENGINE_ONLY;
  aiProviderRow.append(aiProviderLabel, aiProvider);
  const isAiOn = () => aiProvider.value !== ENGINE_ONLY;

  // Inline hint when no engine is configured: analysis depends on the same
  // engine the play / view perspectives use, so it can't function without
  // one. Surfaced here rather than as a toast so the user can act on it
  // without leaving the tab.
  let aiNoEngineHint = null;
  if (noEngine) {
    aiNoEngineHint = document.createElement("div");
    aiNoEngineHint.className = "settings-row settings-row-hint";
    const hint = document.createElement("small");
    hint.textContent = "Register an engine in the Engines tab to enable analysis.";
    aiNoEngineHint.append(hint);
  }

  // Model row, dual-purpose by provider:
  //  - LLM provider: a dropdown populated from the provider's list_models
  //    API. If the fetch fails (no key / unreachable / not implemented), the
  //    free-text input takes over. Hint line below reports the state.
  //  - "Engine only": relabeled "Engine" and populated from the registered
  //    engines; selection persists as analysis_engine_id. There is no LLM
  //    "model" in this mode -- the engine version is the analogue.
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
    // In engine-only mode the dropdown holds engine ids; persist as the
    // analysis-engine pin. Otherwise it holds LLM model ids.
    if (isAiOn()) putSettings({ [AI_MODEL_KEY]: aiModelSelect.value });
    else putSettings({ [ANALYSIS_ENGINE_KEY]: aiModelSelect.value });
  });
  aiModelRow.append(aiModelLabel, aiModelSelect, aiModelInput);

  const aiModelHint = document.createElement("div");
  aiModelHint.className = "settings-row settings-row-hint";
  const aiModelHintText = document.createElement("small");
  aiModelHint.append(aiModelHintText);

  // Anthropic field: API key (masked when set). Ollama field: base URL.
  // Toggled by provider selection.
  const aiKeyRow = document.createElement("div");
  aiKeyRow.className = "settings-row ai-row";
  const aiKeyLabel = document.createElement("label");
  aiKeyLabel.textContent = "API key";
  const aiKey = document.createElement("wa-input");
  aiKey.size = "small";
  // type="text" + CSS mask (text-security: disc) instead of
  // type="password": browsers don't offer to save a non-password
  // field. Visual security is identical; both expose the value
  // via devtools.
  aiKey.type = "text";
  aiKey.classList.add("ai-key-masked");
  aiKey.setAttribute("autocomplete", "off");
  aiKey.setAttribute("data-lpignore", "true");
  aiKey.setAttribute("data-form-type", "other");
  aiKey.setAttribute("spellcheck", "false");
  // Eye icon slotted into the input's suffix slot so it sits
  // inside the field's border (matches WA's password-toggle look).
  const aiKeyToggleIcon = document.createElement("wa-icon");
  aiKeyToggleIcon.setAttribute("name", "eye");
  aiKeyToggleIcon.setAttribute("slot", "end");
  aiKeyToggleIcon.classList.add("ai-key-toggle");
  aiKeyToggleIcon.setAttribute("role", "button");
  aiKeyToggleIcon.setAttribute("tabindex", "0");
  aiKeyToggleIcon.setAttribute("aria-label", "Show/hide API key");
  let aiKeyRevealed = false;
  const syncAiKeyMaskUi = () => {
    // No mask/toggle when the field is empty: the disc font would
    // swap the placeholder font ("dance") and an eye on an empty
    // field is meaningless.
    const hasInput = (aiKey.value || "").trim().length > 0;
    aiKeyToggleIcon.style.display = hasInput ? "" : "none";
    const shouldMask = hasInput && !aiKeyRevealed;
    aiKey.classList.toggle("ai-key-masked", shouldMask);
    aiKeyToggleIcon.setAttribute("name", shouldMask ? "eye" : "eye-slash");
  };
  aiKeyToggleIcon.addEventListener("click", () => {
    aiKeyRevealed = !aiKeyRevealed;
    syncAiKeyMaskUi();
  });
  const setKeyPlaceholder = () => {
    // setAttribute on host AND on the shadow input. wa-input mirrors
    // the host attribute to the internal <input> on connect, but if
    // we set it before connect, the mirror may not happen; if we set
    // it after, the internal input has the value pinned. Doing both
    // covers every order without relying on WA internals.
    const text = initial[AI_API_KEY_SET_KEY] ? "Saved -- enter new to replace" : "";
    if (text) aiKey.setAttribute("placeholder", text);
    else aiKey.removeAttribute("placeholder");
    const inner = aiKey.shadowRoot?.querySelector("input");
    if (inner) {
      if (text) inner.setAttribute("placeholder", text);
      else inner.removeAttribute("placeholder");
    }
  };
  setKeyPlaceholder();
  // wa-input upgrades async on first connect. Apply once more after
  // upgrade so the inner <input> picks the value up even when we
  // set the attribute too early.
  if (customElements.whenDefined) {
    customElements.whenDefined("wa-input").then(() => {
      requestAnimationFrame(() => {
        setKeyPlaceholder();
        syncAiKeyMaskUi();
      });
    });
  }
  // Commit on blur/Enter only -- mid-typing persists would spam
  // Anthropic's /v1/models with partial keys (401 storm).
  let aiKeyDirty = false;
  const commitAiKey = async () => {
    if (!aiKeyDirty) return;
    aiKeyDirty = false;
    const trimmed = (aiKey.value || "").trim();
    await putSettings({ [AI_API_KEY_KEY]: trimmed });
    // Server cleared/set the slot; update local view so the
    // "Saved -- enter new to replace" placeholder appears the
    // first time the user supplies a key.
    initial[AI_API_KEY_SET_KEY] = !!trimmed;
    setKeyPlaceholder();
    refreshAiModels();
  };
  aiKey.addEventListener("input", () => {
    aiKeyDirty = true;
    syncAiKeyMaskUi();
  });
  aiKey.addEventListener("blur", commitAiKey);
  aiKey.addEventListener("keydown", (e) => {
    if (e.key === "Enter") commitAiKey();
  });
  // Esc/X close can fire before blur -- flush any pending edit
  // so the key isn't silently dropped.
  dialog.addEventListener("wa-hide", commitAiKey);
  syncAiKeyMaskUi();
  aiKey.append(aiKeyToggleIcon);
  aiKeyRow.append(aiKeyLabel, aiKey);

  const aiUrlRow = document.createElement("div");
  aiUrlRow.className = "settings-row ai-row";
  const aiUrlLabel = document.createElement("label");
  aiUrlLabel.textContent = "Base URL";
  const aiUrl = document.createElement("wa-input");
  aiUrl.size = "small";
  aiUrl.setAttribute("autocomplete", "off");
  aiUrl.placeholder = "http://localhost:11434";
  aiUrl.value = initial[AI_BASE_URL_KEY] || "";
  const persistAiUrlThenRefresh = debounce(async () => {
    await putSettings({ [AI_BASE_URL_KEY]: aiUrl.value });
    refreshAiModels();
  }, 400);
  aiUrl.addEventListener("input", () => {
    persistAiUrlThenRefresh();
  });
  aiUrlRow.append(aiUrlLabel, aiUrl);

  // Divider separates provider/credentials block from thinking row.
  // Stays visible for both providers; budget input inside the row
  // hides for Ollama.
  const aiThinkingDivider = document.createElement("hr");
  aiThinkingDivider.className = "settings-divider";

  const aiThinkingRow = document.createElement("div");
  aiThinkingRow.className = "settings-row ai-row ai-thinking-row";

  // Mode select replaces the old switch + adaptive badge. Values:
  //   off -> AI_THINKING_ENABLED_KEY=false
  //   on  -> AI_THINKING_ENABLED_KEY=true (budget shown only for
  //          Anthropic non-adaptive models)
  const aiThinkingMode = document.createElement("wa-select");
  aiThinkingMode.size = "small";
  aiThinkingMode.setAttribute("label", "Extended thinking");
  aiThinkingMode.className = "ai-thinking-mode";

  // Persisted via the same AI_THINKING_ENABLED_KEY so the server
  // contract is unchanged.
  let thinkingEnabled = !!initial[AI_THINKING_ENABLED_KEY];

  const aiThinkingBudget = document.createElement("wa-input");
  aiThinkingBudget.type = "number";
  aiThinkingBudget.size = "small";
  aiThinkingBudget.setAttribute("label", "Budget (tokens)");
  aiThinkingBudget.min = String(AI_THINKING_BUDGET_MIN);
  aiThinkingBudget.step = "1024";
  aiThinkingBudget.value = String(
    initial[AI_THINKING_BUDGET_TOKENS_KEY] || AI_THINKING_BUDGET_MIN
  );
  aiThinkingBudget.className = "ai-thinking-budget";

  const isAdaptiveModel = () => {
    if (aiProvider.value !== "anthropic") return false;
    const live = aiModelSelect.style.display === "none"
      ? aiModelInput.value
      : aiModelSelect.value;
    const model = live || initial[AI_MODEL_KEY] || "";
    const m = /^claude-opus-(\d+)-(\d+)/.exec(model);
    if (!m) return false;
    const major = Number(m[1]), minor = Number(m[2]);
    return major > 4 || (major === 4 && minor >= 6);
  };

  // Adaptive is a model capability, not a user choice. When an
  // adaptive-capable model is selected, "On" auto-uses the model's
  // budget and the budget input disappears.
  // Static options -- never replaced, so wa-select never loses its value.
  for (const [v, label] of [["off", "Off"], ["on", "On"]]) {
    const opt = document.createElement("wa-option");
    opt.value = v;
    opt.textContent = label;
    aiThinkingMode.append(opt);
  }
  aiThinkingMode.value = thinkingEnabled ? "on" : "off";

  aiThinkingRow.append(aiThinkingMode, aiThinkingBudget);

  const syncBudgetVisibility = () => {
    const on = aiThinkingMode.value === "on";
    const isAnthropic = aiProvider.value === "anthropic";
    const adaptive = isAnthropic && isAdaptiveModel();
    // Budget shows for Anthropic-on-non-adaptive only.
    const showBudget = on && isAnthropic && !adaptive;
    aiThinkingBudget.style.display = showBudget ? "" : "none";
    aiThinkingBudget.disabled = !showBudget;
    // Update "On" label to reflect adaptive capability.
    const onOpt = aiThinkingMode.querySelector("wa-option[value='on']");
    if (onOpt) onOpt.textContent = adaptive ? "On (adaptive)" : "On";
  };

  // Called on model/provider change to sync budget visibility + label.
  const syncThinkingOptions = () => syncBudgetVisibility();
  syncBudgetVisibility();

  aiThinkingMode.addEventListener("change", () => {
    thinkingEnabled = aiThinkingMode.value !== "off";
    putSettings({ [AI_THINKING_ENABLED_KEY]: thinkingEnabled });
    syncBudgetVisibility();
  });
  aiModelSelect.addEventListener("change", syncThinkingOptions);
  aiModelInput.addEventListener("input", syncThinkingOptions);
  const persistThinkingBudget = debounce(() => {
    const n = Number(aiThinkingBudget.value);
    if (!Number.isFinite(n) || n < AI_THINKING_BUDGET_MIN) return;
    putSettings({ [AI_THINKING_BUDGET_TOKENS_KEY]: n });
  }, 400);
  aiThinkingBudget.addEventListener("input", persistThinkingBudget);

  // Agent-loop round caps. Two independent guardrails: the narrator's
  // per-turn round budget, and the tighter verifier sub-run budget.
  const aiRoundsRow = document.createElement("div");
  aiRoundsRow.className = "settings-row ai-row ai-rounds-row";

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

  const aiMaxToolRounds = makeIntInput(
    AI_MAX_TOOL_ROUNDS_KEY, "Max rounds", AI_ROUNDS_MIN
  );
  aiMaxToolRounds.className = "ai-max-tool-rounds";
  const aiVerifierMaxRounds = makeIntInput(
    AI_VERIFIER_MAX_ROUNDS_KEY, "Max subagent rounds", AI_ROUNDS_MIN
  );
  aiVerifierMaxRounds.className = "ai-verifier-max-rounds";
  aiRoundsRow.append(aiMaxToolRounds, aiVerifierMaxRounds);

  // Search-depth caps: the analyze/recommend clamp and the end-of-turn
  // verifier floor. Mirrors the rounds row's structure.
  const aiDepthRow = document.createElement("div");
  aiDepthRow.className = "settings-row ai-row ai-depth-row";
  const aiAnalyzeMaxDepth = makeIntInput(
    AI_ANALYZE_MAX_DEPTH_KEY, "Max search depth", AI_DEPTH_MIN
  );
  aiAnalyzeMaxDepth.className = "ai-analyze-max-depth";
  const aiVerificationDepth = makeIntInput(
    AI_VERIFICATION_DEPTH_KEY, "Min verify depth", AI_DEPTH_MIN
  );
  aiVerificationDepth.className = "ai-verification-depth";
  aiDepthRow.append(aiAnalyzeMaxDepth, aiVerificationDepth);

  // Credential shape per provider: key-based providers show the API key
  // row; URL-based (Ollama) shows the base URL row. "Engine only" has no
  // credentials -- hide both. A set keeps adding a provider to a one-line
  // change here.
  const KEY_BASED_PROVIDERS = new Set(["anthropic", "gemini"]);
  function applyAiProviderVisibility() {
    const engineOnly = !isAiOn();
    const usesKey = KEY_BASED_PROVIDERS.has(aiProvider.value);
    aiKeyRow.style.display = !engineOnly && usesKey ? "" : "none";
    aiUrlRow.style.display = !engineOnly && !usesKey ? "" : "none";
    // Budget visibility is owned by syncThinkingOptions (provider +
    // mode + adaptive-model interplay).
    syncThinkingOptions();
  }

  function showModelInput(reason) {
    // Fall back to free-text input. Used when the provider can't
    // be queried or returns nothing usable. Hint slot is always
    // reserved (CSS min-height); we toggle text only -- so an
    // appearing/disappearing error never reflows the dialog.
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
  // Lists every registered engine; pre-selects the user's pinned engine
  // (analysis_engine_id) or, if none, the active HvE engine -- the same one
  // analysis falls back to server-side. No free-text fallback (engines are a
  // closed, registry-backed set).
  function showEngineSelect() {
    aiModelLabel.textContent = "Engine";
    aiModelSelect.replaceChildren();
    for (const e of engineList) {
      const opt = document.createElement("wa-option");
      opt.value = e.id;
      opt.textContent = e.name;
      aiModelSelect.append(opt);
    }
    const pinned = initial[ANALYSIS_ENGINE_KEY] || "";
    aiModelSelect.value = pinned || activeEngineId || (engineList[0]?.id || "");
    aiModelSelect.style.display = "";
    aiModelInput.style.display = "none";
    aiModelHintText.textContent = "";
  }

  // Lazy fetch: requested on dialog open + on provider/key/url
  // changes that could affect what the endpoint returns. Failures
  // collapse to the free-text input with the server's error in
  // the hint line.
  let _modelsFetchSeq = 0;
  async function refreshAiModels() {
    const mySeq = ++_modelsFetchSeq;
    try {
      const r = await api("GET", "/settings/ai/models");
      if (mySeq !== _modelsFetchSeq) return;  // raced
      const models = (r && r.models) || [];
      if (!models.length) {
        showModelInput("Provider returned no models -- enter one manually.");
      } else {
        showModelSelect(models);
      }
    } catch (e) {
      if (mySeq !== _modelsFetchSeq) return;
      showModelInput(apiErrorDetail(e) || "Provider unavailable");
    }
  }

  // Routes the model row by mode: engine picker when "Engine only", else the
  // provider's LLM model list. The single entry point so every caller
  // (open, provider change, key/url change) stays mode-correct.
  function refreshModelRow() {
    if (isAiOn()) refreshAiModels();
    else showEngineSelect();
  }

  aiProvider.addEventListener("change", async () => {
    // Provider selection maps to two server fields: ai_enabled (off iff
    // "Engine only") and -- for a real provider -- ai_provider. "Engine
    // only" never overwrites the remembered LLM provider, so flipping back
    // restores it. After persisting, re-read settings so the model/key
    // fields reflect the server's restored values for THIS provider.
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
    setKeyPlaceholder();
    refreshModelRow();
  });
  applyAiProviderVisibility();

  // One Map drives BOTH the visual order (insertion order = render
  // order) and the lockout target set, so adding a row can't drift
  // between the two lists. Pair each row with the input(s) inside it
  // that need the disabled attribute when AI is off. The provider select
  // is the master control and is never in this set. The model row is the
  // ONLY row that stays live when off -- it becomes the engine picker --
  // so it carries no lockout inputs.
  const AI_ROWS = new Map([
    ["provider",         { row: aiProviderRow,     inputs: [] }],
    ["model",            { row: aiModelRow,        inputs: [] }],
    ["model-hint",       { row: aiModelHint,       inputs: [] }],
    ["api-key",          { row: aiKeyRow,          inputs: [aiKey] }],
    ["base-url",         { row: aiUrlRow,          inputs: [aiUrl] }],
    ["thinking",         { row: aiThinkingRow,     inputs: [aiThinkingMode, aiThinkingBudget] }],
    ["thinking-divider", { row: aiThinkingDivider, inputs: [] }],
    ["rounds",           { row: aiRoundsRow,       inputs: [aiMaxToolRounds, aiVerifierMaxRounds] }],
    ["depth",            { row: aiDepthRow,        inputs: [aiAnalyzeMaxDepth, aiVerificationDepth] }],
  ]);

  // LLM-only rows greyed out when provider is "Engine only". The model row
  // is excluded (it's the engine picker in that mode) and is handled by
  // refreshModelRow. Values are retained server-side; flipping back to an
  // LLM provider restores them.
  function applyAiEnabledLockout() {
    const off = !isAiOn();
    // Pull focus off the control before re-enabling fields so the user
    // doesn't see a focus ring flash on an unrelated control.
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

  if (aiNoEngineHint) analysisPanel.append(aiNoEngineHint);
  for (const { row } of AI_ROWS.values()) analysisPanel.append(row);

  // Initial state: hide the select until the first fetch/populate tells us
  // what to show. Lock fields based on the current provider, then route the
  // model row (engine picker when off, LLM list when on).
  showModelInput("");
  applyAiEnabledLockout();
  refreshModelRow();

  return { tab: analysisTab, panel: analysisPanel };
}