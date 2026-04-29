// Engines panel: list / add / remove / select.
// Mounts into a container element, builds its own DOM. Caller is responsible
// for placement (a sidebar, a settings tab, etc).

import { confirm, pickFile, toast } from "./dialogs.js";

export function mountEngines({ container, api, onError }) {
  container.innerHTML = `
    <ul class="engines-list"></ul>
    <form class="engine-add">
      <wa-input class="engine-name" size="small" placeholder="Engine name"></wa-input>
      <wa-input class="engine-path" size="small" placeholder="/path/to/engine"></wa-input>
      <wa-button type="button" class="engine-browse" size="small" variant="neutral">Browse…</wa-button>
      <wa-button type="submit" size="small" variant="brand">Add</wa-button>
    </form>
  `;

  const list = container.querySelector(".engines-list");
  const form = container.querySelector(".engine-add");
  const nameInput = container.querySelector(".engine-name");
  const pathInput = container.querySelector(".engine-path");
  const browseBtn = container.querySelector(".engine-browse");

  browseBtn.addEventListener("click", async () => {
    const picked = await pickFile({
      api,
      title: "Pick engine binary",
      mode: "executable",
      startPath: (pathInput.value || "").trim() || null,
    });
    if (picked) {
      pathInput.value = picked;
      if (!(nameInput.value || "").trim()) {
        // Default the name to the binary's filename if empty.
        nameInput.value = picked.split(/[\\/]/).pop();
      }
    }
  });

  async function refresh() {
    try {
      const { engines, selected_id } = await api("GET", "/engines");
      render(engines, selected_id);
    } catch (e) {
      onError?.(`engines: ${e.message}`);
    }
  }

  function render(engines, selectedId) {
    list.innerHTML = "";
    if (engines.length === 0) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = "(none registered)";
      list.appendChild(li);
      return;
    }
    for (const e of engines) {
      const li = document.createElement("li");
      if (e.id === selectedId) li.classList.add("selected");

      const name = document.createElement("span");
      name.className = "name";
      name.textContent = e.name;

      const path = document.createElement("span");
      path.className = "path";
      path.title = e.path;
      path.textContent = e.path;

      const selectBtn = document.createElement("wa-button");
      selectBtn.size = "small";
      selectBtn.variant = e.id === selectedId ? "success" : "neutral";
      selectBtn.appearance = "outlined";
      selectBtn.textContent = e.id === selectedId ? "Active" : "Use";
      selectBtn.disabled = e.id === selectedId;
      selectBtn.addEventListener("click", async () => {
        try {
          await api("POST", `/engines/${e.id}/select`);
          refresh();
        } catch (err) {
          onError?.(`select: ${err.message}`);
        }
      });

      const removeBtn = document.createElement("wa-button");
      removeBtn.size = "small";
      removeBtn.variant = "danger";
      removeBtn.appearance = "outlined";
      removeBtn.textContent = "Remove";
      removeBtn.addEventListener("click", async () => {
        const ok = await confirm({
          title: "Remove engine",
          message: `Remove ${e.name}?`,
          okLabel: "Remove",
          destructive: true,
        });
        if (!ok) return;
        try {
          await api("DELETE", `/engines/${e.id}`);
          toast(`Removed ${e.name}`, { variant: "success" });
          refresh();
        } catch (err) {
          onError?.(`remove: ${err.message}`);
        }
      });

      li.append(name, path, selectBtn, removeBtn);
      list.appendChild(li);
    }
  }

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const name = (nameInput.value || "").trim();
    const path = (pathInput.value || "").trim();
    if (!name || !path) return;
    try {
      await api("POST", "/engines", { name, path });
      nameInput.value = "";
      pathInput.value = "";
      refresh();
    } catch (e) {
      onError?.(`add: ${e.message}`);
    }
  });

  refresh();
  return { refresh };
}
