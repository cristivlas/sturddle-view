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
//   UI flashing before the real state. Capped at READY_TIMEOUT_MS so a
//   perspective that forgets to signal doesn't stay invisible forever.

const READY_TIMEOUT_MS = 500;
// Must match the opacity transition on #perspective-root in styles.css.
const FADE_MS = 80;

const STORAGE_KEY = "sturddle:active-perspective";

export class PerspectiveRouter {
  constructor({ root, ctx }) {
    this._root = root;
    this._ctx = ctx;
    this._registry = new Map();
    this._active = null;
    this._activeController = null;
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

  async activate(id) {
    if (id === this._active) return true;
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
    // blank space.
    if (this._activeController) {
      this._root.classList.add("is-pending");
      await new Promise((r) => setTimeout(r, FADE_MS));
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
    try {
      localStorage.setItem(STORAGE_KEY, id);
    } catch {
      // localStorage may be unavailable; non-fatal.
    }
    this._activeController = (await persp.mount(this._root, this._ctx)) ?? null;
    if (this._activeController?.ready) {
      await Promise.race([
        this._activeController.ready,
        new Promise((r) => setTimeout(r, READY_TIMEOUT_MS)),
      ]);
    }
    this._root.classList.remove("is-pending");
    return true;
  }

  /** Activate the last-used perspective if known, else the first registered. */
  async activateInitial() {
    let id;
    try {
      id = localStorage.getItem(STORAGE_KEY);
    } catch {
      id = null;
    }
    if (!id || !this._registry.has(id)) {
      id = this._registry.keys().next().value;
    }
    if (id) await this.activate(id);
  }
}
