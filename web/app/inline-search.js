// Inline search bar for Engines, Openings and Arena (styles.css .inline-search).
// Esc (capture phase: a host dialog stays open) or a press outside the bar, the
// button and `inside` (the list: closing would re-render it under the click) closes it.

import { ESCAPE_KEY } from "./keys.js";

const OPEN_CLASS = "open";
// The lit ribbon button while the bar is open.
const ACTIVE_CLASS = "is-active";

// onInput(text) on every keystroke; onOpen() after the bar opens; onClose()
// after it closes with the input cleared (the caller drops its filter there).
// focusNextFrame defers the focus a frame, for a bar inside a dialog that's
// still settling. Returns { close, stopListening }: stopListening drops the
// document listeners when the host goes away without closing the bar.
export function wireInlineSearch({ btn, bar, input, inside = [], onInput, onOpen, onClose, focusNextFrame = false }) {
  input.addEventListener("input", () => onInput(input.value || ""));

  const isOpen = () => bar.classList.contains(OPEN_CLASS);

  function stopListening() {
    document.removeEventListener("pointerdown", onOutsideClick);
    document.removeEventListener("keydown", onSearchKey, true);
  }

  function close() {
    if (!isOpen()) return;
    bar.classList.remove(OPEN_CLASS);
    btn.classList.remove(ACTIVE_CLASS);
    input.value = "";
    stopListening();
    onClose?.();
  }

  function onOutsideClick(e) {
    if ([bar, btn, ...inside].some((el) => el.contains(e.target))) return;
    close();
  }

  function onSearchKey(e) {
    if (e.key !== ESCAPE_KEY) return;
    close();
    e.preventDefault();
    e.stopPropagation();
  }

  btn.addEventListener("click", () => {
    if (isOpen()) {
      close();
      return;
    }
    bar.classList.add(OPEN_CLASS);
    btn.classList.add(ACTIVE_CLASS);
    onOpen?.();
    if (focusNextFrame) requestAnimationFrame(() => input.focus());
    else input.focus();
    document.addEventListener("pointerdown", onOutsideClick);
    document.addEventListener("keydown", onSearchKey, true);
  });

  return { close, stopListening };
}
