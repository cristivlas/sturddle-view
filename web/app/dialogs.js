// Promise-returning wrappers around Web Awesome's <wa-dialog>.
// Application code calls these helpers; the actual UI library stays
// behind this seam so it can be swapped later.

function ensureContainer() {
  let c = document.getElementById("dialog-host");
  if (!c) {
    c = document.createElement("div");
    c.id = "dialog-host";
    document.body.appendChild(c);
  }
  return c;
}

/**
 * Show an arbitrary <wa-dialog>. Resolves with whatever value the caller
 * passes to `resolve()` from inside the body. The dialog is removed from
 * the DOM after it closes.
 *
 * `body(resolve, dialog)` builds the dialog content and wires up handlers
 * that eventually call `resolve(value)` to close.
 */
export function showDialog({ label, body, defaultValue = null, width }) {
  return new Promise((resolveOuter) => {
    const host = ensureContainer();
    const dialog = document.createElement("wa-dialog");
    dialog.label = label ?? "";
    if (width) dialog.style.setProperty("--width", width);
    let resolved = false;
    let resolvedValue = defaultValue;

    function resolve(value) {
      resolved = true;
      resolvedValue = value;
      dialog.open = false;
    }

    body(resolve, dialog);

    dialog.addEventListener("wa-after-hide", () => {
      dialog.remove();
      resolveOuter(resolved ? resolvedValue : defaultValue);
    });

    host.appendChild(dialog);
    requestAnimationFrame(() => {
      dialog.open = true;
    });
  });
}

/** Modal alert. Resolves to undefined when dismissed. */
export function alert({ message, title = "Notice", okLabel = "OK" } = {}) {
  return showDialog({
    label: title,
    body: (resolve, dialog) => {
      const p = document.createElement("p");
      p.textContent = message ?? "";
      const ok = document.createElement("wa-button");
      ok.variant = "brand";
      ok.slot = "footer";
      ok.textContent = okLabel;
      ok.addEventListener("click", () => resolve());
      dialog.append(p, ok);
    },
  });
}

/** Modal confirm. Resolves true on confirm, false otherwise. */
export function confirm({
  message,
  title = "Confirm",
  okLabel = "OK",
  cancelLabel = "Cancel",
  destructive = false,
} = {}) {
  return showDialog({
    label: title,
    defaultValue: false,
    body: (resolve, dialog) => {
      const p = document.createElement("p");
      p.textContent = message ?? "";

      const cancel = document.createElement("wa-button");
      cancel.slot = "footer";
      cancel.textContent = cancelLabel;
      cancel.addEventListener("click", () => resolve(false));

      const ok = document.createElement("wa-button");
      ok.slot = "footer";
      ok.variant = destructive ? "danger" : "brand";
      ok.textContent = okLabel;
      ok.addEventListener("click", () => resolve(true));

      dialog.append(p, cancel, ok);
    },
  });
}

/** Modal prompt. Resolves to entered string, or null on cancel. */
export function prompt({
  message,
  title = "Input",
  defaultValue = "",
  okLabel = "OK",
  cancelLabel = "Cancel",
  placeholder,
} = {}) {
  return showDialog({
    label: title,
    defaultValue: null,
    body: (resolve, dialog) => {
      const p = document.createElement("p");
      p.textContent = message ?? "";

      const input = document.createElement("wa-input");
      input.value = defaultValue;
      if (placeholder) input.placeholder = placeholder;

      input.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") resolve(input.value);
      });

      const cancel = document.createElement("wa-button");
      cancel.slot = "footer";
      cancel.textContent = cancelLabel;
      cancel.addEventListener("click", () => resolve(null));

      const ok = document.createElement("wa-button");
      ok.slot = "footer";
      ok.variant = "brand";
      ok.textContent = okLabel;
      ok.addEventListener("click", () => resolve(input.value));

      dialog.append(p, input, cancel, ok);
      requestAnimationFrame(() => input.focus());
    },
  });
}

/**
 * Modal file/directory picker. Browses the server's filesystem via /fs.
 * Resolves to the selected path string, or null on cancel.
 *
 * Options:
 *   - api(method, path) -> json    (required)
 *   - title                        ("Pick a file")
 *   - mode: "file" | "directory" | "executable"  ("file")
 *   - startPath                    (defaults to home; from /fs without path)
 */
export function pickFile({
  api,
  title = "Pick a file",
  mode = "file",
  startPath = null,
} = {}) {
  if (!api) throw new Error("pickFile requires an api function");

  const wantsExec = mode === "executable";
  const wantsDir = mode === "directory";

  return showDialog({
    label: title,
    width: "min(800px, 90vw)",
    defaultValue: null,
    body: (resolve, dialog) => {
      // Layout: path bar at top, listing in the middle, footer with select/cancel.
      const wrap = document.createElement("div");
      wrap.className = "fs-picker";

      const pathBar = document.createElement("div");
      pathBar.className = "fs-picker-pathbar";
      const pathInput = document.createElement("wa-input");
      pathInput.size = "small";
      pathInput.className = "fs-picker-path";
      const upBtn = document.createElement("wa-button");
      upBtn.size = "small";
      upBtn.appearance = "outlined";
      upBtn.textContent = "Up";
      pathBar.append(upBtn, pathInput);

      const listing = document.createElement("ul");
      listing.className = "fs-picker-list";

      const cancel = document.createElement("wa-button");
      cancel.slot = "footer";
      cancel.size = "small";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", () => resolve(null));

      const selectBtn = document.createElement("wa-button");
      selectBtn.slot = "footer";
      selectBtn.size = "small";
      selectBtn.variant = "brand";
      selectBtn.textContent = wantsDir ? "Select directory" : "Select";
      selectBtn.disabled = true;

      let currentSelection = null;

      function eligible(entry) {
        if (entry.error) return false;
        if (wantsDir) return entry.is_dir;
        if (wantsExec) return entry.is_file && entry.is_executable;
        return entry.is_file;
      }

      async function navigate(path) {
        let body;
        try {
          const url = path ? `/fs?path=${encodeURIComponent(path)}` : "/fs";
          body = await api("GET", url);
        } catch (e) {
          toast(`Cannot list ${path}: ${e.message}`, { variant: "danger" });
          return;
        }
        pathInput.value = body.path;
        currentSelection = wantsDir ? body.path : null;
        selectBtn.disabled = !wantsDir;
        upBtn.disabled = !body.parent;
        listing.innerHTML = "";

        for (const entry of body.entries) {
          const li = document.createElement("li");
          li.className = "fs-entry";
          if (!eligible(entry) && !entry.is_dir) li.classList.add("dim");
          li.dataset.path = entry.path;
          li.dataset.isDir = String(entry.is_dir);

          const icon = document.createElement("wa-icon");
          icon.name = entry.is_dir ? "folder" : entry.is_executable ? "gear" : "file";
          li.append(icon);

          const name = document.createElement("span");
          name.className = "fs-name";
          name.textContent = entry.name;
          li.append(name);

          li.addEventListener("click", () => {
            for (const sel of listing.querySelectorAll(".selected")) {
              sel.classList.remove("selected");
            }
            li.classList.add("selected");
            if (entry.is_dir && !wantsDir) {
              // Single click selects the dir for navigation; double click descends.
              currentSelection = null;
              selectBtn.disabled = true;
            } else {
              currentSelection = eligible(entry) ? entry.path : null;
              selectBtn.disabled = currentSelection === null;
            }
          });

          li.addEventListener("dblclick", () => {
            if (entry.is_dir) {
              navigate(entry.path);
            } else if (eligible(entry)) {
              resolve(entry.path);
            }
          });

          listing.append(li);
        }
      }

      upBtn.addEventListener("click", () => {
        if (pathInput.value) {
          // Walk one level up using the API's parent.
          api("GET", `/fs?path=${encodeURIComponent(pathInput.value)}`).then((b) => {
            if (b.parent) navigate(b.parent);
          });
        }
      });

      pathInput.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") navigate((pathInput.value || "").trim());
      });

      selectBtn.addEventListener("click", () => {
        if (currentSelection) resolve(currentSelection);
      });

      wrap.append(pathBar, listing);
      dialog.append(wrap, cancel, selectBtn);
      navigate(startPath);
    },
  });
}

/**
 * Report an error: short toast + full detail to the app log.
 * `action` is a verb phrase ("Move rejected", "New game failed").
 * `error` is the caught Error or anything with a `.message`.
 * `ctx` must expose a `log(line)` function (same as the perspective ctx).
 */
export function reportError(ctx, action, error) {
  const message = (error && error.message) || String(error);
  // Try to peel off the API URL prefix our api() helper adds:
  //   "POST /game/move -> 400 detail-here"
  // The toast wants only the human-readable trailing detail.
  let short = message;
  const m = message.match(/^[A-Z]+\s+\/\S+\s+->\s+\d+\s+(.*)$/s);
  if (m) {
    try {
      const parsed = JSON.parse(m[1]);
      short = parsed.detail || m[1];
    } catch {
      short = m[1];
    }
  }
  toast(`${action}: ${short}`, { variant: "danger" });
  ctx?.log?.(`${action}: ${message}`);
}

/** Transient toast. Returns undefined; non-blocking. */
export function toast(message, { variant = "neutral", duration = 4000 } = {}) {
  // Simple toast implementation; Web Awesome's callout supports more styling.
  const host = ensureContainer();
  let stack = document.getElementById("toast-stack");
  if (!stack) {
    stack = document.createElement("div");
    stack.id = "toast-stack";
    host.appendChild(stack);
  }
  const t = document.createElement("div");
  t.className = `toast toast-${variant}`;
  t.textContent = message;
  stack.appendChild(t);
  setTimeout(() => {
    t.classList.add("toast-hide");
    setTimeout(() => t.remove(), 200);
  }, duration);
}
