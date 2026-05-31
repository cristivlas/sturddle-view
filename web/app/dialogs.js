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

/** Show <wa-dialog>; resolve when body callback invokes resolve(value). */
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

/** Modal alert; `messageClass` opts into a custom message style. */
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
  width = "min(440px, 92vw)",
  messageClass = "",
} = {}) {
  return showDialog({
    label: "",
    defaultValue: false,
    width,
    body: (resolve, dialog) => {
      dialog.setAttribute("no-header", "");

      const p = document.createElement("p");
      p.className = `confirm-message ${messageClass}`.trim();
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

const LAST_DIR_KEY_PREFIX = "sturddle:fs-picker:last:";

function recallLastDir(key) {
  try {
    return localStorage.getItem(LAST_DIR_KEY_PREFIX + key) || null;
  } catch {
    return null;
  }
}

function rememberLastDir(key, dir) {
  if (!dir) return;
  try {
    localStorage.setItem(LAST_DIR_KEY_PREFIX + key, dir);
  } catch {
    // localStorage may be unavailable (private mode quotas, disabled). Best-effort.
  }
}


/** Modal file/directory picker (browses server FS via /fs).
 *  Resolves to selected path or null. mode: "file" | "directory" | "executable". */
export function pickFile({
  api,
  title = "Pick a file",
  mode = "file",
  startPath = null,
} = {}) {
  if (!api) throw new Error("pickFile requires an api function");

  const wantsExec = mode === "executable";
  const wantsDir = mode === "directory";
  // Per-context recall, keyed by dialog title: each call site passes a
  // distinct title (e.g. "Pick fastchess binary" vs "Add engine"), so two
  // executable pickers don't bleed into each other. Explicit startPath wins.
  const recallKey = title;
  const initialPath = startPath ?? recallLastDir(recallKey);

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

      const filterBar = document.createElement("div");
      filterBar.className = "fs-picker-filterbar";
      const filterInput = document.createElement("wa-input");
      filterInput.size = "small";
      filterInput.className = "fs-picker-filter";
      filterInput.setAttribute("placeholder", "Filter…");
      filterInput.setAttribute("clearable", "");
      filterInput.setAttribute("autocomplete", "off");
      filterInput.setAttribute("autocorrect", "off");
      filterInput.setAttribute("autocapitalize", "off");
      filterInput.setAttribute("spellcheck", "false");
      const filterIcon = document.createElement("wa-icon");
      filterIcon.setAttribute("name", "magnifying-glass");
      filterIcon.setAttribute("slot", "start");
      filterInput.appendChild(filterIcon);
      filterBar.append(filterInput);

      const listing = document.createElement("ul");
      listing.className = "fs-picker-list";

      function firstVisibleEntry() {
        return listing.querySelector(".fs-entry:not(.fs-hidden)");
      }

      function applyFilter() {
        const q = (filterInput.value || "").trim().toLowerCase();
        let anyVisible = false;
        for (const li of listing.querySelectorAll(".fs-entry")) {
          const name = li.querySelector(".fs-name")?.textContent?.toLowerCase() || "";
          const hide = q && !name.includes(q);
          li.classList.toggle("fs-hidden", hide);
          if (!hide) anyVisible = true;
        }
        listing.classList.toggle("fs-empty", !anyVisible);
        // Auto-highlight the first visible match so Enter on the filter
        // (or Tab → Select) acts on something predictable.
        if (q && anyVisible) firstVisibleEntry().click();
      }
      filterInput.addEventListener("input", applyFilter);
      filterInput.addEventListener("keydown", (ev) => {
        if (ev.key !== "Enter") return;
        const li = firstVisibleEntry();
        if (!li) return;
        ev.preventDefault();
        li.dispatchEvent(new MouseEvent("dblclick"));
      });

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
          const params = new URLSearchParams({ show_hidden: "true" });
          if (path) params.set("path", path);
          const url = `/fs?${params.toString()}`;
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
        filterInput.value = "";
        listing.classList.remove("fs-empty");

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
              rememberLastDir(recallKey, pathInput.value);
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
          rememberLastDir(recallKey, wantsDir ? currentSelection : pathInput.value);
          resolve(currentSelection);
        }
      });

      wrap.append(pathBar, filterBar, listing);
      dialog.append(wrap, selectBtn);
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

/** Strip the "METHOD /path -> STATUS " prefix and unwrap a JSON `detail`
 *  field from the kind of Error our api() helper throws. */
export function apiErrorDetail(error) {
  const message = (error && error.message) || String(error);
  const m = message.match(/^[A-Z]+\s+\/\S+\s+->\s+\d+\s+(.*)$/s);
  if (!m) return message;
  try {
    const parsed = JSON.parse(m[1]);
    const detail = parsed.detail || m[1];
    if (detail && typeof detail === "object") return detail.message || JSON.stringify(detail);
    return detail;
  } catch {
    return m[1];
  }
}

/** Like apiErrorDetail but returns the structured detail object when the
 *  server provided one (e.g. {code, message}). Returns null for non-API
 *  errors or unstructured detail. */
export function apiErrorObject(error) {
  const message = (error && error.message) || String(error);
  const m = message.match(/^[A-Z]+\s+\/\S+\s+->\s+\d+\s+(.*)$/s);
  if (!m) return null;
  try {
    const parsed = JSON.parse(m[1]);
    const detail = parsed.detail;
    return (detail && typeof detail === "object") ? detail : null;
  } catch {
    return null;
  }
}

const OPEN_SETTINGS_EVENT = "sturddle:open-settings";
const SETTINGS_TAB_ENGINES = "engines";

/** Dispatch the deep-link event that opens the Settings dialog at
 *  `tab` (e.g. "engines"). Main wires up the actual open in main.js. */
export function openSettings(tab) {
  window.dispatchEvent(new CustomEvent(OPEN_SETTINGS_EVENT, { detail: { tab } }));
}

const SVG_NS = "http://www.w3.org/2000/svg";

/** Mount an inline SVG icon that follows the parent's color and font-size,
 *  matching wa-icon's behavior closely enough to drop in alongside it.
 *
 *  `innerSvg` is the raw inner markup of the icon (no outer <svg> wrapper).
 *  `viewBox` defaults to Font Awesome's 512x512 grid so paths lifted from
 *  FA-style sources work without rescaling. Pass `ariaLabel` to expose a
 *  meaningful name, or leave undefined for purely decorative icons. */
export function inlineSvgIcon(innerSvg, { viewBox = "0 0 512 512", ariaLabel } = {}) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", viewBox);
  svg.setAttribute("fill", "currentColor");
  svg.setAttribute("width", "1em");
  svg.setAttribute("height", "1em");
  if (ariaLabel) {
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", ariaLabel);
  } else {
    svg.setAttribute("aria-hidden", "true");
  }
  svg.innerHTML = innerSvg;
  return svg;
}

// Map of well-known server error codes to inline toast actions.
// Centralized here so every reportError call site picks up the same
// remediation affordance (e.g. removing the active engine mid-session
// surfaces "no_engine_configured" through many endpoints, not just
// /game/new).
const ERROR_CODE_ACTIONS = {
  no_engine_configured: [{
    icon: "gear",
    ariaLabel: "Open engine settings",
    onClick: () => openSettings(SETTINGS_TAB_ENGINES),
  }],
};

/** Canonical "open the Engines settings tab" toast action. Use this in
 *  client-side guards that want the same affordance as the server-side
 *  no_engine_configured handler. */
export const OPEN_ENGINES_ACTION = {
  icon: "gear",
  ariaLabel: "Open engine settings",
  onClick: () => openSettings(SETTINGS_TAB_ENGINES),
};

/** Build a single inline toast action button. `action` is
 *  {icon|label, ariaLabel?, onClick}. */
export function buildToastActionButton(action) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "toast-action-btn";
  if (action.icon) {
    // Icon variant gets the square icon-button sizing; text variant
    // keeps toast-action-btn's padding so the label fits.
    btn.classList.add("toast-icon-btn");
    const ic = document.createElement("wa-icon");
    ic.setAttribute("name", action.icon);
    btn.appendChild(ic);
    const al = action.ariaLabel || action.label || "";
    if (al) {
      btn.setAttribute("aria-label", al);
      btn.setAttribute("title", al);
    }
  } else {
    btn.textContent = action.label;
  }
  btn.addEventListener("click", action.onClick);
  return btn;
}

/** Build an xmark dismiss button for use inside a toast. */
export function makeToastDismissBtn(onClick) {
  const btn = document.createElement("button");
  btn.className = "xgame-toast-x";
  btn.type = "button";
  btn.setAttribute("aria-label", "Dismiss");
  btn.title = "Dismiss";
  const icon = document.createElement("wa-icon");
  icon.setAttribute("name", "xmark");
  btn.append(icon);
  btn.addEventListener("click", onClick);
  return btn;
}

/** Compose a toast message Node from leading text plus action buttons. */
export function buildToastWithActions(text, actions) {
  const node = document.createElement("span");
  node.append(document.createTextNode(text + " "));
  for (const a of actions) node.appendChild(buildToastActionButton(a));
  return node;
}

/** Report an error: toast + ctx.log(). `action` is a verb phrase.
 *  Optional `opts.actions`: [{label|icon, ariaLabel?, onClick}] adds
 *  inline buttons to the toast. Well-known server error codes (see
 *  ERROR_CODE_ACTIONS) attach their canonical action automatically. */
export function reportError(ctx, action, error, opts = {}) {
  const message = (error && error.message) || String(error);
  const detailText = apiErrorDetail(error);
  const detailObj = apiErrorObject(error);
  const codeActions = detailObj?.code ? ERROR_CODE_ACTIONS[detailObj.code] : null;
  const actions = [...(opts.actions || []), ...(codeActions || [])];
  if (actions.length) {
    toast(buildToastWithActions(`${action}: ${detailText}`, actions), { variant: "danger" });
  } else {
    toast(`${action}: ${detailText}`, { variant: "danger" });
  }
  ctx?.log?.(`${action}: ${message}`);
}

/** Transient toast. Pass duration: 0 (or Infinity) to keep it open until the
 *  caller invokes the returned dismiss function. `stack: "xgame"` routes
 *  to the moves-list-side stack (used for fork/variation navigation). */
export function toast(message, { variant = "neutral", duration = 4000, stack: stackName = "default" } = {}) {
  // Simple toast implementation; Web Awesome's callout supports more styling.
  const host = ensureContainer();
  const stackId = stackName === "xgame" ? "xgame-toast-stack" : "toast-stack";
  let stack = document.getElementById(stackId);
  if (!stack) {
    stack = document.createElement("div");
    stack.id = stackId;
    // popover="manual" promotes the stack to the top-layer so toasts sit
    // above any open <wa-dialog> backdrop (native <dialog> is top-layer
    // too; without this, author z-index alone can't beat it).
    stack.setAttribute("popover", "manual");
    host.appendChild(stack);
  }
  if (stack.showPopover) {
    try {
      if (stack.matches?.(':popover-open')) stack.hidePopover();
      stack.showPopover();
    } catch { /* unsupported */ }
  }
  const t = document.createElement("div");
  t.className = `toast toast-${variant}`;
  if (message instanceof Node) {
    t.appendChild(message);
  } else {
    t.textContent = message;
  }
  stack.appendChild(t);
  let dismissed = false;
  const dismiss = () => {
    if (dismissed) return;
    dismissed = true;
    t.classList.add("toast-hide");
    setTimeout(() => t.remove(), 200);
  };
  if (duration && Number.isFinite(duration)) {
    setTimeout(dismiss, duration);
  }
  return dismiss;
}
