// Settings dialog: tabbed Web Awesome dialog. Apply-on-change semantics --
// every toggle / input commits to the server immediately (debounced for
// text fields). No Save button. The X just closes.

import { apiErrorDetail, showDialog, toast } from "./dialogs.js";
import { APP_EVT } from "./app-events.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { loadRaw, saveRaw } from "./storage.js";
import { mountEngineList } from "./engines.js";
import { DEFAULT_BOARD_STYLE } from "./board-styles.js";
import { mqSettingsTopTabs } from "./breakpoints.js";
import { buildAnalysisTab } from "./settings-analysis-tab.js";
import { buildTournamentTab } from "./settings-tournament-tab.js";
import { buildCommonTab } from "./settings-common-tab.js";
import { makePathRow } from "./settings-path-row.js";
import { buildPlayTab } from "./settings-play-tab.js";
import { buildDisplayTab } from "./settings-display-tab.js";
import { debounce } from "./wb-utils.js";

const SETTINGS_ENGINES_COL_PCTS_KEY = STORAGE_KEY.ENGINES_SETTINGS_COL_PCTS;
const PLAYER_NAME_KEY = STORAGE_KEY.PLAYER_NAME;
export const PLAYER_NAME_DEFAULT = "Human";
const PLAYER_NAME_MAX_LEN = 32;
const SETTINGS_PATH = "/settings";
const TOURNAMENT_SETTINGS_PATH = "/api/tournament-settings";
const ENGINES_TAB = "engines";
const PUT_DEBOUNCE_MS = 400;
const DIALOG_WIDTH = "min(690px, 94vw)";
// Top-tab layouts get the full vertical share; side-tab layouts cap high
// enough that the Tournament tab's disclosure area fits without body scroll.
const DIALOG_HEIGHT_NARROW = "92vh";
const DIALOG_HEIGHT = `min(640px, ${DIALOG_HEIGHT_NARROW})`;

// One-time migration off the pre-0.5.2 localStorage name: if the server
// name is still unset, push the local one; either way drop the local key.
// Called at boot with the freshly fetched /settings payload.
export function migrateLegacyPlayerName(api, serverSettings) {
  const legacy = (loadRaw(PLAYER_NAME_KEY) || "").trim();
  if (!legacy) return;
  if (serverSettings?.player_name) {
    saveRaw(PLAYER_NAME_KEY, null);
    return;
  }
  // Clear only after the PUT lands, so a failed push retries next boot.
  api("PUT", SETTINGS_PATH, { player_name: legacy })
    .then(() => saveRaw(PLAYER_NAME_KEY, null))
    .catch(() => {});
}

// Persisted unit is always seconds (float); the UI shows the largest unit
// with no fractional remainder. Wire resolution is 1ms.
const SECONDS_PER_MINUTE = 60;
const MS_PER_SECOND = 1000;
const TENTHS_PER_SECOND = 10;
const UNIT_MIN = "min";
const UNIT_SEC = "sec";
const UNIT_MS = "ms";
const DURATION_UNITS = [
  { id: UNIT_MIN, label: UNIT_MIN, toSeconds: SECONDS_PER_MINUTE },
  { id: UNIT_SEC, label: UNIT_SEC, toSeconds: 1 },
  { id: UNIT_MS,  label: UNIT_MS,  toSeconds: 1 / MS_PER_SECOND },
];

function pickDurationUnit(seconds) {
  if (seconds === 0) return UNIT_SEC;
  // Whole minutes -> minutes (300 -> "5 min").
  if (seconds >= SECONDS_PER_MINUTE && seconds % SECONDS_PER_MINUTE === 0) return UNIT_MIN;
  // Tenth-of-a-second resolution fits "sec" (0.1, 0.5, 60.5 all stay readable).
  // Round to 1 decimal place to absorb float jitter.
  if (Math.round(seconds * TENTHS_PER_SECOND) === seconds * TENTHS_PER_SECOND) return UNIT_SEC;
  // Otherwise ms -- sub-100ms or multi-decimal values.
  return UNIT_MS;
}

function makeDurationRow({ label, seconds, minSeconds, onChange }) {
  const row = document.createElement("div");
  row.className = "settings-row";

  const lbl = document.createElement("label");
  lbl.textContent = label;

  let unitId = pickDurationUnit(seconds);
  const unitDef = () => DURATION_UNITS.find((u) => u.id === unitId);

  const input = document.createElement("wa-input");
  input.size = "small";
  input.type = "number";
  input.setAttribute("autocomplete", "off");
  // Render `sec` (and the floor) in the current display unit.
  const showSeconds = (sec) => {
    input.value = String(sec / unitDef().toSeconds);
    input.min = String(minSeconds / unitDef().toSeconds);
  };
  showSeconds(seconds);

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
    const sec = Math.round(raw * unitDef().toSeconds * MS_PER_SECOND) / MS_PER_SECOND;
    if (sec < minSeconds) return;
    onChange(sec);
  }

  input.addEventListener("input", commit);
  unit.addEventListener("change", () => {
    // Display-only: re-express the same seconds in the new unit. No
    // commit() -- the value didn't change; only its presentation did.
    const sec = (parseFloat(input.value) || 0) * unitDef().toSeconds;
    unitId = unit.value;
    showSeconds(sec);
  });

  const inputs = document.createElement("div");
  inputs.className = "settings-duration";
  inputs.append(input, unit);
  row.append(lbl, inputs);
  return row;
}

export async function openSettingsDialog({
  api, initialTab, focusClass, reloadPerspective,
}) {
  let initial;
  let tournamentInitial;
  let engineList = [];
  let activeEngineId = "";
  try {
    initial = await api("GET", SETTINGS_PATH);
    tournamentInitial = await api("GET", TOURNAMENT_SETTINGS_PATH);
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
    width: DIALOG_WIDTH,
    height: mqSettingsTopTabs.matches ? DIALOG_HEIGHT_NARROW : DIALOG_HEIGHT,
    body: (resolve, dialog) => {
      // Builders pass this signal to listeners on long-lived globals (e.g.
      // the mqMobile media query); aborting on close removes them all, so
      // reopening doesn't leak listeners pinning the detached panel.
      const dialogClosed = new AbortController();
      dialog.addEventListener("wa-after-hide", (ev) => {
        if (ev.target === dialog) dialogClosed.abort();
      });
      const onDialogShown = (fn) => {
        dialog.addEventListener("wa-after-show", function once(ev) {
          if (ev.target !== dialog) return;
          dialog.removeEventListener("wa-after-show", once);
          fn();
        });
      };

      // PUT a partial update; broadcast on success, toast on failure.
      const putAndNotify = async (path, patch) => {
        try {
          await api("PUT", path, patch);
          window.dispatchEvent(new CustomEvent(APP_EVT.SETTINGS_CHANGED));
        } catch (e) {
          toast(`Save failed: ${apiErrorDetail(e)}`, { variant: "danger" });
        }
      };
      const putSettings = (patch) => putAndNotify(SETTINGS_PATH, patch);
      const putSettingsDebounced = debounce(putSettings, PUT_DEBOUNCE_MS);
      const putTournamentSettings = (patch) => putAndNotify(TOURNAMENT_SETTINGS_PATH, patch);

      const tabs = document.createElement("wa-tab-group");
      const topTabs = mqSettingsTopTabs.matches;
      tabs.placement = topTabs ? "top" : "start";
      tabs.classList.add("dialog-side-tabs", "settings-tabs");
      if (topTabs) tabs.classList.add("settings-top-tabs");

      // --- Engines tab ---
      const enginesTab = document.createElement("wa-tab");
      enginesTab.panel = ENGINES_TAB;
      enginesTab.textContent = "Engines";
      const enginesPanel = document.createElement("wa-tab-panel");
      enginesPanel.name = ENGINES_TAB;
      enginesPanel.classList.add("settings-engines-panel");
      const enginesHost = document.createElement("div");
      enginesHost.className = "settings-engines-host";
      enginesPanel.appendChild(enginesHost);
      const mountEngines = () => mountEngineList(enginesHost, api, {
        colPctsKey: SETTINGS_ENGINES_COL_PCTS_KEY,
      });
      let enginesMounted = false;
      tabs.addEventListener("wa-tab-show", (ev) => {
        if (ev.detail?.name !== ENGINES_TAB || enginesMounted) return;
        enginesMounted = true;
        mountEngines();
      });

      // Shared path-row builder, bound to api for the file picker. Used by
      // the Gameplay, Common and Tournament tabs.
      const pathRow = makePathRow(api);

      // --- Play (Gameplay) tab ---
      const { tab: playTab, panel: playPanel } = buildPlayTab({
        initial, putSettings, putSettingsDebounced, makeDurationRow, pathRow,
        playerNameDefault: PLAYER_NAME_DEFAULT,
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
      const { tab: generalTab, panel: generalPanel, footer: generalFooter } = buildCommonTab({
        api, initial, putSettings, putSettingsDebounced, pathRow,
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
        if (ev.detail?.name !== analysisPanel.name || analysisMounted) return;
        analysisMounted = true;
        mountAnalysis();
      });
      // Insertion order IS the visual tab order. Pairing each tab with its
      // panel keeps tabs.append() from forgetting one of them.
      const TABS = new Map([
        { tab: generalTab,    panel: generalPanel },
        { tab: enginesTab,    panel: enginesPanel },
        { tab: playTab,       panel: playPanel },
        { tab: displayTab,    panel: displayPanel },
        { tab: analysisTab,   panel: analysisPanel },
        { tab: tournamentTab, panel: tournamentPanel },
      ].map((entry) => [entry.panel.name, entry]));

      const startTab = (TABS.get(initialTab) || TABS.get(generalPanel.name)).tab;
      startTab.setAttribute("active", "");
      // Common's footer shows only on that tab. WA renders the footer slot
      // only when filled and misses a re-fill, so force the re-render.
      const showGeneralFooter = (on) => {
        if (on) dialog.append(generalFooter);
        else generalFooter.remove();
        dialog.requestUpdate();
      };
      tabs.addEventListener("wa-tab-show", (ev) => showGeneralFooter(ev.detail?.name === generalPanel.name));
      showGeneralFooter(startTab === generalTab);
      // Engines as the start tab mounts only once the dialog is in the
      // document: mountEngineList measures the dialog body to size its
      // table, and a detached host yields a collapsed list.
      if (startTab === enginesTab) {
        enginesMounted = true;
        onDialogShown(mountEngines);
      } else if (startTab === analysisTab) {
        analysisMounted = true;
        mountAnalysis();
      }

      // Deep link naming a control: focus it once the dialog is showing, so
      // the setting the link exists to fix is the one under the cursor.
      // Silent no-op if the tab doesn't carry it.
      if (focusClass) onDialogShown(() => dialog.querySelector(`.${focusClass}`)?.focus());

      for (const { tab, panel } of TABS.values()) tabs.append(tab, panel);

      dialog.append(tabs);
    },
  });

  if (boardStyleDirty) {
    try { await boardStylePending; } catch {}
    // Re-PUT guards against the fire-and-forget change-handler racing a fast close.
    try { await api("PUT", SETTINGS_PATH, { board_style: boardStyleFinal }); } catch {}
    if (reloadPerspective) reloadPerspective();
    else location.reload();
  }
}
