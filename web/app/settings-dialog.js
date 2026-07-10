// Settings dialog: tabbed Web Awesome dialog. Apply-on-change semantics —
// every toggle / input commits to the server immediately (debounced for
// text fields). No Save button. The X just closes.

import { apiErrorDetail, showDialog, toast } from "./dialogs.js";
import { APP_EVT } from "./app-events.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { loadRaw } from "./storage.js";
import { mountEngineList } from "./engines.js";
import { DEFAULT_BOARD_STYLE } from "./board-styles.js";
import { mqNarrowDialog } from "./breakpoints.js";
import { buildAnalysisTab } from "./settings-analysis-tab.js";
import { buildTournamentTab } from "./settings-tournament-tab.js";
import { buildCommonTab } from "./settings-common-tab.js";
import { makePathRow } from "./settings-path-row.js";
import { buildPlayTab } from "./settings-play-tab.js";
import { buildDisplayTab } from "./settings-display-tab.js";
import { debounce } from "./wb-utils.js";

const SETTINGS_ENGINES_COL_PCTS_KEY = STORAGE_KEY.ENGINES_SETTINGS_COL_PCTS;
export const PLAYER_NAME_KEY = STORAGE_KEY.PLAYER_NAME;
export const PLAYER_NAME_DEFAULT = "Human";
const PLAYER_NAME_MAX_LEN = 32;

export function getConfiguredPlayerName() {
  return loadRaw(PLAYER_NAME_KEY) || PLAYER_NAME_DEFAULT;
}

// Persisted unit is always seconds (float). The UI picks the most natural
// display unit on load (largest unit with no fractional remainder) and
// converts back to seconds on save. UCI/cutechess/fastchess all support
// sub-second values; the wire protocol resolution is 1ms.
const DURATION_UNITS = [
  { id: "min", label: "min", toSeconds: 60 },
  { id: "sec", label: "sec", toSeconds: 1 },
  { id: "ms",  label: "ms",  toSeconds: 0.001 },
];

function pickDurationUnit(seconds) {
  if (seconds === 0) return "sec";
  // Whole minutes -> minutes (300 -> "5 min").
  if (seconds >= 60 && seconds % 60 === 0) return "min";
  // Tenth-of-a-second resolution fits "sec" (0.1, 0.5, 60.5 all stay readable).
  // Round to 1 decimal place to absorb float jitter.
  if (Math.round(seconds * 10) === seconds * 10) return "sec";
  // Otherwise ms — sub-100ms or multi-decimal values.
  return "ms";
}

function makeDurationRow({ label, seconds, minSeconds, onChange }) {
  const row = document.createElement("div");
  row.className = "settings-row";

  const lbl = document.createElement("label");
  lbl.textContent = label;

  const initialUnit = pickDurationUnit(seconds);
  let unitId = initialUnit;
  const unitDef = () => DURATION_UNITS.find((u) => u.id === unitId);

  const input = document.createElement("wa-input");
  input.size = "small";
  input.type = "number";
  input.setAttribute("autocomplete", "off");
  input.value = String(seconds / unitDef().toSeconds);
  input.min = String(minSeconds / unitDef().toSeconds);

  const unit = document.createElement("wa-select");
  unit.size = "small";
  unit.value = unitId;
  for (const u of DURATION_UNITS) {
    const opt = document.createElement("wa-option");
    opt.value = u.id;
    opt.textContent = u.label;
    unit.appendChild(opt);
  }

  function commit() {
    const raw = parseFloat(input.value);
    if (!Number.isFinite(raw) || raw < 0) return;
    const sec = Math.round(raw * unitDef().toSeconds * 1000) / 1000;  // 1ms resolution
    if (sec < minSeconds) return;
    onChange(sec);
  }

  input.addEventListener("input", commit);
  unit.addEventListener("change", () => {
    // Display-only: convert the shown value into the new unit so the
    // underlying seconds stays the same. No commit() — the value didn't
    // change; only its presentation did.
    const oldDef = DURATION_UNITS.find((u) => u.id === unitId);
    const sec = (parseFloat(input.value) || 0) * oldDef.toSeconds;
    unitId = unit.value;
    input.value = String(sec / unitDef().toSeconds);
    input.min = String(minSeconds / unitDef().toSeconds);
  });

  const inputs = document.createElement("div");
  inputs.className = "settings-duration";
  inputs.append(input, unit);
  row.append(lbl, inputs);
  return row;
}

export async function openSettingsDialog({ api, initialTab, getActivePerspective, reloadPerspective }) {
  let initial;
  let tournamentInitial;
  let engineList = [];
  let activeEngineId = "";
  try {
    initial = await api("GET", "/settings");
    tournamentInitial = await api("GET", "/api/tournament-settings");
    // Engine list feeds the analysis-engine dropdown shown when the
    // provider is "Engine only".
    try {
      const enginesInfo = await api("GET", "/engines");
      activeEngineId = enginesInfo.selected_id || "";
      engineList = enginesInfo.engines || [];
    } catch { /* ignore */ }
  } catch (e) {
    toast(`Couldn't load settings: ${e.message}`, { variant: "danger" });
    return;
  }

  // If board_style is changed during this dialog session, reload after
  // close so the new style takes effect on the live board. Game state
  // lives server-side and is restored via /game/sync on remount.
  const initialStyle = initial.board_style || DEFAULT_BOARD_STYLE;
  let boardStyleDirty = false;
  let boardStylePending = null;
  let boardStyleFinal = initialStyle;

  await showDialog({
    label: "Settings",
    width: "min(690px, 94vw)",
    // Phones get the full vertical share; desktops cap high enough that the
    // Tournament tab's fixed-height disclosure area (ttf-sections) fits without
    // the body scrolling.
    height: mqNarrowDialog.matches ? "92vh" : "min(640px, 92vh)",
    body: (resolve, dialog) => {
      // Listeners on long-lived globals (e.g. the mqMobile media query) must
      // be torn down when the dialog closes, or each open leaks a pair and
      // pins the detached panel. Builders take this signal and pass it to
      // addEventListener; aborting on close removes them all at once.
      const dialogClosed = new AbortController();
      dialog.addEventListener("wa-after-hide", (ev) => {
        if (ev.target === dialog) dialogClosed.abort();
      });

      // ---- helper: PUT a partial settings update; toast on failure. ----
      const putSettings = async (patch) => {
        try {
          await api("PUT", "/settings", patch);
          window.dispatchEvent(new CustomEvent(APP_EVT.SETTINGS_CHANGED));
        } catch (e) {
          toast(`Save failed: ${apiErrorDetail(e)}`, { variant: "danger" });
        }
      };
      const putSettingsDebounced = debounce(putSettings, 400);

      const putTournamentSettings = async (patch) => {
        try {
          tournamentInitial = await api("PUT", "/api/tournament-settings", patch);
          window.dispatchEvent(new CustomEvent(APP_EVT.SETTINGS_CHANGED));
        } catch (e) {
          toast(`Save failed: ${apiErrorDetail(e)}`, { variant: "danger" });
        }
      };

      const tabs = document.createElement("wa-tab-group");
      const isNarrow = mqNarrowDialog.matches;
      tabs.placement = isNarrow ? "top" : "start";
      tabs.classList.add("dialog-side-tabs", "settings-tabs");

      // --- Engines tab ---
      const enginesTab = document.createElement("wa-tab");
      enginesTab.panel = "engines";
      enginesTab.textContent = "Engines";
      const enginesPanel = document.createElement("wa-tab-panel");
      enginesPanel.name = "engines";
      enginesPanel.classList.add("settings-engines-panel");
      const enginesHost = document.createElement("div");
      enginesHost.className = "settings-engines-host";
      enginesPanel.appendChild(enginesHost);
      let enginesMounted = false;
      tabs.addEventListener("wa-tab-show", (ev) => {
        if (ev.detail?.name !== "engines" || enginesMounted) return;
        enginesMounted = true;
        mountEngineList(enginesHost, api, {
          colPctsKey: SETTINGS_ENGINES_COL_PCTS_KEY,
        });
      });

      // Shared path-row builder, bound to api for the file picker. Used by
      // the Gameplay, Common and Tournament tabs.
      const pathRow = makePathRow(api);

      // --- Play (Gameplay) tab ---
      const { tab: playTab, panel: playPanel } = buildPlayTab({
        initial, putSettings, putSettingsDebounced, makeDurationRow, pathRow,
        playerNameDefault: PLAYER_NAME_DEFAULT,
        playerNameKey: PLAYER_NAME_KEY,
        playerNameMaxLen: PLAYER_NAME_MAX_LEN,
      });

      // --- Display tab ---
      // Board-style change must reload on dialog close; the dialog owns that
      // dirty-tracking, so the builder calls back here with the new style.
      const { tab: displayTab, panel: displayPanel } = buildDisplayTab({
        initial, putSettings, initialStyle, signal: dialogClosed.signal,
        onBoardStyleChange: (styleId) => {
          boardStyleFinal = styleId;
          boardStyleDirty = boardStyleFinal !== initialStyle;
          // Track the in-flight save so we can await it before reloading on
          // close -- fire-and-forget would race location.reload().
          boardStylePending = putSettings({ board_style: boardStyleFinal });
        },
      });

      // --- Common tab ---
      const { tab: generalTab, panel: generalPanel } = buildCommonTab({
        initial, putSettings, putSettingsDebounced, pathRow,
      });

      // --- Tournament tab ---
      const { tab: tournamentTab, panel: tournamentPanel } = buildTournamentTab({
        tournamentInitial, putTournamentSettings, pathRow, debounce,
      });

      // --- AI Analysis tab ---
      const { tab: analysisTab, panel: analysisPanel, mount: mountAnalysis } = buildAnalysisTab({
        api, initial, dialog, engineList, activeEngineId,
        putSettings, putSettingsDebounced, debounce,
      });
      // Defer the model row (and its /models fetch) to first activation so
      // opening Settings on another tab issues no provider call.
      let analysisMounted = false;
      tabs.addEventListener("wa-tab-show", (ev) => {
        if (ev.detail?.name !== "analysis" || analysisMounted) return;
        analysisMounted = true;
        mountAnalysis();
      });
      // Map preserves insertion order by spec -- the iteration order here
      // IS the visual tab order. Each entry pairs the tab control with
      // its panel, eliminating the parallel-list bug class where one of
      // them gets forgotten in tabs.append().
      const TABS = new Map([
        ["general",    { tab: generalTab,    panel: generalPanel }],
        ["engines",    { tab: enginesTab,    panel: enginesPanel }],
        ["play",       { tab: playTab,       panel: playPanel }],
        ["display",    { tab: displayTab,    panel: displayPanel }],
        ["analysis",   { tab: analysisTab,   panel: analysisPanel }],
        ["tournament", { tab: tournamentTab, panel: tournamentPanel }],
      ]);

      const startTab = (TABS.get(initialTab) || TABS.get("general")).tab;
      startTab.setAttribute("active", "");
      // Eager mount when Engines is the starting tab: defer until the
      // dialog is actually in the document so mountEngineList can measure
      // its surroundings (it reads getBoundingClientRect on the dialog
      // body to size the table-wrap). Doing it inline here would leave
      // the host detached and yield a collapsed list.
      if (startTab === enginesTab) {
        enginesMounted = true;
        dialog.addEventListener("wa-after-show", function once(ev) {
          if (ev.target !== dialog) return;
          dialog.removeEventListener("wa-after-show", once);
          mountEngineList(enginesHost, api, {
            colPctsKey: SETTINGS_ENGINES_COL_PCTS_KEY,
          });
        });
      } else if (startTab === analysisTab) {
        analysisMounted = true;
        mountAnalysis();
      }

      for (const { tab, panel } of TABS.values()) tabs.append(tab, panel);

      dialog.append(tabs);
    },
  });

  if (boardStyleDirty) {
    try { await boardStylePending; } catch {}
    // Re-PUT guards against the fire-and-forget change-handler racing a fast close.
    try { await api("PUT", "/settings", { board_style: boardStyleFinal }); } catch {}
    if (reloadPerspective) reloadPerspective();
    else location.reload();
  }
}
