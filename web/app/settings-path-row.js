// Shared path-row builder used by the Common and Tournament settings tabs.
// Layout: label on top, [path-field][Browse][Clear] on a row underneath.
// The field is always a wa-input -- editable for PGN dir, readonly for
// paths picked via Browse only. Using a real input means long values clip
// naturally inside the field instead of expanding the row and pushing the
// action buttons out of column alignment.
//
// Returns a pathRow(labelText, value, mode, pickerTitle, onPick, opts) fn
// bound to the dialog's api (for the file picker).

import { pickFile } from "./dialogs.js";

export function makePathRow(api) {
  return function pathRow(labelText, value, mode, pickerTitle, onPick, opts = {}) {
    const { hint, editable = false, placeholder } = opts;
    const row = document.createElement("div");
    row.className = "settings-tournament-path-row";
    const lbl = document.createElement("div");
    lbl.className = "settings-tournament-path-label";
    if (labelText instanceof Node) lbl.appendChild(labelText);
    else lbl.textContent = labelText;
    if (hint) {
      const h = document.createElement("span");
      h.className = "muted settings-row-hint";
      h.textContent = ` ${hint}`;
      lbl.appendChild(h);
    }
    const inner = document.createElement("div");
    inner.className = "settings-tournament-path-inner";

    const field = document.createElement("wa-input");
    field.size = "small";
    field.setAttribute("autocomplete", "off");
    field.classList.add("path-field");
    field.value = value || "";
    const inner_actions = document.createElement("div");
    inner_actions.className = "settings-row-actions";
    const browse = document.createElement("wa-button");
    browse.size = "small";
    browse.title = "Browse…";
    browse.setAttribute("aria-label", pickerTitle || "Browse");
    const browseIcon = document.createElement("wa-icon");
    browseIcon.setAttribute("name", "folder-open");
    browse.appendChild(browseIcon);
    const clear = document.createElement("wa-button");
    clear.size = "small";
    clear.title = "Clear";
    clear.setAttribute("aria-label", `Clear ${pickerTitle || "value"}`);
    const clearIcon = document.createElement("wa-icon");
    clearIcon.setAttribute("name", "xmark");
    clear.appendChild(clearIcon);
    const syncClear = () => {
      clear.disabled = !(field.value || "").trim();
    };

    if (editable) {
      if (placeholder) field.placeholder = placeholder;
      field.addEventListener("input", () => {
        syncClear();
        onPick((field.value || "").trim(), { typing: true });
      });
      // Commit on blur / Enter -- typing-time callbacks can debounce or
      // skip; this is the "user is done editing" signal.
      field.addEventListener("change", () => {
        syncClear();
        onPick((field.value || "").trim());
      });
    } else {
      field.setAttribute("readonly", "");
      field.placeholder = "(not set)";
    }
    browse.addEventListener("click", async () => {
      const path = await pickFile({ api, mode, title: pickerTitle });
      if (!path) return;
      field.value = path;
      syncClear();
      onPick(path);
    });
    clear.addEventListener("click", () => {
      field.value = "";
      syncClear();
      onPick("");
    });
    syncClear();
    inner_actions.append(browse, clear);
    inner.append(field, inner_actions);
    row.append(lbl, inner);
    return row;
  };
}
