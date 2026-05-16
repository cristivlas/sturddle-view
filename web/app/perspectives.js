// Perspective router: mounts/unmounts perspective modules into a single root.
//
// A perspective is an object: { id, label, mount(root, ctx) -> { unmount() } }.
// `mount` populates the root element and returns a controller. Switching
// perspectives calls the previous controller's `unmount` (if any), clears the
// root, and mounts the next.
//
// Optional controller hook: `canUnmount() -> Promise<bool>`. Called before
// unmount; if it resolves to false, the switch is aborted (controller stays
// mounted). Lets a perspective guard against losing in-progress work (e.g.
// confirm before discarding an open editor).

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
    if (this._activeController?.unmount) {
      try {
        await this._activeController.unmount();
      } catch (e) {
        console.error(`unmount(${this._active}) failed`, e);
      }
    }
    this._activeController = null;
    this._root.innerHTML = "";

    const persp = this._registry.get(id);
    this._active = id;
    try {
      localStorage.setItem(STORAGE_KEY, id);
    } catch {
      // localStorage may be unavailable; non-fatal.
    }
    this._activeController = (await persp.mount(this._root, this._ctx)) ?? null;
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
