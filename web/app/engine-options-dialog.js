// Per-engine UCI options dialog. Renders form controls from the cached
// option_schema on the registry entry. Saves only changed-from-default
// values back to the engine registry.

import { showDialog, pickFile, toast, reportError } from "./dialogs.js";

const PATH_NAME_RE = /(Path|File|Dir)$/i;

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
        browse.textContent = "Browse…";
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
        toast(`${name} queued for next session`, { variant: "neutral" });
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

/**
 * Open the UCI options dialog for an engine.
 * @param {object} args
 * @param {object} args.engine - Engine registry entry (must have option_schema).
 * @param {Function} args.api - api(method, path, body?) -> Promise.
 * @returns {Promise<object|null>} the saved engine on commit, or null on cancel.
 */
export function showEngineOptionsDialog({ engine, api }) {
  const schema = engine.option_schema || {};
  const startValues = {};
  for (const [name, entry] of Object.entries(schema)) {
    if (entry.type === "button") continue;
    startValues[name] = engine.options?.[name] ?? entry.default;
  }

  return showDialog({
    label: engine.name || "UCI options",
    width: "520px",
    body: (resolve, dialog) => {
      const ctx = {
        api,
        values: { ...startValues },
        buttonsClicked: [],
      };

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
        note.textContent =
          "This engine reported no UCI options. (Click Refresh to re-query.)";
        form.appendChild(note);
      }
      for (const name of names) {
        const built = buildField(name, schema[name], ctx.values[name], ctx);
        fields.set(name, built);
        form.appendChild(built.row);
      }
      dialog.appendChild(form);

      // Footer buttons.
      const refresh = document.createElement("wa-button");
      refresh.slot = "footer";
      refresh.size = "small";
      refresh.textContent = "Refresh";
      refresh.addEventListener("click", async () => {
        try {
          const updated = await api(
            "POST",
            `/engines/${engine.id}/refresh-schema`,
            {},
          );
          // Re-open with updated schema.
          resolve({ __refresh: true, engine: updated });
        } catch (e) {
          reportError(null,"Refresh failed", e);
        }
      });

      const defaultsBtn = document.createElement("wa-button");
      defaultsBtn.slot = "footer";
      defaultsBtn.size = "small";
      defaultsBtn.textContent = "Defaults";
      defaultsBtn.addEventListener("click", () => {
        // Reopen with a synthetic engine that has options cleared so the
        // initial render uses every field's advertised default.
        resolve({ __reopen: true, engine: { ...engine, options: {} } });
      });

      const save = document.createElement("wa-button");
      save.slot = "footer";
      save.size = "small";
      save.variant = "brand";
      save.textContent = "Save";
      save.addEventListener("click", async () => {
        const diff = diffFromDefaults(ctx.values, schema);
        const trimmed = (currentName || "").trim();
        const body = { options: diff };
        if (trimmed && trimmed !== startName) {
          body.name = trimmed;
        }
        try {
          const updated = await api("PATCH", `/engines/${engine.id}`, body);
          resolve(updated);
        } catch (e) {
          reportError(null,"Save failed", e);
        }
      });

      dialog.append(refresh, defaultsBtn, save);
    },
  });
}
