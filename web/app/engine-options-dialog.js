// Per-engine settings dialog. Two tabs:
//   Options — UCI option grid (rendered from option_schema).
//   Launch  — extra command-line argv + per-engine environment overrides.
// Save persists name + options + args + env in one PATCH; Refresh re-probes
// the engine using the *currently-edited* path/args/env (not the saved
// profile) so the user sees what the new launch profile actually exposes.

import { showDialog, pickFile, toast, reportError } from "./dialogs.js";

const PATH_NAME_RE = /(Path|File|Dir)$/i;
const REFRESH_BTN_LABEL = "Refresh";
const RESET_BTN_LABEL = "Reset";
const SAVE_BTN_LABEL = "Save";

function isPathOption(name) {
  return PATH_NAME_RE.test(name);
}

function diffFromDefaults(values, schema) {
  const out = {};
  for (const [name, entry] of Object.entries(schema)) {
    if (entry.type === "button") continue;
    const v = values[name];
    if (v === undefined) continue;
    if (v === entry.default) continue;
    out[name] = v;
  }
  return out;
}

function buildField(name, entry, current, ctx) {
  const row = document.createElement("div");
  row.className = "engine-opt-row";

  const label = document.createElement("label");
  label.className = "engine-opt-label";
  label.textContent = name;
  row.appendChild(label);

  let input;
  switch (entry.type) {
    case "check": {
      input = document.createElement("wa-switch");
      input.size = "small";
      if (current) input.setAttribute("checked", "");
      input.addEventListener("change", () => {
        ctx.values[name] = !!input.checked;
      });
      break;
    }
    case "spin": {
      input = document.createElement("wa-input");
      input.type = "number";
      input.size = "small";
      input.autocomplete = "off";
      input.value = String(current ?? 0);
      if (entry.min != null) input.min = String(entry.min);
      if (entry.max != null) input.max = String(entry.max);
      input.addEventListener("input", () => {
        const n = parseInt(input.value, 10);
        ctx.values[name] = Number.isFinite(n) ? n : 0;
      });
      break;
    }
    case "combo": {
      input = document.createElement("wa-select");
      input.size = "small";
      input.value = current ?? entry.default ?? "";
      for (const v of entry.vars || []) {
        const opt = document.createElement("wa-option");
        opt.value = v;
        opt.textContent = v;
        input.appendChild(opt);
      }
      input.addEventListener("change", () => {
        ctx.values[name] = input.value;
      });
      break;
    }
    case "string": {
      if (isPathOption(name)) {
        input = document.createElement("div");
        input.className = "engine-opt-path";
        const text = document.createElement("wa-input");
        text.size = "small";
        text.value = String(current ?? "");
        text.addEventListener("input", () => {
          ctx.values[name] = text.value;
        });
        const browse = document.createElement("wa-button");
        browse.size = "small";
        browse.title = "Browse…";
        browse.setAttribute("aria-label", `Pick ${name}`);
        const browseIcon = document.createElement("wa-icon");
        browseIcon.setAttribute("name", "folder-open");
        browse.appendChild(browseIcon);
        browse.addEventListener("click", async () => {
          const picked = await pickFile({
            api: ctx.api,
            title: `Pick ${name}`,
            mode: /(path|dir)$/i.test(name) ? "directory" : "file",
          });
          if (picked) {
            text.value = picked;
            ctx.values[name] = picked;
          }
        });
        input.append(text, browse);
      } else {
        input = document.createElement("wa-input");
        input.size = "small";
        input.value = String(current ?? "");
        input.addEventListener("input", () => {
          ctx.values[name] = input.value;
        });
      }
      break;
    }
    case "button": {
      input = document.createElement("wa-button");
      input.size = "small";
      input.textContent = "Send";
      input.addEventListener("click", () => {
        ctx.buttonsClicked.push(name);
        toast(`${name} queued for next session`, { variant: "success" });
      });
      break;
    }
    default:
      input = document.createElement("span");
      input.className = "muted";
      input.textContent = `(unsupported type: ${entry.type})`;
  }

  input.classList.add("engine-opt-input");
  row.appendChild(input);
  return { row, input };
}

// `launchState.args` and `launchState.env` are the persisted shapes
// (filtered list[str] and dict[str,str] respectively). The dialog keeps
// parallel working copies — `argsRows: string[]` and `envRows: {key, value}[]`
// — so the user can leave blank rows in flight without persisting them
// or colliding on duplicate keys. Each input event syncs the working
// copy back into the persisted shape (filtering out empties); Save and
// Refresh always read the persisted shape.
function buildLaunchTab(engine, launchState) {
  const wrap = document.createElement("div");
  wrap.className = "engine-launch-form";

  // Args section --------------------------------------------------------
  const argsSection = document.createElement("section");
  argsSection.className = "engine-launch-section";
  const argsHdrRow = document.createElement("div");
  argsHdrRow.className = "engine-launch-section-hdr-row";
  const argsHdr = document.createElement("h4");
  argsHdr.textContent = "Arguments";
  argsHdr.className = "engine-launch-section-hdr";
  const argsHelp = document.createElement("p");
  argsHelp.className = "muted engine-launch-help";
  argsHelp.textContent = "Values pass through verbatim — no shell parsing.";
  const argsList = document.createElement("div");
  argsList.className = "engine-launch-args-list";

  // Working copy: includes blanks the user is mid-typing. Sync to
  // launchState.args (filtered) on every edit so Save/Refresh see fresh
  // values without needing a flush call.
  if (!Array.isArray(launchState.argsRows)) {
    launchState.argsRows = [...(launchState.args || [])];
  }
  function syncArgs() {
    launchState.args = launchState.argsRows.filter((s) => s !== "");
  }

  function renderArgRows() {
    argsList.replaceChildren();
    launchState.argsRows.forEach((val, idx) => {
      const row = document.createElement("div");
      row.className = "engine-launch-args-row";
      const input = document.createElement("wa-input");
      input.size = "small";
      input.value = val;
      input.placeholder = "argument";
      input.addEventListener("input", () => {
        launchState.argsRows[idx] = input.value;
        syncArgs();
      });
      const removeBtn = document.createElement("wa-button");
      removeBtn.size = "small";
      removeBtn.appearance = "plain";
      removeBtn.title = "Remove argument";
      removeBtn.setAttribute("aria-label", "Remove argument");
      const trashIcon = document.createElement("wa-icon");
      trashIcon.setAttribute("name", "trash");
      removeBtn.appendChild(trashIcon);
      removeBtn.addEventListener("click", () => {
        launchState.argsRows.splice(idx, 1);
        syncArgs();
        renderArgRows();
      });
      row.append(input, removeBtn);
      argsList.appendChild(row);
    });
  }
  renderArgRows();

  const addArgBtn = document.createElement("wa-button");
  addArgBtn.size = "small";
  addArgBtn.appearance = "plain";
  addArgBtn.title = "Add argument";
  addArgBtn.setAttribute("aria-label", "Add argument");
  addArgBtn.classList.add("engine-launch-section-add");
  const addArgIcon = document.createElement("wa-icon");
  addArgIcon.setAttribute("name", "plus");
  addArgBtn.appendChild(addArgIcon);
  addArgBtn.addEventListener("click", () => {
    launchState.argsRows.push("");
    renderArgRows();
  });

  argsHdrRow.append(argsHdr, addArgBtn);
  argsSection.append(argsHdrRow, argsHelp, argsList);
  wrap.appendChild(argsSection);

  // Env section ---------------------------------------------------------
  const envSection = document.createElement("section");
  envSection.className = "engine-launch-section engine-launch-section-divided";
  const envHdrRow = document.createElement("div");
  envHdrRow.className = "engine-launch-section-hdr-row";
  const envHdr = document.createElement("h4");
  envHdr.textContent = "Environment";
  envHdr.className = "engine-launch-section-hdr";
  const envHelp = document.createElement("p");
  envHelp.className = "muted engine-launch-help";
  envHelp.textContent = "Overlaid on top of the inherited environment at engine spawn.";
  const envGrid = document.createElement("div");
  envGrid.className = "engine-launch-env-grid";

  // Working copy: array of {key, value} so multiple empty rows don't
  // collapse on each other (a Record<string,string> can't hold dupes).
  // Persisted shape (launchState.env) is rebuilt on every edit, dropping
  // rows with an empty key. On duplicate keys the last entry wins —
  // matches what the server would do anyway.
  if (!Array.isArray(launchState.envRows)) {
    launchState.envRows = Object.entries(launchState.env || {}).map(
      ([key, value]) => ({ key, value }),
    );
  }
  function syncEnv() {
    const out = {};
    for (const { key, value } of launchState.envRows) {
      if (key !== "") out[key] = value;
    }
    launchState.env = out;
  }

  function renderEnvRows() {
    envGrid.replaceChildren();
    launchState.envRows.forEach(({ key, value }, idx) => {
      const row = document.createElement("div");
      row.className = "engine-launch-env-row";
      const keyInput = document.createElement("wa-input");
      keyInput.size = "small";
      keyInput.value = key;
      keyInput.placeholder = "KEY";
      const valInput = document.createElement("wa-input");
      valInput.size = "small";
      valInput.value = value;
      valInput.placeholder = "value";
      const removeBtn = document.createElement("wa-button");
      removeBtn.size = "small";
      removeBtn.appearance = "plain";
      removeBtn.title = "Remove variable";
      removeBtn.setAttribute("aria-label", "Remove variable");
      const trashIcon = document.createElement("wa-icon");
      trashIcon.setAttribute("name", "trash");
      removeBtn.appendChild(trashIcon);
      keyInput.addEventListener("input", () => {
        launchState.envRows[idx].key = keyInput.value;
        syncEnv();
      });
      valInput.addEventListener("input", () => {
        launchState.envRows[idx].value = valInput.value;
        syncEnv();
      });
      removeBtn.addEventListener("click", () => {
        launchState.envRows.splice(idx, 1);
        syncEnv();
        renderEnvRows();
      });
      row.append(keyInput, valInput, removeBtn);
      envGrid.appendChild(row);
    });
  }
  renderEnvRows();

  const addEnvBtn = document.createElement("wa-button");
  addEnvBtn.size = "small";
  addEnvBtn.appearance = "plain";
  addEnvBtn.title = "Add variable";
  addEnvBtn.setAttribute("aria-label", "Add variable");
  addEnvBtn.classList.add("engine-launch-section-add");
  const addEnvIcon = document.createElement("wa-icon");
  addEnvIcon.setAttribute("name", "plus");
  addEnvBtn.appendChild(addEnvIcon);
  addEnvBtn.addEventListener("click", () => {
    launchState.envRows.push({ key: "", value: "" });
    renderEnvRows();
  });

  envHdrRow.append(envHdr, addEnvBtn);
  envSection.append(envHdrRow, envHelp, envGrid);
  wrap.appendChild(envSection);
  return wrap;
}

/** Open the Engine Settings dialog; resolves to the saved engine, or null
 *  on cancel. `probeError`: shown in place of the generic "no options" note. */
export function showEngineOptionsDialog({ engine, api, probeError = null }) {
  const schema = engine.option_schema || {};
  const startValues = {};
  for (const [name, entry] of Object.entries(schema)) {
    if (entry.type === "button") continue;
    startValues[name] = engine.options?.[name] ?? entry.default;
  }

  return showDialog({
    label: engine.name || "Engine settings",
    width: "min(560px, 94vw)",
    body: (resolve, dialog) => {
      const ctx = {
        api,
        values: { ...startValues },
        buttonsClicked: [],
      };
      const launchState = {
        args: Array.isArray(engine.args) ? [...engine.args] : [],
        env: engine.env ? { ...engine.env } : {},
      };

      // Tab group ------------------------------------------------------
      const tabs = document.createElement("wa-tab-group");
      // Side tabs on desktop, top tabs on narrow viewports — the rail
      // eats too much horizontal space on phones.
      const isNarrow = matchMedia("(max-width: 480px)").matches;
      tabs.placement = isNarrow ? "top" : "start";
      tabs.classList.add("engine-settings-tabs", "dialog-side-tabs");

      const optionsTab = document.createElement("wa-tab");
      optionsTab.slot = "nav";
      optionsTab.setAttribute("panel", "options");
      optionsTab.textContent = "Options";

      const launchTab = document.createElement("wa-tab");
      launchTab.slot = "nav";
      launchTab.setAttribute("panel", "launch");
      launchTab.textContent = "Launch";

      const optionsPanel = document.createElement("wa-tab-panel");
      optionsPanel.setAttribute("name", "options");
      const launchPanel = document.createElement("wa-tab-panel");
      launchPanel.setAttribute("name", "launch");

      // Options tab body -----------------------------------------------
      const form = document.createElement("div");
      form.className = "engine-opt-form";

      // Display name — defaults to what the engine announced via UCI id name
      // (or the binary basename if the probe failed). Editable; this is the
      // label shown in clocks, PGN headers, and tournaments.
      const startName = engine.name || "";
      let currentName = startName;
      const nameRow = document.createElement("div");
      nameRow.className = "engine-opt-row";
      const nameLabel = document.createElement("label");
      nameLabel.className = "engine-opt-label";
      nameLabel.textContent = "Name";
      const nameInput = document.createElement("wa-input");
      nameInput.size = "small";
      nameInput.classList.add("engine-opt-input");
      nameInput.value = startName;
      nameInput.addEventListener("input", () => {
        currentName = nameInput.value;
      });
      nameRow.append(nameLabel, nameInput);
      form.appendChild(nameRow);

      const fields = new Map();
      const names = Object.keys(schema).sort((a, b) => a.localeCompare(b));
      if (names.length === 0) {
        const note = document.createElement("p");
        note.className = "muted";
        // Form is a 2-col grid (display: contents on rows); span both
        // columns so the note wraps instead of squashing the Name input.
        note.style.gridColumn = "1 / -1";
        note.textContent = probeError
          ? `UCI probe failed: ${probeError} (Click Refresh to retry.)`
          : "This engine reported no UCI options. (Click Refresh to re-query.)";
        form.appendChild(note);
      }
      for (const name of names) {
        const built = buildField(name, schema[name], ctx.values[name], ctx);
        fields.set(name, built);
        form.appendChild(built.row);
      }
      optionsPanel.appendChild(form);

      // Launch tab body ------------------------------------------------
      launchPanel.appendChild(buildLaunchTab(engine, launchState));

      tabs.append(optionsTab, launchTab, optionsPanel, launchPanel);
      dialog.appendChild(tabs);

      // Footer buttons --------------------------------------------------
      const refresh = document.createElement("wa-button");
      refresh.slot = "footer";
      refresh.size = "small";
      refresh.textContent = REFRESH_BTN_LABEL;
      refresh.addEventListener("click", async () => {
        // Probe with the *in-progress* launch profile so the user sees
        // options gated by their newly-typed args/env. Server doesn't
        // touch the registry; we merge the result into a synthetic engine
        // and re-open the dialog with the same edits intact.
        try {
          const probed = await api("POST", "/engines/probe", {
            path: engine.path,
            args: launchState.args,
            env: launchState.env,
          });
          // On probe failure (no schema), keep the previous schema so
          // the user's edits don't render against an empty options list.
          // The probeError gets surfaced via the panel header note.
          const newSchema = (probed.option_schema && Object.keys(probed.option_schema).length)
            ? probed.option_schema
            : engine.option_schema;
          resolve({
            __refresh: true,
            engine: {
              ...engine,
              option_schema: newSchema,
              args: launchState.args,
              env: launchState.env,
            },
            probeError: probed.probe_error ? probed.probe_error.message : null,
          });
        } catch (e) {
          reportError(null, "Refresh failed", e);
        }
      });

      const defaultsBtn = document.createElement("wa-button");
      defaultsBtn.slot = "footer";
      defaultsBtn.size = "small";
      defaultsBtn.textContent = RESET_BTN_LABEL;
      defaultsBtn.addEventListener("click", () => {
        // Reopen with a synthetic engine that has options cleared so the
        // initial render uses every field's advertised default. Args/env
        // edits in the Launch tab carry over.
        resolve({
          __reopen: true,
          engine: {
            ...engine,
            options: {},
            args: launchState.args,
            env: launchState.env,
          },
        });
      });

      const save = document.createElement("wa-button");
      save.slot = "footer";
      save.size = "small";
      save.variant = "brand";
      save.textContent = SAVE_BTN_LABEL;
      save.addEventListener("click", async () => {
        const diff = diffFromDefaults(ctx.values, schema);
        const trimmed = (currentName || "").trim();
        const body = {
          options: diff,
          args: launchState.args,
          env: launchState.env,
        };
        if (trimmed && trimmed !== startName) {
          body.name = trimmed;
        }
        try {
          const updated = await api("PATCH", `/engines/${engine.id}`, body);
          resolve(updated);
        } catch (e) {
          reportError(null, "Save failed", e);
        }
      });

      dialog.append(refresh, defaultsBtn, save);

      // Refresh and Reset are options-tab actions — hide them on the
      // Launch tab where they have no meaningful target.
      function syncFooterForTab(panelName) {
        const onOptions = panelName === "options";
        refresh.style.display = onOptions ? "" : "none";
        defaultsBtn.style.display = onOptions ? "" : "none";
      }
      tabs.addEventListener("wa-tab-show", (e) => {
        syncFooterForTab(e.detail?.name);
      });
      queueMicrotask(() => syncFooterForTab(tabs.active || "options"));
    },
  });
}
