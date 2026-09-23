// Promise-returning wrappers around Web Awesome's <wa-dialog>.
// Application code calls these helpers; the actual UI library stays
// behind this seam so it can be swapped later.

import { APP_EVT } from "./app-events.js";
import { attachColumnResize, makePctApplySizes } from "./col-resize.js";
import { wireSplitScroll } from "./split-table.js";
import { attachColumnSort, baseCompare, modelACompare, scrollSortedRowIntoView } from "./col-sort.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { loadRaw, saveRaw } from "./storage.js";
import { markSelectable, rafCoalesce, splitPath, suppressMultiClickSelect, wireArrowKeyNav } from "./wb-utils.js";

const FS_ENTRY_CLASS = "fs-entry";
const FS_SELECTED_CLASS = "selected";
const FS_NAME_CLASS = "fs-name";
const FS_ENTRY_SEL = `.${FS_ENTRY_CLASS}`;
const FS_SELECTED_SEL = `${FS_ENTRY_SEL}.${FS_SELECTED_CLASS}`;
const FS_NAME_SEL = `.${FS_NAME_CLASS}`;
const FS_ICON_DIR = "folder";
const FS_ICON_EXE = "gears";
const FS_ICON_FILE = "file-lines";
const FS_HEADERS = ["Name", "Date modified", "Size"];
// Typing pause after which jump-to-prefix starts a fresh prefix.
const FS_JUMP_RESET_MS = 600;
const FS_COL_DEFAULT_PCTS = [55, 30, 15];
const FS_MIN_COL_PCT = 8;
// Sort keys aligned to the three columns, in header order.
const FS_COL_NAME = "name";
const FS_COL_DATE = "mtime";
const FS_COL_SIZE = "size";

// Folders always group before files; within a group the active column
// orders, with name as the fixed final tiebreaker (Model A, deterministic).
function fsCompare(key, dir) {
  const byName = (a, b) => baseCompare(a.name, b.name);
  const primary =
    key === FS_COL_DATE ? (a, b) => (a.mtime || 0) - (b.mtime || 0)
      : key === FS_COL_SIZE ? (a, b) => (a.size || 0) - (b.size || 0)
        : byName;
  return modelACompare({
    dir, primary, tiebreak: byName,
    group: (a, b) => (a.is_dir === b.is_dir ? 0 : a.is_dir ? -1 : 1),
  });
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
 *  constrains the dialog (defaults to content width). `message` may be a
 *  string, a Node, or a `(resolve) => Node` builder -- the builder form
 *  lets embedded links close the dialog (e.g. deep links to Settings). */
export function alert({ message, okLabel = "OK", messageClass, width } = {}) {
  return showDialog({
    label: "",
    width,
    body: (resolve, dialog) => {
      dialog.setAttribute("no-header", "");
      const p = document.createElement("p");
      p.className = messageClass ? `confirm-message ${messageClass}` : "confirm-message";
      if (typeof message === "function") p.appendChild(message(resolve));
      else if (message instanceof Node) p.appendChild(message);
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

// Standard width for confirm-style dialogs; shared so kin dialogs
// (e.g. the engine-drift chooser) pin the same footprint.
export const CONFIRM_DIALOG_WIDTH = "min(440px, 92vw)";

// Default dwell for error/warning toasts that carry a line worth reading.
export const TOAST_DURATION_MS = 8000;

/** Modal confirm. Resolves true on confirm, false otherwise.
 *  No title by design — the message itself carries the question, the
 *  destructive button label is the verb. (iOS-style.) */
export function confirm({
  message,
  okLabel = "OK",
  cancelLabel = "Cancel",
  destructive = false,
  width = CONFIRM_DIALOG_WIDTH,
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


function makeIconButton(iconName, ariaLabel) {
  const btn = document.createElement("wa-button");
  btn.size = "small";
  btn.className = "icon-only";
  if (ariaLabel) btn.setAttribute("aria-label", ariaLabel);
  const icon = document.createElement("wa-icon");
  if (iconName) icon.setAttribute("name", iconName);
  btn.appendChild(icon);
  return btn;
}

function buildFsPathBar() {
  const pathBar = document.createElement("div");
  pathBar.className = "fs-picker-pathbar";
  const backBtn = makeIconButton("reply", "Back to previous directory");
  backBtn.disabled = true;
  const upBtn = makeIconButton("arrow-up", "Up to parent directory");
  const pathInput = document.createElement("wa-input");
  pathInput.size = "small";
  pathInput.className = "fs-picker-path";
  pathBar.append(backBtn, upBtn, pathInput);
  return { pathBar, backBtn, upBtn, pathInput };
}

// Executable pickers' filter: on (the default) hides non-executables.
// Persisted under storageKey; onChange re-renders the listing.
function buildExeOnlyToggle(storageKey, onChange) {
  let on = loadRaw(storageKey) !== "false";
  const btn = makeIconButton();
  btn.classList.add("fs-exe-only-btn");
  const icon = btn.querySelector("wa-icon");
  const sync = () => {
    btn.title = on ? "Executables only" : "All files";
    btn.setAttribute("aria-pressed", String(on));
    icon.setAttribute("name", on ? FS_ICON_EXE : FS_ICON_FILE);
  };
  sync();
  btn.addEventListener("click", () => {
    on = !on;
    saveRaw(storageKey, String(on));
    sync();
    onChange();
  });
  return { btn, isOn: () => on };
}

// Real tables so the shared column-resize helper (.th-grip / .col-drag-line)
// can size Name/Date the way the engines and openings tables do.
// Split header/body table pattern -- see split-table.js.
function buildFsTable() {
  const tableWrap = document.createElement("div");
  tableWrap.className = "fs-picker-table-wrap";
  const headScroll = document.createElement("div");
  headScroll.className = "fs-picker-head-scroll";
  const headTable = document.createElement("table");
  headTable.className = "fs-picker-table fs-picker-head-table";
  markSelectable(headTable);
  const bodyScroll = document.createElement("div");
  bodyScroll.className = "fs-picker-body-scroll";
  const table = document.createElement("table");
  table.className = "fs-picker-table fs-picker-body-table";
  table.tabIndex = 0;
  markSelectable(table);
  suppressMultiClickSelect(table);

  function makeColgroup() {
    const colgroup = document.createElement("colgroup");
    for (let i = 0; i < FS_HEADERS.length; i++) colgroup.append(document.createElement("col"));
    return colgroup;
  }
  const headColgroup = makeColgroup();
  const bodyColgroup = makeColgroup();
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  const grips = [];
  FS_HEADERS.forEach((text, i) => {
    const th = document.createElement("th");
    th.textContent = text;
    // No grip on the last (Size) column -- it absorbs the remainder.
    if (i < FS_HEADERS.length - 1) {
      const grip = document.createElement("span");
      grip.className = "th-grip";
      th.append(grip);
      grips.push(grip);
    }
    headRow.append(th);
  });
  thead.append(headRow);
  const listing = document.createElement("tbody");
  headTable.append(headColgroup, thead);
  table.append(bodyColgroup, listing);
  headScroll.append(headTable);
  bodyScroll.append(table);
  tableWrap.append(headScroll, bodyScroll);
  wireSplitScroll(headScroll, bodyScroll);

  const headColEls = Array.from(headColgroup.querySelectorAll("col"));
  const bodyColEls = Array.from(bodyColgroup.querySelectorAll("col"));
  attachColumnResize({
    table: headTable,
    grips,
    overlayHost: tableWrap,
    storageKey: STORAGE_KEY.FS_PICKER_COL_PCTS,
    sizes: FS_COL_DEFAULT_PCTS.slice(),
    unit: "pct",
    applySizes: makePctApplySizes([headColEls, bodyColEls], FS_MIN_COL_PCT),
  });
  return { tableWrap, headTable, table, listing };
}

function buildFsRow(entry, dim) {
  const row = document.createElement("tr");
  row.className = FS_ENTRY_CLASS;
  if (dim) row.classList.add("dim");
  row.dataset.path = entry.path;

  const nameCell = document.createElement("td");
  nameCell.className = "fs-name-cell";
  const icon = document.createElement("wa-icon");
  icon.name = entry.is_dir ? FS_ICON_DIR : entry.is_executable ? FS_ICON_EXE : FS_ICON_FILE;
  const name = document.createElement("span");
  name.className = FS_NAME_CLASS;
  name.textContent = entry.name;
  nameCell.append(icon, name);

  const date = document.createElement("td");
  date.className = "fs-date";
  date.textContent = fmtDate(entry.mtime);

  const size = document.createElement("td");
  size.className = "fs-size";
  size.textContent = entry.is_dir ? "" : fmtSize(entry.size);

  row.append(nameCell, date, size);
  return row;
}

// Selecting through the row's own click keeps currentSelection and the
// Select button's enabled state in one place (the row click handler).
function selectFsRow(row) {
  row.click();
  row.scrollIntoView({ block: "nearest" });
}

// Arrows walk rows; Enter opens the selected (else first) row; typed chars
// jump to the next row whose name starts with them, a repeated single key
// cycling matches. Returns a reset for when the listing changes.
function wireFsTableKeys(table, listing) {
  wireArrowKeyNav(table, {
    rows: FS_ENTRY_SEL,
    selected: FS_SELECTED_SEL,
    select: (row) => row.click(),
  });

  let prefix = "";
  let lastPrefix = "";
  let cycleIdx = -1;
  let timer = null;
  const reset = () => { prefix = ""; lastPrefix = ""; cycleIdx = -1; };

  table.addEventListener("keydown", (ev) => {
    if (ev.ctrlKey || ev.altKey || ev.metaKey) return;
    if (ev.key === "Enter") {
      ev.preventDefault();
      const target = listing.querySelector(FS_SELECTED_SEL) ?? listing.querySelector(FS_ENTRY_SEL);
      target?.dispatchEvent(new MouseEvent("dblclick"));
      return;
    }
    if (ev.key.length !== 1) return;
    ev.preventDefault();
    clearTimeout(timer);
    prefix += ev.key.toLowerCase();
    const entries = Array.from(listing.querySelectorAll(FS_ENTRY_SEL));
    const names = entries.map(r => (r.querySelector(FS_NAME_SEL)?.textContent || "").toLowerCase());
    const isCycle = prefix === lastPrefix && prefix.length === 1;
    let hitIdx = -1;
    if (isCycle) {
      const start = (cycleIdx + 1) % entries.length;
      for (let i = 0; i < entries.length; i++) {
        const idx = (start + i) % entries.length;
        if (names[idx].startsWith(prefix)) { hitIdx = idx; break; }
      }
    } else {
      hitIdx = names.findIndex(n => n.startsWith(prefix));
    }
    if (hitIdx !== -1) {
      cycleIdx = hitIdx;
      selectFsRow(entries[hitIdx]);
    }
    lastPrefix = prefix;
    timer = setTimeout(reset, FS_JUMP_RESET_MS);
  });
  return reset;
}

/** Modal file/directory picker (browses server FS via /fs).
 *  Resolves to selected path or null. mode: "file" | "directory" | "executable".
 *  currentPath: the associated field's value, used as the starting point. */
export function pickFile({
  api,
  title = "Pick a file",
  mode = "file",
  currentPath = null,
  extensions = null,
} = {}) {
  if (!api) throw new Error("pickFile requires an api function");

  const wantsExec = mode === "executable";
  const wantsDir = mode === "directory";
  const extSet = extensions
    ? new Set(extensions.map((e) => e.toLowerCase()))
    : null;
  const hasExt = (name) => {
    const dot = name.lastIndexOf(".");
    return dot >= 0 && extSet.has(name.slice(dot).toLowerCase());
  };
  // Open at currentPath's parent with it preselected. An empty or bare-name
  // value falls back to recall, keyed by dialog title (same-titled pickers,
  // e.g. the two opening-book rows, share one).
  const recallKey = title;
  const { dir: currentDir, name: currentName } = splitPath((currentPath || "").trim());
  const initialPath = currentDir || recallLastDir(recallKey);

  return showDialog({
    label: title,
    width: "min(720px, 90vw)",
    defaultValue: null,
    body: (resolve, dialog) => {
      // Layout: path bar at top, listing in the middle, footer with select/cancel.
      const wrap = document.createElement("div");
      wrap.className = "fs-picker";
      const { pathBar, backBtn, upBtn, pathInput } = buildFsPathBar();
      const exeToggle = wantsExec
        ? buildExeOnlyToggle(EXE_ONLY_KEY_PREFIX + recallKey, () => applySort(sortCtrl.current()))
        : null;
      if (exeToggle) pathBar.append(exeToggle.btn);
      const { tableWrap, headTable, table, listing } = buildFsTable();
      const resetJump = wireFsTableKeys(table, listing);

      const selectBtn = document.createElement("wa-button");
      selectBtn.slot = "footer";
      selectBtn.size = "small";
      selectBtn.variant = "brand";
      selectBtn.textContent = wantsDir ? "Select directory" : "Select";
      selectBtn.disabled = true;

      let currentSelection = null;
      function setSelection(path) {
        currentSelection = path;
        selectBtn.disabled = path === null;
      }
      // Server-resolved listed dir and its parent, as of the last navigate.
      let listedDir = null;
      let parentDir = null;
      // Browser-style history: stack of visited paths. The current dir
      // sits at the top; Back pops one and re-navigates without pushing.
      const history = [];

      function eligible(entry) {
        if (entry.error) return false;
        if (wantsDir) return entry.is_dir;
        if (wantsExec) return entry.is_file && entry.is_executable;
        if (extSet) return entry.is_file && hasExt(entry.name);
        return entry.is_file;
      }

      function pick(path) {
        rememberLastDir(recallKey, listedDir);
        resolve(path);
      }

      // Entries for the current dir, kept so a sort can re-render without
      // re-fetching. The server already returns dirs-first, name-sorted.
      let currentEntries = [];

      function renderRows(entries) {
        listing.innerHTML = "";
        // Directory mode, exe-only mode, and extension filters hide
        // ineligible files; otherwise they show dimmed.
        const hideIneligible = wantsDir || extSet || exeToggle?.isOn();
        for (const entry of entries) {
          const ineligibleFile = !entry.is_dir && !eligible(entry);
          if (ineligibleFile && hideIneligible) continue;
          const row = buildFsRow(entry, ineligibleFile);
          // Dirs are eligible only in directory mode; elsewhere a click just
          // highlights one for navigation and a double click descends.
          row.addEventListener("click", () => {
            for (const sel of listing.querySelectorAll(FS_SELECTED_SEL)) {
              sel.classList.remove(FS_SELECTED_CLASS);
            }
            row.classList.add(FS_SELECTED_CLASS);
            table.focus({ preventScroll: true });
            setSelection(eligible(entry) ? entry.path : null);
          });
          row.addEventListener("dblclick", () => {
            if (entry.is_dir) navigate(entry.path);
            else if (eligible(entry)) pick(entry.path);
          });
          listing.append(row);
        }
      }

      function rowByPath(path) {
        return listing.querySelector(`${FS_ENTRY_SEL}[data-path="${CSS.escape(path)}"]`);
      }

      function selectRowByName(name) {
        const entry = currentEntries.find((e) => e.name === name);
        const row = entry && rowByPath(entry.path);
        if (row) selectFsRow(row);
      }

      // Re-render currentEntries under the active sort (or server default if
      // none). Preserves any active selection by path.
      function applySort(state) {
        const selected = listing.querySelector(FS_SELECTED_SEL)?.dataset.path;
        const rows = currentEntries.slice();
        if (state) rows.sort(fsCompare(state.key, state.dir));
        renderRows(rows);
        if (selected) {
          const row = rowByPath(selected);
          if (row) {
            row.classList.add(FS_SELECTED_CLASS);
            scrollSortedRowIntoView(listing, FS_SELECTED_SEL);
          }
        }
      }

      const sortCtrl = attachColumnSort({
        table: headTable,
        columns: [
          { key: FS_COL_NAME, firstDir: "asc" },
          { key: FS_COL_DATE, firstDir: "desc" },
          { key: FS_COL_SIZE, firstDir: "desc" },
        ],
        storageKey: STORAGE_KEY.FS_PICKER_SORT,
        onSort: applySort,
      });

      // quiet: fail without a toast, for callers with their own fallback.
      async function navigate(path, { push = true, quiet = false } = {}) {
        let body;
        try {
          const params = new URLSearchParams({ show_hidden: "true" });
          if (path) params.set("path", path);
          const url = `/fs?${params.toString()}`;
          body = await api("GET", url);
        } catch (e) {
          if (!quiet) toast(`Cannot list ${path}: ${e.message}`, { variant: "danger" });
          return false;
        }
        listedDir = body.path;
        parentDir = body.parent;
        pathInput.value = body.path;
        setSelection(wantsDir ? body.path : null);
        upBtn.disabled = !parentDir;
        if (push && history[history.length - 1] !== body.path) {
          history.push(body.path);
        }
        backBtn.disabled = history.length < 2;
        resetJump();
        currentEntries = body.entries;
        applySort(sortCtrl.current());
        table.focus({ preventScroll: true });
        return true;
      }

      upBtn.addEventListener("click", () => {
        if (parentDir) navigate(parentDir);
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
        if (currentSelection) pick(currentSelection);
      });

      wrap.append(pathBar, tableWrap);
      dialog.append(wrap, selectBtn);
      // If the initial dir is gone (deleted/renamed since), silently fall
      // back to home rather than show a toast.
      (async () => {
        if (initialPath && await navigate(initialPath, { quiet: true })) {
          if (currentDir) selectRowByName(currentName);
          return;
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

export const SETTINGS_TAB_ENGINES = "engines";
export const SETTINGS_TAB_ANALYSIS = "analysis";

// Deep-link focus target: class on the extended-thinking control, shared
// with the tab that builds it so the selector has one source of truth.
export const AI_THINKING_MODE_CLASS = "ai-thinking-mode";

/** Dispatch the deep-link event that opens the Settings dialog at
 *  `tab` (e.g. "engines"). `focusClass` optionally names a control within
 *  that tab to focus once the dialog is showing, so a link that exists to
 *  fix one setting lands on it. Main wires up the actual open in main.js. */
export function openSettings(tab, focusClass) {
  window.dispatchEvent(new CustomEvent(APP_EVT.OPEN_SETTINGS, {
    detail: { tab, focus: focusClass },
  }));
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

/** Gear toast action that deep-links into a Settings tab. `focusClass`
 *  optionally names the control to land on once the dialog is up. */
function openSettingsAction(tab, ariaLabel, focusClass) {
  return {
    icon: "gear",
    ariaLabel,
    onClick: () => openSettings(tab, focusClass),
  };
}

/** Canonical "open the Engines settings tab" toast action. Use this in
 *  client-side guards that want the same affordance as the server-side
 *  no_engine_configured handler. */
export const OPEN_ENGINES_ACTION =
  openSettingsAction(SETTINGS_TAB_ENGINES, "Open engine settings");

/** Canonical "open the Analysis settings tab" toast action, for failures
 *  the user fixes among the AI settings. */
export const OPEN_ANALYSIS_ACTION =
  openSettingsAction(SETTINGS_TAB_ANALYSIS, "Open AI settings");

// Map of well-known server error codes to inline toast actions.
// Centralized here so every reportError call site picks up the same
// remediation affordance (e.g. removing the active engine mid-session
// surfaces "no_engine_configured" through many endpoints, not just
// /game/new).
const ERROR_CODE_ACTIONS = {
  no_engine_configured: [OPEN_ENGINES_ACTION],
  // A verifier sub-run that never concluded -- usually the verifier round
  // cap is too low. Gear deep-links to the Analysis tab to raise it.
  no_verdict: [OPEN_ANALYSIS_ACTION],
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
 *  errors that must not auto-vanish before the user reads them.
 *  Optional `actions`: [{icon|label, ariaLabel?, onClick}] adds inline
 *  buttons between the message and the X, as flex siblings -- not inline
 *  text -- so they can't break away from the X when the message wraps.
 *  Optional `onDismiss` fires once, on X click or the returned fn. */
export function stickyToast(content, { variant = "neutral", stack, onDismiss, actions } = {}) {
  let dismiss;
  const grow = document.createElement("span");
  grow.className = "toast-grow";
  if (content instanceof Node) grow.appendChild(content);
  else grow.textContent = content;
  const node = document.createElement("span");
  node.className = "toast-sort-msg";
  node.append(grow);
  for (const a of actions || []) node.appendChild(buildToastActionButton(a));
  node.append(makeToastDismissBtn(() => dismiss?.()));
  const hide = toast(node, { variant, duration: 0, stack });
  let notified = false;
  dismiss = () => {
    hide();
    if (!notified) {
      notified = true;
      onDismiss?.();
    }
  };
  return dismiss;
}

// Matches a leading sentence: up to the first ./!/? that is followed by
// whitespace or end-of-string, so URLs (dots mid-token) stay intact.
const FIRST_SENTENCE_RE = /.+?[.!?]+(?=\s|$)/;
export const DETAILS_ICON = "circle-info";
const DETAILS_ARIA = "Error details";
// Standard width for details modals opened from a toast.
export const DETAILS_DIALOG_WIDTH = "min(560px, 92vw)";

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
  const actions = [{
    icon: DETAILS_ICON,
    ariaLabel: DETAILS_ARIA,
    onClick: async () => {
      await showVerboseErrorDetails(full);
      dismiss?.();
    },
  }];
  dismiss = stickyToast(summary, { variant, actions });
  return dismiss;
}

// Inline fixes for the AI failures we recognize, keyed by the exception
// class the server reports on the done event. The message stays the
// server's -- it phrases these itself, naming the model -- so this only
// says how to fix them.
const AI_ERROR_ACTIONS = {
  ThinkingUnsupported: [
    openSettingsAction(SETTINGS_TAB_ANALYSIS, "Open AI settings", AI_THINKING_MODE_CLASS),
  ],
};

/** Sticky danger toast for a failed AI run. A recognized failure carries
 *  the action that fixes it; anything else falls back to the plain verbose
 *  error. Returns the toast dismiss fn. */
export function reportAiError(name, detail) {
  const actions = AI_ERROR_ACTIONS[name];
  if (!actions) return reportVerboseError(detail || name);
  const { summary } = summarizeError(detail || name);
  return stickyToast(summary, { variant: "danger", actions });
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
    width: DETAILS_DIALOG_WIDTH,
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
 *  duration (0 / Infinity) makes it sticky with a dismiss button.
 *  Returns the sticky toast's dismiss fn, or null for a transient toast. */
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
  let dismiss = null;
  if (sticky) {
    dismiss = stickyToast(body, { variant: "danger" });
  } else {
    toast(body, { variant: "danger", duration: opts.duration });
  }
  ctx?.log?.(`${action}: ${message}`);
  return dismiss;
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
