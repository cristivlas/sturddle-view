// Observe perspective (stub): tournament observer / headless peek.
// Phase 1 will populate this with a WinBox-driven workspace.

export const observePerspective = {
  id: "observe",
  label: "Observe",

  async mount(root, _ctx) {
    root.innerHTML = `
      <section id="observe-perspective" class="placeholder">
        <p>Observe perspective is not implemented yet.</p>
        <p class="muted">
          This will become a workspace canvas for tournament games and
          headless-peek viewing.
        </p>
      </section>
    `;
    return { unmount() {} };
  },
};
