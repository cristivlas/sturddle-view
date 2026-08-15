// Perspective router: mounts/unmounts perspective modules into a single root.
//
// A perspective is an object: { id, label, mount(root, ctx) -> { unmount() } }.
// `mount` populates the root element and returns a controller. Switching
// perspectives calls the previous controller's `unmount` (if any), clears the
// root, and mounts the next.
//
// Optional controller hooks:
// - `canUnmount() -> Promise<bool>`: called before unmount; resolving to
//   false aborts the switch (controller stays mounted). Lets a perspective
//   guard against losing in-progress work.
// - `ready: Promise<void>`: resolves when the perspective has rendered its
//   real content (e.g. first server payload landed). The router keeps the
//   root faded out until this resolves, so the user doesn't see half-loaded
//   UI flashing before the real state.

import { STORAGE_KEY } from "./storage-keys.js";
import { loadRaw, saveRaw } from "./storage.js";

// Parse a CSS time string ("80ms" / "0.08s") to milliseconds. Multi-value
// lists fall back to the first entry.
function _cssMs(value) {
  const first = (value || "").split(",")[0].trim();
  if (first.endsWith("ms")) return parseFloat(first) || 0;
  if (first.endsWith("s")) return (parseFloat(first) || 0) * 1000;
  return 0;
}

const ACTIVE_PERSPECTIVE_KEY = STORAGE_KEY.ACTIVE_PERSPECTIVE;

// persist defaults to true (deliberate switch); the single source of truth
// for that default, shared by _activateImpl and the coalescing guard.
const PERSIST_DEFAULT = true;

function isDeliberate(opts) {
  return Boolean(opts.persist ?? PERSIST_DEFAULT);
}

export class PerspectiveRouter {
  constructor({ root, ctx }) {
    this._root = root;
    this._ctx = ctx;
    this._registry = new Map();
    this._active = null;
    this._activeController = null;
    this._activating = null;
    this._latestRequest = null;
  }

  register(perspective) {
    this._registry.set(perspective.id, perspective);
  }

  list() {
    return [...this._registry.values()];
  }

  activeId() {
    return this._active;
  }

  // persist: only a deliberate switch (nav click, explicit request) may
  // rewrite the remembered perspective. Involuntary switches -- e.g. the
  // disconnect fallback out of a server-backed tab -- must not, or a
  // transient drop silently becomes the user's new startup tab.
  //
  // Serialized: concurrent calls would interleave across _activateImpl's
  // awaits (fade, unmount, mount), double-mounting perspectives and leaking
  // the loser's controller (orphan toasts, live handlers with no dock).
  // Queued calls coalesce last-wins: superseded ones skip and return false.
  // Exception: an involuntary switch (persist:false) never displaces a
  // queued deliberate one -- the user's click wins, the fallback skips.
  async activate(id, opts = {}) {
    const req = { id, opts, queued: true };
    const latest = this._latestRequest;
    const displacesQueuedDeliberate =
      latest?.queued && isDeliberate(latest.opts) && !isDeliberate(opts);
    if (!displacesQueuedDeliberate) {
      this._latestRequest = req;
    }
    const run = (this._activating ?? Promise.resolve())
      .catch(() => {})
      .then(() => {
        req.queued = false;
        if (this._latestRequest !== req) return false;
        return this._activateImpl(id, opts);
      });
    this._activating = run;
    // Queue drained: drop the settled promise and request so they don't
    // outlive the switch. Trailing catch: this side chain must not turn
    // a mount failure (already surfaced via `run`) into an unhandled one.
    run.finally(() => {
      if (this._activating === run) {
        this._activating = null;
        this._latestRequest = null;
      }
    }).catch(() => {});
    return run;
  }

  async _activateImpl(id, { force = false, persist = PERSIST_DEFAULT } = {}) {
    if (!force && id === this._active) {
      // Already here, but a deliberate pick still claims the startup slot:
      // after an involuntary switch the stored id is the pre-drop tab, and
      // re-picking the current one must override it.
      if (persist) saveRaw(ACTIVE_PERSPECTIVE_KEY, id);
      return true;
    }
    if (!this._registry.has(id)) throw new Error(`unknown perspective: ${id}`);

    if (this._activeController?.canUnmount) {
      try {
        const ok = await this._activeController.canUnmount();
        if (!ok) return false;
      } catch (e) {
        console.error(`canUnmount(${this._active}) failed`, e);
      }
    }
    // Phase 1: fade the current perspective out (if any) before tearing
    // it down, so the user doesn't see a hard cut from old content to
    // blank space. Duration comes from the CSS transition on the root.
    if (this._activeController) {
      const fadeMs = _cssMs(getComputedStyle(this._root).transitionDuration);
      this._root.classList.add("is-pending");
      if (fadeMs > 0) await new Promise((r) => setTimeout(r, fadeMs));
    }

    if (this._activeController?.unmount) {
      try {
        await this._activeController.unmount();
      } catch (e) {
        console.error(`unmount(${this._active}) failed`, e);
      }
    }
    this._activeController = null;
    this._root.innerHTML = "";
    // Keep is-pending applied (or apply it for the first-load case where
    // there was no prior controller) so the new content is invisible
    // while it mounts and waits for `ready`.
    this._root.classList.add("is-pending");

    const persp = this._registry.get(id);
    this._active = id;
    if (persist) saveRaw(ACTIVE_PERSPECTIVE_KEY, id);
    try {
      this._activeController = (await persp.mount(this._root, this._ctx)) ?? null;
      if (this._activeController?.ready) {
        await this._activeController.ready;
      }
    } finally {
      // A mount/ready throw must not leave the root opacity-locked (a
      // permanently blank perspective); clear is-pending on every exit.
      this._root.classList.remove("is-pending");
    }
    return true;
  }

  /** Activate the last-used perspective if known, else the first registered. */
  async activateInitial() {
    let id = loadRaw(ACTIVE_PERSPECTIVE_KEY);
    if (!id || !this._registry.has(id)) {
      id = this._registry.keys().next().value;
    }
    if (id) await this.activate(id);
  }
}
