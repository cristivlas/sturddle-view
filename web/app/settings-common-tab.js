// Common settings tab: global engine defaults (UCI Threads/Hash, Syzygy,
// opening book). The PGN directory lives on the Gameplay tab (HVE-only).
//
// pathRow is shared with the Gameplay and Tournament tabs, so it's passed in.

export function buildCommonTab({ initial, putSettings, putSettingsDebounced, pathRow }) {
  const generalTab = document.createElement("wa-tab");
  generalTab.panel = "general";
  generalTab.textContent = "Common";
  const generalPanel = document.createElement("wa-tab-panel");
  generalPanel.name = "general";

  function makeNumInput(labelText, key, opts = {}) {
    const { max } = opts;
    const item = document.createElement("div");
    const lbl = document.createElement("label");
    lbl.textContent = labelText;
    const input = document.createElement("wa-input");
    input.size = "small";
    input.type = "number";
    input.min = "1";
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

  // Threads subgroup: bordered block holding Analysis + Play threads
  // (same UCI knob, two contexts) so the relationship is obvious;
  // Hash sits alongside as a peer with a matching border so the two
  // visually pair without padding arithmetic.
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
      makeNumInput("Analysis", "engine_default_analysis_threads", { max: maxThreads }),
      makeNumInput("Play", "engine_default_threads", { max: maxThreads }),
    );
    threads.append(hdr, inner);
    // Hash: just the existing input, with a border to match the
    // threads box's frame.
    const hash = makeNumInput("Hash (MB)", "engine_default_hash_mb");
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
    plies.setAttribute("min", "1");
    plies.setAttribute("autocomplete", "off");
    plies.placeholder = "engine default";
    const curPlies = initial.engine_default_book_plies;
    if (curPlies != null) plies.value = String(curPlies);
    plies.addEventListener("input", () => {
      const raw = (plies.value || "").trim();
      if (raw === "") return putSettingsDebounced({ engine_default_book_plies: null });
      const n = Number(raw);
      if (Number.isFinite(n)) putSettingsDebounced({ engine_default_book_plies: n });
    });

    const order = document.createElement("wa-select");
    order.size = "small";
    order.setAttribute("distance", "4");
    order.value = initial.engine_default_book_order ?? "sequential";
    for (const [val, label] of [["sequential", "Sequential"], ["random", "Random"]]) {
      const opt = document.createElement("wa-option");
      opt.value = val;
      opt.textContent = label;
      order.append(opt);
    }
    order.addEventListener("change", () => {
      putSettings({ engine_default_book_order: order.value });
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

  const { row: bookOptionsRow, setEnabled: setBookOptionsEnabled } = bookPliesAndOrderRow();
  setBookOptionsEnabled(!!initial.engine_default_book_path);

  generalPanel.append(
    makeThreadsHashRow(initial.host?.logical_cores),
    pathRow(
      "SyzygyPath",
      initial.engine_default_syzygy_path || "",
      "directory",
      "Pick Syzygy tablebase directory",
      (p) => putSettings({ engine_default_syzygy_path: p }),
    ),
    pathRow(
      "Opening book",
      initial.engine_default_book_path || "",
      "file",
      "Pick opening book (.epd / .pgn)",
      (p) => {
        putSettings({ engine_default_book_path: p });
        setBookOptionsEnabled(!!p);
      },
    ),
    bookOptionsRow,
  );

  return { tab: generalTab, panel: generalPanel };
}
