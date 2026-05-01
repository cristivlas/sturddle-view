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
export function showDialog({ label, body, defaultValue = null, width, height }) {
  return new Promise((resolveOuter) => {
    const host = ensureContainer();
    const dialog = document.createElement("wa-dialog");
    dialog.label = label ?? "";
    if (width) dialog.style.setProperty("--width", width);
    if (height) dialog.style.setProperty("--dialog-height", height);
    let resolved = false;
    let resolvedValue = defaultValue;

    function resolve(value) {
      resolved = true;
      resolvedValue = value;
      dialog.open = false;
    }

    body(resolve, dialog);

    dialog.addEventListener("wa-after-hide", (ev) => {
      // Other Web Awesome overlays (selects, popovers, dropdowns) bubble
      // their own wa-after-hide through the dialog. Only the dialog's own
      // close should resolve us.
      if (ev.target !== dialog) return;
      dialog.remove();
      resolveOuter(resolved ? resolvedValue : defaultValue);
    });

    host.appendChild(dialog);
    requestAnimationFrame(() => {
      dialog.open = true;
    });
  });
}

/** Modal alert. Resolves to undefined when dismissed.
 *
 * `messageClass` lets callers opt into a different message style (e.g.
 * "game-over-message" for a large, centered headline). */
export function alert({ message, okLabel = "OK", messageClass } = {}) {
  return showDialog({
    label: "",
    body: (resolve, dialog) => {
      dialog.setAttribute("no-header", "");
      const p = document.createElement("p");
      p.className = messageClass ? `confirm-message ${messageClass}` : "confirm-message";
      p.textContent = message ?? "";
      const ok = document.createElement("wa-button");
      ok.size = "small";
      ok.slot = "footer";
      ok.textContent = okLabel;
      ok.addEventListener("click", () => resolve());
      dialog.append(p, ok);
    },
  });
}

/** Modal confirm. Resolves true on confirm, false otherwise.
 *  No title by design — the message itself carries the question, the
 *  destructive button label is the verb. (iOS-style.) */
export function confirm({
  message,
  okLabel = "OK",
  cancelLabel = "Cancel",
  destructive = false,
} = {}) {
  return showDialog({
    label: "",
    defaultValue: false,
    body: (resolve, dialog) => {
      dialog.setAttribute("no-header", "");

      const p = document.createElement("p");
      p.className = "confirm-message";
      p.textContent = message ?? "";

      const cancel = document.createElement("wa-button");
      cancel.slot = "footer";
      cancel.size = "small";
      cancel.textContent = cancelLabel;
      cancel.addEventListener("click", () => resolve(false));

      const ok = document.createElement("wa-button");
      ok.slot = "footer";
      ok.size = "small";
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

const LAST_DIR_KEY_PREFIX = "fs-picker:last:";

function recallLastDir(mode) {
  try {
    return localStorage.getItem(LAST_DIR_KEY_PREFIX + mode) || null;
  } catch {
    return null;
  }
}

function rememberLastDir(mode, dir) {
  if (!dir) return;
  try {
    localStorage.setItem(LAST_DIR_KEY_PREFIX + mode, dir);
  } catch {
    // localStorage may be unavailable (private mode quotas, disabled). Best-effort.
  }
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
  // Per-mode recall: an executable pick in ~/bin shouldn't bias the next
  // PGN open. Explicit startPath always wins.
  const initialPath = startPath ?? recallLastDir(mode);

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

      const backBtn = document.createElement("wa-button");
      backBtn.size = "small";
      backBtn.className = "icon-only";
      backBtn.setAttribute("aria-label", "Back to previous directory");
      const backIcon = document.createElement("wa-icon");
      backIcon.setAttribute("name", "reply");
      backBtn.appendChild(backIcon);
      backBtn.disabled = true;

      const upBtn = document.createElement("wa-button");
      upBtn.size = "small";
      upBtn.className = "icon-only";
      upBtn.setAttribute("aria-label", "Up to parent directory");
      const upIcon = document.createElement("wa-icon");
      upIcon.setAttribute("name", "arrow-up");
      upBtn.appendChild(upIcon);

      pathBar.append(backBtn, upBtn, pathInput);

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
      // Browser-style history: stack of visited paths. The current dir
      // sits at the top; Back pops one and re-navigates without pushing.
      const history = [];

      function eligible(entry) {
        if (entry.error) return false;
        if (wantsDir) return entry.is_dir;
        if (wantsExec) return entry.is_file && entry.is_executable;
        return entry.is_file;
      }

      async function navigate(path, { push = true } = {}) {
        let body;
        try {
          const url = path ? `/fs?path=${encodeURIComponent(path)}` : "/fs";
          body = await api("GET", url);
        } catch (e) {
          toast(`Cannot list ${path}: ${e.message}`, { variant: "danger" });
          return false;
        }
        pathInput.value = body.path;
        currentSelection = wantsDir ? body.path : null;
        selectBtn.disabled = !wantsDir;
        upBtn.disabled = !body.parent;
        if (push && history[history.length - 1] !== body.path) {
          history.push(body.path);
        }
        backBtn.disabled = history.length < 2;
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
              rememberLastDir(mode, pathInput.value);
              resolve(entry.path);
            }
          });

          listing.append(li);
        }
        return true;
      }

      upBtn.addEventListener("click", () => {
        if (pathInput.value) {
          api("GET", `/fs?path=${encodeURIComponent(pathInput.value)}`).then((b) => {
            if (b.parent) navigate(b.parent);
          });
        }
      });

      backBtn.addEventListener("click", async () => {
        if (history.length < 2) return;
        const popped = history.pop(); // current
        const prev = history[history.length - 1];
        const ok = await navigate(prev, { push: false });
        // Restore the popped entry if the navigation failed; otherwise
        // we'd shorten history without actually moving back.
        if (!ok) history.push(popped);
        backBtn.disabled = history.length < 2;
      });

      pathInput.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") navigate((pathInput.value || "").trim());
      });

      selectBtn.addEventListener("click", () => {
        if (currentSelection) {
          // For file/exec mode, pathInput.value is the containing dir; for
          // directory mode, currentSelection IS a dir. Either is valid recall.
          rememberLastDir(mode, wantsDir ? currentSelection : pathInput.value);
          resolve(currentSelection);
        }
      });

      wrap.append(pathBar, listing);
      dialog.append(wrap, cancel, selectBtn);
      // Open at the recalled dir; if it's gone (deleted/renamed since the
      // last pick), silently fall back to home rather than show a toast.
      (async () => {
        if (initialPath) {
          try {
            await api("GET", `/fs?path=${encodeURIComponent(initialPath)}`);
            navigate(initialPath);
            return;
          } catch {
            // fall through to default
          }
        }
        navigate(null);
      })();
    },
  });
}

/**
 * Report an error: short toast + full detail to the app log.
 * `action` is a verb phrase ("Move rejected", "New game failed").
 * `error` is the caught Error or anything with a `.message`.
 * `ctx` must expose a `log(line)` function (same as the perspective ctx).
 */
/** Strip the "METHOD /path -> STATUS " prefix and unwrap a JSON `detail`
 *  field from the kind of Error our api() helper throws. */
export function apiErrorDetail(error) {
  const message = (error && error.message) || String(error);
  const m = message.match(/^[A-Z]+\s+\/\S+\s+->\s+\d+\s+(.*)$/s);
  if (!m) return message;
  try {
    const parsed = JSON.parse(m[1]);
    return parsed.detail || m[1];
  } catch {
    return m[1];
  }
}

export function reportError(ctx, action, error) {
  const message = (error && error.message) || String(error);
  toast(`${action}: ${apiErrorDetail(error)}`, { variant: "danger" });
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
