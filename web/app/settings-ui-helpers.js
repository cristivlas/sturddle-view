// Small DOM helpers shared across settings tab builders.

export function makeDivider() {
  const hr = document.createElement("hr");
  hr.className = "settings-divider";
  return hr;
}

// Settings toggles are uniform: small, label as light-DOM text, optional
// hover title, change handler fed the new value.
export function makeSettingSwitch({ label, title = "", checked, onChange }) {
  const sw = document.createElement("wa-switch");
  sw.size = "small";
  sw.checked = checked;
  sw.textContent = label;
  if (title) sw.title = title;
  sw.addEventListener("change", () => onChange(sw.checked));
  return sw;
}

export function makeSection(iconEl, ariaLabel, ...children) {
  const fs = document.createElement("fieldset");
  fs.className = "settings-section";
  const lg = document.createElement("legend");
  lg.setAttribute("aria-label", ariaLabel);
  lg.append(iconEl);
  fs.append(lg, ...children);
  return fs;
}
