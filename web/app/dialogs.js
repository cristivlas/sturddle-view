// Promise-returning wrappers around Web Awesome's <wa-dialog>.
// Application code calls these helpers; the actual UI library stays
// behind this seam so it can be swapped later.

import { APP_EVT } from "./app-events.js";
import { attachColumnResize } from "./col-resize.js";
import { attachColumnSort } from "./col-sort.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { loadRaw, saveRaw } from "./storage.js";
import { markSelectable, rafCoalesce } from "./wb-utils.js";

const FS_COL_DEFAULT_PCTS = [55, 30, 15];
const FS_MIN_COL_PCT = 8;
// Sort keys aligned to the three columns, in header order.
const FS_COL_NAME = "name";
const FS_COL_DATE = "mtime";
const FS_COL_SIZE = "size";

// Folders always group before files; within a group the active column
// orders, with name as the fixed final tiebreaker (Model A, deterministic).
function fsCompare(key, dir) {
  const sign = dir === "asc" ? 1 : -1;
  const byName = (a, b) =>
    a.name.localeCompare(b.name, undefined, { sensitivity: "base", numeric: true });
  return (a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    let primary = 0;
    if (key === FS_COL_NAME) primary = byName(a, b);
    else if (key === FS_COL_DATE) primary = (a.mtime || 0) - (b.mtime || 0);
    else if (key === FS_COL_SIZE) primary = (a.size || 0) - (b.size || 0);
    return primary !== 0 ? sign * primary : byName(a, b);
  };
}

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

/** Modal alert; `messageClass` opts into a custom message style, `width`
 *  constrains the dialog (defaults to content width). */
export function alert({ message, okLabel = "OK", messageClass, width } = {}) {
  return showDialog({
    label: "",
    width,
    body: (resolve, dialog) => {
      dialog.setAttribute("no-header", "");
      const p = document.createElement("p");
      p.className = messageClass ? `confirm-message ${messageClass}` : "confirm-message";
      if (message instanceof Node) p.appendChild(message);
      else p.textContent = message ?? "";
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

const LAST_DIR_KEY_PREFIX = STORAGE_KEY.FS_PICKER_LAST_PREFIX;
const EXE_ONLY_KEY_PREFIX = STORAGE_KEY.FS_PICKER_EXE_ONLY_PREFIX;

function recallLastDir(key) {
  return loadRaw(LAST_DIR_KEY_PREFIX + key) || null;
}

function rememberLastDir(key, dir) {
  if (!dir) return;
  saveRaw(LAST_DIR_KEY_PREFIX + key, dir);
}


const ONE_KBYTE = 1024;
const SIZE_UNITS = ["B", "KB", "MB", "GB", "TB"];
const MS_PER_SEC = 1000;

function fmtSize(bytes) {
  if (bytes == null) return "";
  let n = bytes;
  let unit = 0;
  while (n >= ONE_KBYTE && unit < SIZE_UNITS.length - 1) {
    n /= ONE_KBYTE;
    unit += 1;
  }
  const val = unit === 0 ? n : n.toFixed(1);
  return `${val} ${SIZE_UNITS[unit]}`;
}

function fmtDate(mtime) {
  if (mtime == null) return "";
  return new Date(mtime * MS_PER_SEC).toLocaleString().replace(",", "");
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
    width: "min(720px, 90vw)",
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

      // Exe-only toggle: only shown for executable pickers. Default on (hide
      // non-executables); the button reveals all files when toggled off.
      let exeOnly = wantsExec
        ? (loadRaw(EXE_ONLY_KEY_PREFIX + recallKey) !== "false")
        : false;

      let exeOnlyBtn = null;
      if (wantsExec) {
        exeOnlyBtn = document.createElement("wa-button");
        exeOnlyBtn.size = "small";
        exeOnlyBtn.className = "icon-only fs-exe-only-btn";
        const exeOnlyIcon = document.createElement("wa-icon");
        exeOnlyBtn.appendChild(exeOnlyIcon);
        const syncExeBtn = () => {
          exeOnlyBtn.title = exeOnly ? "Executables only" : "All files";
          exeOnlyBtn.setAttribute("aria-pressed", String(exeOnly));
          exeOnlyIcon.setAttribute("name", exeOnly ? "gears" : "file-lines");
        };
        syncExeBtn();
        exeOnlyBtn.addEventListener("click", () => {
          exeOnly = !exeOnly;
          saveRaw(EXE_ONLY_KEY_PREFIX + recallKey, String(exeOnly));
          syncExeBtn();
          applySort(sortCtrl.current());
        });
        pathBar.append(exeOnlyBtn);
      }

      // Real table so the shared column-resize helper (.th-grip / .col-drag-line)
      // can size Name/Date the way the engines and openings tables do.
      const tableWrap = document.createElement("div");
      tableWrap.className = "fs-picker-table-wrap";
      const table = document.createElement("table");
      table.className = "fs-picker-table";
      table.tabIndex = 0;
      markSelectable(table);
      const colgroup = document.createElement("colgroup");
      for (let i = 0; i < 3; i++) colgroup.append(document.createElement("col"));
      const thead = document.createElement("thead");
      const headRow = document.createElement("tr");
      const HEADERS = ["Name", "Date modified", "Size"];
      HEADERS.forEach((text, i) => {
        const th = document.createElement("th");
        th.textContent = text;
        // No grip on the last (Size) column -- it absorbs the remainder.
        if (i < HEADERS.length - 1) {
          const grip = document.createElement("span");
          grip.className = "th-grip";
          th.append(grip);
        }
        headRow.append(th);
      });
      thead.append(headRow);
      const listing = document.createElement("tbody");
      table.append(colgroup, thead, listing);
      tableWrap.append(table);

      const colEls = Array.from(colgroup.querySelectorAll("col"));
      const fsColPcts = FS_COL_DEFAULT_PCTS.slice();
      attachColumnResize({
        table,
        grips: Array.from(thead.querySelectorAll(".th-grip")),
        overlayHost: tableWrap,
        storageKey: STORAGE_KEY.FS_PICKER_COL_PCTS,
        sizes: fsColPcts,
        unit: "pct",
        applySizes(sizes, ctx) {
          if (ctx) {
            const { deltaFrac, startSizes, gripIdx } = ctx;
            const dPct = deltaFrac * 100;
            let a = startSizes[gripIdx] + dPct;
            let b = startSizes[gripIdx + 1] - dPct;
            if (a < FS_MIN_COL_PCT) { b -= FS_MIN_COL_PCT - a; a = FS_MIN_COL_PCT; }
            if (b < FS_MIN_COL_PCT) { a -= FS_MIN_COL_PCT - b; b = FS_MIN_COL_PCT; }
            sizes[gripIdx] = a;
            sizes[gripIdx + 1] = b;
          }
          colEls.forEach((c, i) => { c.style.width = sizes[i] + "%"; });
        },
      });

      // Jump-to-prefix: type chars while the table has focus to select the
      // next matching row. Repeated same key cycles through matches.
      const JUMP_RESET_MS = 600;
      let jumpPrefix = "";
      let jumpTimer = null;
      let jumpLastPrefix = "";
      let jumpCycleIdx = -1;

      table.addEventListener("keydown", (ev) => {
        if (ev.ctrlKey || ev.altKey || ev.metaKey) return;
        if (ev.key === "Enter") {
          ev.preventDefault();
          const target = listing.querySelector(".fs-entry.selected") ?? listing.querySelector(".fs-entry");
          target?.dispatchEvent(new MouseEvent("dblclick"));
          return;
        }
        if (ev.key.length !== 1) return;
        ev.preventDefault();
        clearTimeout(jumpTimer);
        jumpPrefix += ev.key.toLowerCase();
        const entries = Array.from(listing.querySelectorAll(".fs-entry"));
        const names = entries.map(r => (r.querySelector(".fs-name")?.textContent || "").toLowerCase());
        const isCycle = jumpPrefix === jumpLastPrefix && jumpPrefix.length === 1;
        let hitIdx = -1;
        if (isCycle) {
          const start = (jumpCycleIdx + 1) % entries.length;
          for (let i = 0; i < entries.length; i++) {
            const idx = (start + i) % entries.length;
            if (names[idx].startsWith(jumpPrefix)) { hitIdx = idx; break; }
          }
        } else {
          hitIdx = names.findIndex(n => n.startsWith(jumpPrefix));
        }
        if (hitIdx !== -1) {
          jumpCycleIdx = hitIdx;
          entries[hitIdx].click();
          entries[hitIdx].scrollIntoView({ block: "nearest" });
        }
        jumpLastPrefix = jumpPrefix;
        jumpTimer = setTimeout(() => { jumpPrefix = ""; jumpLastPrefix = ""; jumpCycleIdx = -1; }, JUMP_RESET_MS);
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

      // Entries for the current dir, kept so a sort can re-render without
      // re-fetching. The server already returns dirs-first, name-sorted.
      let currentEntries = [];

      function renderRows(entries) {
        listing.innerHTML = "";
        for (const entry of entries) {
          // In exe-only mode, skip non-eligible files entirely (dirs still show).
          if (exeOnly && !entry.is_dir && !eligible(entry)) continue;
          const li = document.createElement("tr");
          li.className = "fs-entry";
          if (!eligible(entry) && !entry.is_dir) li.classList.add("dim");
          li.dataset.path = entry.path;
          li.dataset.isDir = String(entry.is_dir);

          const nameCell = document.createElement("td");
          nameCell.className = "fs-name-cell";
          const icon = document.createElement("wa-icon");
          icon.name = entry.is_dir ? "folder" : entry.is_executable ? "gears" : "file-lines";
          const name = document.createElement("span");
          name.className = "fs-name";
          name.textContent = entry.name;
          nameCell.append(icon, name);
          li.append(nameCell);

          const date = document.createElement("td");
          date.className = "fs-date";
          date.textContent = fmtDate(entry.mtime);
          li.append(date);

          const size = document.createElement("td");
          size.className = "fs-size";
          size.textContent = entry.is_dir ? "" : fmtSize(entry.size);
          li.append(size);

          li.addEventListener("click", () => {
            for (const sel of listing.querySelectorAll(".selected")) {
              sel.classList.remove("selected");
            }
            li.classList.add("selected");
            table.focus({ preventScroll: true });
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
      }

      // Re-render currentEntries under the active sort (or server default if
      // none). Preserves any active selection by path.
      function applySort(state) {
        const selected = listing.querySelector(".selected")?.dataset.path;
        const rows = currentEntries.slice();
        if (state) rows.sort(fsCompare(state.key, state.dir));
        renderRows(rows);
        if (selected) {
          const row = listing.querySelector(`.fs-entry[data-path="${CSS.escape(selected)}"]`);
          if (row) row.classList.add("selected");
        }
      }

      const sortCtrl = attachColumnSort({
        table,
        columns: [
          { key: FS_COL_NAME, firstDir: "asc" },
          { key: FS_COL_DATE, firstDir: "desc" },
          { key: FS_COL_SIZE, firstDir: "desc" },
        ],
        storageKey: STORAGE_KEY.FS_PICKER_SORT,
        onSort: applySort,
      });

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
        jumpPrefix = "";
        jumpLastPrefix = "";
        jumpCycleIdx = -1;
        currentEntries = body.entries;
        applySort(sortCtrl.current());
        table.focus({ preventScroll: true });
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

      wrap.append(pathBar, tableWrap);
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
  const m = message.match(/^[A-Z]+\s+\/\S+\s+->\s+(\d+)\s+(.*)$/s);
  if (!m) return message;
  const [, status, body] = m;
  try {
    const parsed = JSON.parse(body);
    const detail = parsed.detail;
    if (detail && typeof detail === "object") return detail.message || JSON.stringify(detail);
    // Empty/missing detail carries no info -- echoing the raw envelope back
    // ({"detail":""}) is useless. Surface the parsed body if it has other
    // keys, else fall back to the HTTP status.
    if (detail) return detail;
    const keys = Object.keys(parsed).filter((k) => k !== "detail");
    return keys.length ? JSON.stringify(parsed) : `HTTP ${status}`;
  } catch {
    return body || `HTTP ${status}`;
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

const SETTINGS_TAB_ENGINES = "engines";
export const SETTINGS_TAB_ANALYSIS = "analysis";

/** Dispatch the deep-link event that opens the Settings dialog at
 *  `tab` (e.g. "engines"). Main wires up the actual open in main.js. */
export function openSettings(tab) {
  window.dispatchEvent(new CustomEvent(APP_EVT.OPEN_SETTINGS, { detail: { tab } }));
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
  // A verifier sub-run that never concluded -- usually the verifier round
  // cap is too low. Gear deep-links to the Analysis tab to raise it.
  no_verdict: [{
    icon: "gear",
    ariaLabel: "Open AI settings",
    onClick: () => openSettings(SETTINGS_TAB_ANALYSIS),
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

/** Canonical inline actions for a server error code, or [] if none.
 *  Same source the toast path uses, so inline renderers (e.g. the AI
 *  tool-call timeline) attach the identical gear affordance. */
export function errorActionsFor(code) {
  return ERROR_CODE_ACTIONS[code] || [];
}

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

/** A toast that stays until dismissed, with an X button. `content` is a
 *  string or a Node (laid out in the grow slot beside the button). For
 *  errors that must not auto-vanish before the user reads them. */
export function stickyToast(content, { variant = "neutral", stack } = {}) {
  let dismiss;
  const grow = document.createElement("span");
  grow.className = "toast-grow";
  if (content instanceof Node) grow.appendChild(content);
  else grow.textContent = content;
  const node = document.createElement("span");
  node.className = "toast-sort-msg";
  node.append(grow, makeToastDismissBtn(() => dismiss?.()));
  dismiss = toast(node, { variant, duration: 0, stack });
  return dismiss;
}

// Matches a leading sentence: up to the first ./!/? that is followed by
// whitespace or end-of-string, so URLs (dots mid-token) stay intact.
const FIRST_SENTENCE_RE = /.+?[.!?]+(?=\s|$)/;
const DETAILS_ICON = "circle-info";
const DETAILS_ARIA = "Error details";
const VERBOSE_ERROR_WIDTH = "min(560px, 92vw)";

/** Collapse whitespace and take the first sentence as a glanceable summary.
 *  Returns { summary, full, truncated }; `truncated` is true only when the
 *  summary actually drops text the user might want to read. */
export function summarizeError(text) {
  const full = String(text ?? "").replace(/\s+/g, " ").trim();
  const summary = (full.match(FIRST_SENTENCE_RE) || [full])[0].trim() || full;
  return { summary, full, truncated: summary.length < full.length };
}

/** Sticky danger toast for a verbose error: shows the first-sentence
 *  summary, and -- when more was dropped -- a "Details" action that opens
 *  the full text in a selectable modal. Returns the toast dismiss fn. */
export function reportVerboseError(text, { variant = "danger" } = {}) {
  const { summary, full, truncated } = summarizeError(text);
  if (!truncated) return stickyToast(summary, { variant });
  let dismiss;
  // Once the full text has been read in the modal the toast has served its
  // purpose; dismiss it when the modal closes.
  const body = buildToastWithActions(summary, [{
    icon: DETAILS_ICON,
    ariaLabel: DETAILS_ARIA,
    onClick: async () => {
      await showVerboseErrorDetails(full);
      dismiss?.();
    },
  }]);
  dismiss = stickyToast(body, { variant });
  return dismiss;
}

// http(s) URLs, stopping before trailing punctuation that is more likely
// sentence boundary than part of the link.
const URL_RE = /https?:\/\/[^\s]+[^\s.,;:!?)\]}'"]/g;

/** Build a fragment with http(s) URLs as new-tab links and everything else
 *  as plain text nodes. Never uses innerHTML, so error text can't inject. */
export function linkifyText(text) {
  const frag = document.createDocumentFragment();
  const str = String(text ?? "");
  let last = 0;
  for (const m of str.matchAll(URL_RE)) {
    if (m.index > last) frag.append(str.slice(last, m.index));
    const a = document.createElement("a");
    a.href = m[0];
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = m[0];
    frag.append(a);
    last = m.index + m[0].length;
  }
  if (last < str.length) frag.append(str.slice(last));
  return frag;
}

/** Modal showing the full error text, selectable, with any URLs rendered
 *  as clickable new-tab links. */
export function showVerboseErrorDetails(full) {
  return alert({
    message: linkifyText(full),
    messageClass: "error-detail-text",
    width: VERBOSE_ERROR_WIDTH,
  });
}

const TOAST_STACK_ID = "toast-stack";
const XGAME_TOAST_STACK_ID = "xgame-toast-stack";
const TOAST_STACK_IDS = [TOAST_STACK_ID, XGAME_TOAST_STACK_ID];
const TOAST_STACK_LIFTED = "toast-stack-lifted";

// Lift a toast stack above the WinBox minimize dock only on actual overlap:
// a minimized window intersecting the stack's horizontal extent. Lifting
// moves the stack vertically only, so the test never oscillates.
let toastLiftObserver = null;

function updateToastLift() {
  const mins = [...document.querySelectorAll(".winbox.min")]
    .map((el) => el.getBoundingClientRect());
  for (const id of TOAST_STACK_IDS) {
    const stack = document.getElementById(id);
    if (!stack) continue;
    const r = stack.getBoundingClientRect();
    const hit = stack.childElementCount > 0 &&
      mins.some((m) => m.left < r.right && m.right > r.left);
    stack.classList.toggle(TOAST_STACK_LIFTED, hit);
  }
}

const scheduleToastLift = rafCoalesce(updateToastLift);

function watchToastLift() {
  if (toastLiftObserver) return;
  toastLiftObserver = new MutationObserver(scheduleToastLift);
  // childList: toasts and WinBoxes enter/leave; class: WinBox .min toggles.
  toastLiftObserver.observe(document.body, {
    subtree: true, childList: true, attributes: true, attributeFilter: ["class"],
  });
}

// Tear the observer down once no toasts remain, so it isn't watching the
// whole body subtree for the lifetime of the page.
function stopToastLiftIfIdle() {
  const live = TOAST_STACK_IDS
    .some((id) => (document.getElementById(id)?.childElementCount ?? 0) > 0);
  if (live || !toastLiftObserver) return;
  toastLiftObserver.disconnect();
  toastLiftObserver = null;
  scheduleToastLift.cancel();
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
 *  ERROR_CODE_ACTIONS) attach their canonical action automatically.
 *  Optional `opts.duration` overrides the toast lifetime; a non-finite
 *  duration (0 / Infinity) makes it sticky with a dismiss button. */
export function reportError(ctx, action, error, opts = {}) {
  const message = (error && error.message) || String(error);
  const detailText = apiErrorDetail(error);
  const detailObj = apiErrorObject(error);
  const codeActions = detailObj?.code ? ERROR_CODE_ACTIONS[detailObj.code] : null;
  const actions = [...(opts.actions || []), ...(codeActions || [])];
  const body = actions.length
    ? buildToastWithActions(`${action}: ${detailText}`, actions)
    : `${action}: ${detailText}`;
  // A sticky toast (no auto-dismiss timer) gets the dismiss button so the
  // user can still close it; toast() alone would leave it button-less.
  const sticky = opts.duration !== undefined && !(opts.duration > 0 && Number.isFinite(opts.duration));
  if (sticky) {
    stickyToast(body, { variant: "danger" });
  } else {
    toast(body, { variant: "danger", duration: opts.duration });
  }
  ctx?.log?.(`${action}: ${message}`);
}

/** Transient toast. Pass duration: 0 (or Infinity) to keep it open until the
 *  caller invokes the returned dismiss function. `stack: "xgame"` routes
 *  to the moves-list-side stack (used for fork/variation navigation). */
export function toast(message, { variant = "neutral", duration = 4000, stack: stackName = "default" } = {}) {
  // Simple toast implementation; Web Awesome's callout supports more styling.
  const host = ensureContainer();
  const stackId = stackName === "xgame" ? XGAME_TOAST_STACK_ID : TOAST_STACK_ID;
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
  watchToastLift();
  updateToastLift();
  let dismissed = false;
  const dismiss = () => {
    if (dismissed) return;
    dismissed = true;
    t.classList.add("toast-hide");
    setTimeout(() => { t.remove(); updateToastLift(); stopToastLiftIfIdle(); }, 200);
  };
  if (duration && Number.isFinite(duration)) {
    setTimeout(dismiss, duration);
  }
  return dismiss;
}
