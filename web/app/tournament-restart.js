// Shared tournament start/restart machinery: the wipe-confirm handshake,
// engine-settings drift detection against the live registry, and the
// "Update & start" flow (see docs/engine-drift-restart.md). Used by the
// Arena/Studio start verb and the workspace's failure-banner Restart.

import {
  apiErrorDetail,
  confirm,
  CONFIRM_DIALOG_WIDTH,
  showDialog,
  toast,
  TOAST_DURATION_MS,
} from "./dialogs.js";
import { STATUS } from "./tournament-events.js";

const CONFIRM_WIPE_QS = "confirm_wipe=true";

// Exported for e2e: tests target the dialog and its verbs by these
// strings instead of hardcoding copies that could silently drift.
export const UPDATE_AND_START_LABEL = "Update & Start";
export const START_WITH_SAVED_LABEL = "Start with Saved";
export const DRIFT_DIALOG_LABEL = "Engine settings changed";

function buildRestartConfirm(name, games) {
  const message = games > 0
    ? `Restart "${name}" from scratch?\nAll ${games} recorded games will be permanently deleted.`
    : `Restart "${name}" from scratch?`;
  return {
    message,
    okLabel: "Restart",
    destructive: true,
    ...(games > 0 ? { messageClass: "confirm-message--multiline" } : {}),
  };
}

// ---- Registry <-> tournament ref helpers ---------------------------------

// The engine ref frozen into a tournament, built from a registry entry.
// UCI options are NOT copied here -- the server snapshots them from the
// registry at create/edit time (see api/tournaments._freeze_engines).
export function engineRefFromRegistry(e) {
  const ref = { id: e.id, name: e.name, cmd: e.path };
  if (Array.isArray(e.args) && e.args.length) ref.args = e.args.slice();
  if (e.env && typeof e.env === "object" && Object.keys(e.env).length) {
    ref.env = { ...e.env };
  }
  return ref;
}

// Frozen ref -> registry entry. Prefer id; fall back to name then cmd so
// a deleted-and-re-added engine (new id) still matches.
export function buildEngineMatcher(available) {
  const byId   = new Map(available.map((e) => [e.id,   e]));
  const byName = new Map(available.map((e) => [e.name, e]));
  const byCmd  = new Map(available.map((e) => [e.path, e]));
  return (ref) => byId.get(ref.id) || byName.get(ref.name) || byCmd.get(ref.cmd) || null;
}

// Resolve worst-case threading + hash from the picked engines and the
// global engine_default_* override. Must mirror the rescheck endpoint's
// formula so it sees the same numbers the user is committing to.
export function resolveResourceParams(template, pickedRegistry, globalDefaults) {
  function resolvedFor(engine, optName, fallback) {
    const opt = engine.options && engine.options[optName];
    if (opt != null && opt !== "") return Number(opt);
    const schema = engine.option_schema && engine.option_schema[optName];
    if (schema && schema.default != null) return Number(schema.default);
    return fallback;
  }
  const maxOver = (key, fallback) => {
    if (!pickedRegistry.length) return fallback;
    return pickedRegistry.reduce(
      (acc, e) => Math.max(acc, resolvedFor(e, key, fallback)),
      0,
    ) || fallback;
  };
  const max_threads = globalDefaults.threads
    ? Number(globalDefaults.threads)
    : maxOver("Threads", 1);
  const max_hash_mb = globalDefaults.hash_mb
    ? Number(globalDefaults.hash_mb)
    : maxOver("Hash", 16);

  return {
    parallel: Number(template.games_in_parallel || 1),
    max_threads,
    max_hash_mb,
    ponder: !!template.ponder,
    pin_affinity: !!template.pin_affinity,
    allow_oversubscribe: !!template.allow_oversubscribe,
  };
}

export async function loadGlobalEngineDefaults(api) {
  try {
    const s = await api("GET", "/settings");
    return {
      threads: s.engine_default_threads,
      hash_mb: s.engine_default_hash_mb,
    };
  } catch {
    return { threads: null, hash_mb: null };
  }
}

// ---- Drift detection ------------------------------------------------------

// JS mirror of fastchess.py _managed_option_keys: option names the
// tournament defines itself, casefolded. Keep the two in sync.
function managedOptionKeys(t) {
  const ed = t.engine_defaults || {};
  const tpl = t.template || {};
  const managed = new Set();
  if (ed.hash_mb != null) managed.add("hash");
  if (ed.threads != null) managed.add("threads");
  if (ed.syzygy_path) managed.add("syzygypath");
  if ("ponder" in tpl) managed.add("ponder");
  if (ed.book_path) managed.add("ownbook");
  return managed;
}

// Legacy refs may carry args as a single pre-joined string; it maps to a
// ONE-element argv, and "" to none (both mirroring fastchess.py) -- never
// split it.
function normalizedArgs(args) {
  if (args == null) return [];
  if (Array.isArray(args)) return args.map(String);
  return args ? [String(args)] : [];
}

// Order-independent dict fingerprint for compare.
function dictKey(d) {
  return JSON.stringify(
    Object.entries(d || {}).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)),
  );
}

function unmanagedOptions(options, managed) {
  return Object.fromEntries(
    Object.entries(options || {}).filter(([k]) => !managed.has(k.toLowerCase())),
  );
}

// True when any frozen engine ref differs from the live registry.
// Deleted engines (no match) are not drift -- the snapshot is all we
// have for them.
export function detectEngineDrift(t, available) {
  const match = buildEngineMatcher(available);
  const managed = managedOptionKeys(t);
  for (const ref of t.engines || []) {
    const entry = match(ref);
    if (!entry) continue;
    if (
      entry.name !== ref.name ||
      entry.path !== ref.cmd ||
      JSON.stringify(normalizedArgs(entry.args)) !== JSON.stringify(normalizedArgs(ref.args)) ||
      dictKey(entry.env) !== dictKey(ref.env) ||
      dictKey(unmanagedOptions(entry.options, managed)) !==
        dictKey(unmanagedOptions(ref.options, managed))
    ) {
      return true;
    }
  }
  return false;
}

// ---- Drift dialog ---------------------------------------------------------

// Three-way choice: "update" | "saved" | null (X/Esc = cancel; the footer
// keeps the standard two-button footprint, header shown for the X).
function openDriftDialog(t) {
  const games = t.standings?.games ?? 0;
  return showDialog({
    label: DRIFT_DIALOG_LABEL,
    defaultValue: null,
    width: CONFIRM_DIALOG_WIDTH,
    body: (resolve, dialog) => {
      const p = document.createElement("p");
      p.className = "confirm-message confirm-message--multiline";
      let message = `Engine settings changed since "${t.name}" was saved.`;
      if (games > 0) {
        message += `\n\nEither way, all ${games} recorded games will be permanently deleted.`;
      }
      p.textContent = message;

      const saved = document.createElement("wa-button");
      saved.size = "small";
      saved.slot = "footer";
      saved.textContent = START_WITH_SAVED_LABEL;
      saved.addEventListener("click", () => resolve("saved"));

      const update = document.createElement("wa-button");
      update.size = "small";
      update.slot = "footer";
      update.variant = "brand";
      update.textContent = UPDATE_AND_START_LABEL;
      update.addEventListener("click", () => resolve("update"));

      dialog.append(p, saved, update);
    },
  });
}

// ---- Update & start -------------------------------------------------------

// Re-freeze the tournament from the registry, then start it. Unmatched
// (deleted) refs round-trip verbatim, options included, so the roster
// never silently shrinks. Returns true when the start POST was issued.
async function updateAndStart(api, t, available) {
  const match = buildEngineMatcher(available);
  const engines = (t.engines || []).map((ref) => {
    const entry = match(ref);
    return entry ? engineRefFromRegistry(entry) : { ...ref };
  });
  const picked = (t.engines || []).map(match).filter(Boolean);

  // The frozen max_threads/max_hash_mb fold may be stale -- re-fold and
  // re-run the resource check before committing, as the Edit dialog does.
  const template = { ...t.template };
  const globalDefaults = await loadGlobalEngineDefaults(api);
  const resolved = resolveResourceParams(template, picked, globalDefaults);
  let rescheck;
  try {
    rescheck = await api("POST", "/api/tournaments/rescheck", resolved);
  } catch (e) {
    const detail = apiErrorDetail(e);
    const msg = (detail && detail.message) || detail || "Resource check failed";
    toast(typeof msg === "string" ? msg : String(msg), {
      variant: "danger", duration: TOAST_DURATION_MS,
    });
    return false;
  }
  for (const w of rescheck.warnings || []) {
    toast(`Warning: ${w.message}`, { variant: "warning", duration: TOAST_DURATION_MS });
  }
  template.max_threads = resolved.max_threads;
  template.max_hash_mb = resolved.max_hash_mb;

  // PATCH resets to idle and wipes games server-side, so the follow-up
  // start needs no confirm_wipe.
  await api("PATCH", `/api/tournaments/${t.id}`, { name: t.name, template, engines });
  await api("POST", `/api/tournaments/${t.id}/start`);
  return true;
}

// ---- The gate -------------------------------------------------------------

// Start `t` behind the drift gate: no drift keeps today's flow (wipe
// confirm for stopped/failed, then start); drift swaps the confirm for
// the three-way dialog. Returns true when a start was issued; false on
// cancel or rescheck blocker. API errors propagate to the caller.
export async function gatedStart({ api, log }, t) {
  const needsWipe = t.status === STATUS.STOPPED || t.status === STATUS.FAILED;
  const wipeQs = needsWipe ? `?${CONFIRM_WIPE_QS}` : "";

  // Best-effort: a registry fetch failure falls back to the ungated flow.
  // probe=false: the drift check reads stored profiles only and must not
  // pay a lazy schema probe (engine spawn) per unprobed registry entry.
  let available = null;
  try {
    available = (await api("GET", "/engines?probe=false")).engines || [];
  } catch (e) {
    log?.(`drift check skipped: ${e.message}`);
  }
  const drifted = available ? detectEngineDrift(t, available) : false;

  if (!drifted) {
    if (needsWipe) {
      const ok = await confirm(buildRestartConfirm(t.name, t.standings?.games ?? 0));
      if (!ok) return false;
    }
    await api("POST", `/api/tournaments/${t.id}/start${wipeQs}`);
    return true;
  }

  const choice = await openDriftDialog(t);
  if (choice === "saved") {
    await api("POST", `/api/tournaments/${t.id}/start${wipeQs}`);
    return true;
  }
  if (choice === "update") return updateAndStart(api, t, available);
  return false;
}
