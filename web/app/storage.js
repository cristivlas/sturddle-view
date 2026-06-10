// localStorage JSON helpers. Both read and write swallow failures
// (private-mode quotas, disabled storage, corrupt JSON) so callers can
// treat persistence as best-effort without scattering try/catch.

// Parse JSON from localStorage; return `fallback` (default null) on miss,
// corrupt JSON, or any storage error.
export function loadJson(key, fallback = null) {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

// Serialize and store; best-effort (swallows quota/disabled errors).
export function saveJson(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Storage may be disabled (private mode quotas); best-effort.
  }
}

// Remove a key; best-effort (swallows disabled-storage errors).
export function removeKey(key) {
  try {
    localStorage.removeItem(key);
  } catch {
    // Storage may be disabled; best-effort.
  }
}

// Read a raw string value; return `fallback` (default null) on miss or
// any storage error. For non-JSON values (flags, ids, enums).
export function loadRaw(key, fallback = null) {
  try {
    const v = localStorage.getItem(key);
    return v === null ? fallback : v;
  } catch {
    return fallback;
  }
}

// Store a raw string value; best-effort (swallows quota/disabled errors).
// Passing null/undefined removes the key (mirrors set-or-clear callers).
export function saveRaw(key, value) {
  try {
    if (value === null || value === undefined) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // Storage may be disabled (private mode quotas); best-effort.
  }
}
