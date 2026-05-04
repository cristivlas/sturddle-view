export function flashWindow(wb) {
  wb.addClass("wb-attention");
  wb.g.addEventListener("animationend", () => wb.removeClass("wb-attention"), { once: true });
}

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
