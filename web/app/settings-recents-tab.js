// Settings > Recents tab: the recent-imports history as a fork tree, with
// per-row Open and Delete. A parent's Delete cascades over its forks after a
// confirm. Fetches on mount and again on RECENTS_CHANGED while the dialog is
// open; Settings is rebuilt per open, so each open starts fresh.

import { apiErrorDetail, apiErrorObject, confirm, reportError, toast } from "./dialogs.js";
import { APP_EVT } from "./app-events.js";
import {
  currentEntryTag, formatSummary, HASH_PREVIEW_LEN, RECENT_IMPORTS_PATH,
} from "./import-position-dialog.js";
import { createWrapSizer } from "./list-wrap-sizer.js";
import {
  activatePlay, confirmOpenInView, GAME_STATUS_PATH, importForView, OPEN_VERDICT,
} from "./open-in-view.js";
import { fmtMoveNo, makeIconButton } from "./wb-utils.js";

const TAB_NAME = "recents";
const TAB_LABEL = "Recents";
const ROW_CLASS = "recents-row";
const BTN_CLASS = "recents-btn";
const SELECTED_CLASS = "selected";
const DEPTH_VAR = "--recents-depth";
const MUTED_CLASS = "muted";
const EMPTY_TEXT = "No recent games.";
const ICON_EXPANDED = "caret-down";
const ICON_COLLAPSED = "caret-right";
const ICON_OPEN = "eye";
const ICON_DELETE = "trash";
const OPEN_TITLE = "Open";
const EXPAND_TITLE = "Expand";
const COLLAPSE_TITLE = "Collapse";
const VARIANT_WARNING = "warning";
const VARIANT_DANGER = "danger";
const DELETE_TITLE = "Delete";
const DELETE_BLOCKED_TITLE = "Its forks include the game in view or in play";
const DELETE_OK_LABEL = "Delete";
const OPEN_LEAVE_MSG = "Open this game and leave the current one? It will be saved to Recents.";
const OPEN_OK_LABEL = "Open";
const OPEN_FAILED = "Open failed";
const LOAD_FAILED = "Couldn't load recents";
const CASCADE_QUERY = "?cascade=1";
const ERR_ACTIVE_IN_SUBTREE = "active_in_subtree";
const ACTIVE_IN_SUBTREE_MSG = "Can't delete: its forks include the game in view or in play.";
const DATE_FORMAT = { month: "short", day: "numeric" };
const ARIA_LEVEL_BASE = 1;

function deleteConfirmMsg(n) {
  return `Delete with ${n} ${n === 1 ? "fork" : "forks"}?`;
}

// Forest of {row, parent, children} from the flat list. The server lists rows by
// ts desc and children are pushed in that order, so siblings stay sorted.
// A row whose parent isn't listed renders as a root.
function buildForest(rows) {
  const nodes = rows.map((row) => ({ row, parent: null, children: [] }));
  const byId = new Map();
  for (const node of nodes) {
    for (const id of [node.row.game_id, ...(node.row.alias_ids || [])]) {
      if (id) byId.set(id, node);
    }
  }
  const roots = [];
  for (const node of nodes) {
    const parent = node.row.parent_game_id ? byId.get(node.row.parent_game_id) : null;
    if (parent && parent !== node) {
      node.parent = parent;
      parent.children.push(node);
    } else {
      roots.push(node);
    }
  }
  return roots;
}

function descendantCount(node) {
  return node.children.reduce((n, c) => n + 1 + descendantCount(c), 0);
}

function subtreeHasCurrent(node, tagOf) {
  return node.children.some((c) => tagOf(c.row) || subtreeHasCurrent(c, tagOf));
}

// The fork's first own move: ply `fork_ply` is the next to be played.
function forkLabel(row) {
  const ply = row.fork_ply;
  return ply ? `move ${fmtMoveNo(ply, ply % 2 === 0)}` : "";
}

function iconButton({ icon, title, onClick }) {
  return makeIconButton({ className: BTN_CLASS, icon, title, onClick });
}

// Delete a row and, after a confirm, its forks.
async function deleteNode(api, node) {
  const { hash } = node.row;
  const forks = descendantCount(node);
  if (forks > 0 && !await confirm({
    message: deleteConfirmMsg(forks), okLabel: DELETE_OK_LABEL, destructive: true,
  })) return;
  try {
    // Always cascade: a row whose only refs are dead renders as a leaf,
    // and a plain delete would 409 on it.
    await api("DELETE", `${RECENT_IMPORTS_PATH}/${hash}${CASCADE_QUERY}`);
  } catch (e) {
    if (apiErrorObject(e)?.error === ERR_ACTIVE_IN_SUBTREE) {
      toast(ACTIVE_IN_SUBTREE_MSG, { variant: VARIANT_WARNING });
    } else {
      toast(apiErrorDetail(e), { variant: VARIANT_DANGER });
    }
    return;
  }
  // Our own listener refetches and redraws; Play refreshes its x-game info.
  window.dispatchEvent(new CustomEvent(APP_EVT.RECENTS_CHANGED));
}

// Open a row's game in Play (the tournament Review flow), then close Settings.
async function openNode(api, node, close) {
  const { hash, summary } = node.row;
  let status;
  try {
    status = await api("GET", GAME_STATUS_PATH);
  } catch (e) {
    reportError(null, OPEN_FAILED, e);
    return;
  }
  const verdict = await confirmOpenInView(status, {
    hash, summary, leaveMessage: OPEN_LEAVE_MSG, okLabel: OPEN_OK_LABEL,
  });
  if (verdict === OPEN_VERDICT.CANCEL) return;
  if (verdict === OPEN_VERDICT.PROCEED) {
    try {
      // Fetched only now: the GET bumps the row's ts, which a cancel
      // must not do.
      const rec = await api("GET", `${RECENT_IMPORTS_PATH}/${hash}`);
      await importForView({ text: rec.text, format: rec.format });
    } catch (e) {
      reportError(null, OPEN_FAILED, e);
      return;
    }
  }
  close();
  activatePlay({ remount: verdict === OPEN_VERDICT.PROCEED });
}

function buildSkeleton() {
  const tab = document.createElement("wa-tab");
  tab.panel = TAB_NAME;
  tab.textContent = TAB_LABEL;
  const panel = document.createElement("wa-tab-panel");
  panel.name = TAB_NAME;
  panel.classList.add("settings-recents-panel");

  const host = document.createElement("div");
  host.className = "recents-host";
  const scroll = document.createElement("div");
  scroll.className = "recents-scroll";
  const tree = document.createElement("ul");
  tree.className = "recents-tree";
  tree.setAttribute("role", "tree");
  tree.setAttribute("aria-label", TAB_LABEL);
  const empty = document.createElement("p");
  empty.className = `recents-empty ${MUTED_CLASS}`;
  empty.textContent = EMPTY_TEXT;
  empty.hidden = true;
  scroll.append(tree, empty);
  host.append(scroll);
  panel.append(host);
  return { tab, panel, host, scroll, tree, empty };
}

export function buildRecentsTab({ api, select, close, signal }) {
  const { tab, panel, host, scroll, tree, empty } = buildSkeleton();

  const state = {
    roots: [],
    status: null,
    selected: select ?? null,
    // hash -> node over the whole forest, collapsed rows included.
    byHash: new Map(),
    // Everything starts collapsed; expanded rows are tracked by hash.
    expanded: new Set(),
    loadSeq: 0,
    busy: false,
  };
  // The deep-linked row: revealed (with its forks) and scrolled to once.
  let pendingReveal = state.selected !== null;

  const tagOf = (row) => currentEntryTag(row, {
    viewingHash: state.status?.view_hash ?? null,
    playingGameId: state.status?.play_game_id ?? null,
  });

  function setSelected(hash) {
    state.selected = hash;
    for (const li of tree.querySelectorAll(`.${ROW_CLASS}`)) {
      const on = li.dataset.hash === hash;
      li.classList.toggle(SELECTED_CLASS, on);
      li.setAttribute("aria-selected", String(on));
    }
  }

  // One action at a time: confirms and requests await before acting.
  async function runExclusive(fn) {
    if (state.busy) return;
    state.busy = true;
    try { await fn(); } finally { state.busy = false; }
  }

  function makeActions(node, tag) {
    const actions = document.createElement("span");
    actions.className = "recents-actions";
    if (tag) {
      // The current game's row: re-opening is a no-op and deleting it would
      // pull the board out from under the user, as in the import dropdown.
      const tagEl = document.createElement("span");
      tagEl.className = `recents-tag ${MUTED_CLASS}`;
      tagEl.textContent = tag;
      actions.append(tagEl);
      return actions;
    }
    const del = iconButton({
      icon: ICON_DELETE, title: DELETE_TITLE,
      onClick: () => runExclusive(() => deleteNode(api, node)),
    });
    if (subtreeHasCurrent(node, tagOf)) {
      del.disabled = true;
      del.title = DELETE_BLOCKED_TITLE;
    }
    actions.append(
      iconButton({ icon: ICON_OPEN, title: OPEN_TITLE, onClick: () => runExclusive(() => openNode(api, node, close)) }),
      del,
    );
    return actions;
  }

  function makeTwisty(node) {
    const twisty = document.createElement("span");
    twisty.className = "recents-twisty";
    if (!node.children.length) return twisty;
    const collapsed = !state.expanded.has(node.row.hash);
    twisty.append(iconButton({
      icon: collapsed ? ICON_COLLAPSED : ICON_EXPANDED,
      title: collapsed ? EXPAND_TITLE : COLLAPSE_TITLE,
      onClick: () => {
        if (collapsed) state.expanded.add(node.row.hash);
        else state.expanded.delete(node.row.hash);
        render();
      },
    }));
    return twisty;
  }

  function makeRow(node, depth) {
    const { row } = node;
    const li = document.createElement("li");
    li.className = ROW_CLASS;
    li.dataset.hash = row.hash;
    li.setAttribute("role", "treeitem");
    li.setAttribute("aria-level", String(depth + ARIA_LEVEL_BASE));
    if (node.children.length) {
      li.setAttribute("aria-expanded", String(state.expanded.has(row.hash)));
    }
    li.style.setProperty(DEPTH_VAR, String(depth));

    const label = document.createElement("span");
    label.className = "recents-label";
    const text = formatSummary(row.summary, { short: true }) || row.hash.slice(0, HASH_PREVIEW_LEN);
    label.textContent = text;
    label.title = text;
    const fork = document.createElement("span");
    fork.className = `recents-fork ${MUTED_CLASS}`;
    fork.textContent = depth > 0 ? forkLabel(row) : "";
    const date = document.createElement("span");
    date.className = `recents-date ${MUTED_CLASS}`;
    date.textContent = row.ts ? new Date(row.ts).toLocaleDateString(undefined, DATE_FORMAT) : "";

    li.append(makeTwisty(node), label, fork, date, makeActions(node, tagOf(row)));
    li.addEventListener("click", () => setSelected(row.hash));
    return li;
  }

  function render() {
    const items = [];
    const walk = (nodes, depth) => {
      for (const node of nodes) {
        items.push(makeRow(node, depth));
        if (state.expanded.has(node.row.hash)) walk(node.children, depth + 1);
      }
    };
    walk(state.roots, 0);
    tree.replaceChildren(...items);
    empty.hidden = items.length > 0;
    // A selection whose row is gone (deleted, evicted) clears silently; one
    // merely hidden under a collapsed parent stays.
    setSelected(state.byHash.has(state.selected) ? state.selected : null);
  }

  async function load() {
    const seq = ++state.loadSeq;
    let list, status;
    try {
      [list, status] = await Promise.all([
        api("GET", RECENT_IMPORTS_PATH),
        api("GET", GAME_STATUS_PATH),
      ]);
    } catch (e) {
      toast(`${LOAD_FAILED}: ${apiErrorDetail(e)}`, { variant: VARIANT_DANGER });
      return;
    }
    // A later load superseded this one.
    if (seq !== state.loadSeq) return;
    state.status = status;
    state.roots = buildForest(list.entries || []);
    state.byHash = new Map();
    const index = (nodes) => nodes.forEach((n) => { state.byHash.set(n.row.hash, n); index(n.children); });
    index(state.roots);
    // Expand the deep-linked row's ancestors and the row itself: the gear
    // that linked here exists to show its forks.
    const target = pendingReveal ? state.byHash.get(state.selected) : null;
    pendingReveal = false;
    for (let n = target; n; n = n.parent) state.expanded.add(n.row.hash);
    render();
    if (target) {
      tree.querySelector(`[data-hash="${target.row.hash}"]`)?.scrollIntoView({ block: "nearest" });
    }
  }

  // Sizer measures the dialog body, so mount only once the dialog shows.
  function mount() {
    const sizer = createWrapSizer({ wrapEl: scroll, anchorEl: host });
    sizer.sizeWrap();
    requestAnimationFrame(sizer.sizeWrap);
    signal.addEventListener("abort", sizer.teardown);
    window.addEventListener(APP_EVT.RECENTS_CHANGED, load, { signal });
    load();
  }

  return { tab, panel, mount };
}
