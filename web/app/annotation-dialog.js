// Annotation editor modal for view/edit-mode commentary.
//
// UX flow: user is in edit mode, clicks the annotate ribbon button. Modal
// opens with the existing comment text preloaded (or empty if no comment).
// OK stages the text locally; Escape/dismiss discards. The OK does NOT
// round-trip to the server -- the staged text is shipped as part of
// /edit/commit's payload when the user exits edit mode via the confirm
// button.
//
// Resolves to:
//   { apply: false }              -- user dismissed (Escape / outside click)
//   { apply: true, text: string } -- user committed; text may be empty
//                                    (whitespace-only counts as delete)

import { showDialog } from "./dialogs.js";

const TITLE = "Edit annotation";
const PLACEHOLDER = "Add a comment for this position...";
const OK_LABEL = "OK";

export function editAnnotation({ currentText = "" } = {}) {
  return showDialog({
    label: TITLE,
    width: "min(540px, 92vw)",
    defaultValue: { apply: false },
    body: (resolve, dialog) => {
      const ta = document.createElement("wa-textarea");
      ta.rows = 6;
      ta.resize = "none";
      ta.placeholder = PLACEHOLDER;
      ta.value = currentText || "";

      const ok = document.createElement("wa-button");
      ok.slot = "footer";
      ok.variant = "brand";
      ok.textContent = OK_LABEL;
      ok.addEventListener("click", () => resolve({ apply: true, text: ta.value }));

      dialog.append(ta, ok);
      requestAnimationFrame(() => ta.focus?.());
    },
  });
}
