// Common settings tab: global engine defaults (UCI Threads/Hash, Syzygy,
// opening book). The PGN directory lives on the Gameplay tab (HVE-only).
//
// pathRow is shared with the Gameplay and Tournament tabs, so it's passed in.

import { apiErrorDetail, confirm, toast } from "./dialogs.js";
import { BOOK_EXTENSIONS } from "./tournament-template-form.js";

const COMMON_TAB = "general";
// Wire keys: server/sturddle_view/config.py.
const ANALYSIS_THREADS_KEY = "engine_default_analysis_threads";
const THREADS_KEY = "engine_default_threads";
const HASH_MB_KEY = "engine_default_hash_mb";
const SYZYGY_PATH_KEY = "engine_default_syzygy_path";
const BOOK_PATH_KEY = "engine_default_book_path";
const BOOK_PLIES_KEY = "engine_default_book_plies";
const BOOK_ORDER_KEY = "engine_default_book_order";
const OPTION_MIN = "1";
const RESET_PATH = "/settings/reset";
// Same icon as Play's Take back.
const RESET_ICON = "rotate-left";
const RESET_CONFIRM_MESSAGE =
  "Reset all settings to defaults? Engines, API keys and window layouts are kept.";

export function buildCommonTab({ api, initial, putSettings, putSettingsDebounced, pathRow }) {
  const generalTab = document.createElement("wa-tab");
  generalTab.panel = COMMON_TAB;
  generalTab.textContent = "Common";
  const generalPanel = document.createElement("wa-tab-panel");
  generalPanel.name = COMMON_TAB;

  function makeNumInput(labelText, key, opts = {}) {
    const { max } = opts;
    const item = document.createElement("div");
    const lbl = document.createElement("label");
    lbl.textContent = labelText;
    const input = document.createElement("wa-input");
    input.size = "small";
    input.type = "number";
    input.min = OPTION_MIN;
    if (max != null) input.max = String(max);
    input.autocomplete = "off";
    input.placeholder = "default";
    const cur = initial[key];
    if (cur != null) input.value = String(cur);
    input.addEventListener("input", () => {
      const raw = (input.value || "").trim();
      if (raw === "") return putSettingsDebounced({ [key]: null });
      const n = Number(raw);
      if (Number.isFinite(n)) putSettingsDebounced({ [key]: n });
    });
    item.append(lbl, input);
    return item;
  }

  // Threads subgroup: bordered block holding Analysis + Play threads (same
  // UCI knob, two contexts); Hash sits alongside as a peer with a matching
  // border so the two visually pair.
  function makeThreadsHashRow(maxThreads) {
    const row = document.createElement("div");
    row.className = "settings-num-group settings-panel-aligned";
    const threads = document.createElement("div");
    threads.className = "settings-threads-subgroup";
    const hdr = document.createElement("div");
    hdr.className = "settings-threads-subgroup-hdr";
    hdr.textContent = "Threads";
    hdr.title = "UCI Threads -- sent to engines on launch";
    const inner = document.createElement("div");
    inner.className = "settings-threads-subgroup-inner";
    inner.append(
      makeNumInput("Analysis", ANALYSIS_THREADS_KEY, { max: maxThreads }),
      makeNumInput("Play", THREADS_KEY, { max: maxThreads }),
    );
    threads.append(hdr, inner);
    // Hash: just the existing input, with a border to match the
    // threads box's frame.
    const hash = makeNumInput("Hash (MB)", HASH_MB_KEY);
    hash.classList.add("settings-hash-boxed");
    row.append(threads, hash);
    return row;
  }

  function bookPliesAndOrderRow() {
    const row = document.createElement("div");
    row.className = "settings-row settings-panel-aligned";
    const lbl = document.createElement("label");
    lbl.textContent = "Book ply depth";

    const plies = document.createElement("wa-input");
    plies.size = "small";
    plies.type = "number";
    plies.setAttribute("min", OPTION_MIN);
    plies.setAttribute("autocomplete", "off");
    plies.placeholder = "engine default";
    const curPlies = initial[BOOK_PLIES_KEY];
    if (curPlies != null) plies.value = String(curPlies);
    plies.addEventListener("input", () => {
      const raw = (plies.value || "").trim();
      if (raw === "") return putSettingsDebounced({ [BOOK_PLIES_KEY]: null });
      const n = Number(raw);
      if (Number.isFinite(n)) putSettingsDebounced({ [BOOK_PLIES_KEY]: n });
    });

    const order = document.createElement("wa-select");
    order.size = "small";
    order.setAttribute("distance", "4");
    order.value = initial[BOOK_ORDER_KEY] ?? "sequential";
    for (const [val, label] of [["sequential", "Sequential"], ["random", "Random"]]) {
      const opt = document.createElement("wa-option");
      opt.value = val;
      opt.textContent = label;
      order.append(opt);
    }
    order.addEventListener("change", () => {
      putSettings({ [BOOK_ORDER_KEY]: order.value });
    });

    const controls = document.createElement("div");
    controls.className = "settings-row-pair";
    controls.append(plies, order);

    row.append(lbl, controls);
    // Ply depth + order only apply when a book is configured; greyed out
    // (values retained server-side) until then.
    const setEnabled = (on) => {
      plies.disabled = !on;
      order.disabled = !on;
    };
    return { row, setEnabled };
  }

  // Resets the server settings file; the reload repaints every consumer.
  function resetAllRow() {
    const row = document.createElement("div");
    row.slot = "footer";
    row.className = "settings-reset-row";
    const btn = document.createElement("wa-button");
    btn.size = "small";
    btn.className = "settings-reset-btn";
    const icon = document.createElement("wa-icon");
    icon.name = RESET_ICON;
    icon.slot = "start";
    btn.append(icon, "Reset all settings");
    btn.addEventListener("click", async () => {
      const ok = await confirm({
        message: RESET_CONFIRM_MESSAGE, okLabel: "Reset", destructive: true,
      });
      if (!ok) return;
      try {
        await api("POST", RESET_PATH);
      } catch (e) {
        toast(`Reset failed: ${apiErrorDetail(e)}`, { variant: "danger" });
        return;
      }
      location.reload();
    });
    row.append(btn);
    return row;
  }

  const { row: bookOptionsRow, setEnabled: setBookOptionsEnabled } = bookPliesAndOrderRow();
  setBookOptionsEnabled(!!initial[BOOK_PATH_KEY]);

  generalPanel.append(
    makeThreadsHashRow(initial.host?.logical_cores),
    pathRow(
      "SyzygyPath",
      initial[SYZYGY_PATH_KEY] || "",
      "directory",
      "Pick Syzygy tablebase directory",
      (p) => putSettings({ [SYZYGY_PATH_KEY]: p }),
    ),
    pathRow(
      "Opening book",
      initial[BOOK_PATH_KEY] || "",
      "file",
      "Pick opening book",
      (p) => {
        putSettings({ [BOOK_PATH_KEY]: p });
        setBookOptionsEnabled(!!p);
      },
      { extensions: BOOK_EXTENSIONS },
    ),
    bookOptionsRow,
  );

  return { tab: generalTab, panel: generalPanel, footer: resetAllRow() };
}
