// Engines panel: list / add / remove / select.

export function mountEngines({ api, onError }) {
  const list = document.getElementById("engines-list");
  const form = document.getElementById("engine-add");
  const nameInput = document.getElementById("engine-name");
  const pathInput = document.getElementById("engine-path");

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
      li.textContent = "(none registered)";
      li.style.color = "var(--muted)";
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

      const selectBtn = document.createElement("button");
      selectBtn.textContent = e.id === selectedId ? "✓" : "use";
      selectBtn.disabled = e.id === selectedId;
      selectBtn.addEventListener("click", async () => {
        try {
          await api("POST", `/engines/${e.id}/select`);
          refresh();
        } catch (err) {
          onError?.(`select: ${err.message}`);
        }
      });

      const removeBtn = document.createElement("button");
      removeBtn.textContent = "×";
      removeBtn.title = "remove";
      removeBtn.addEventListener("click", async () => {
        if (!confirm(`Remove ${e.name}?`)) return;
        try {
          await api("DELETE", `/engines/${e.id}`);
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
    const name = nameInput.value.trim();
    const path = pathInput.value.trim();
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
