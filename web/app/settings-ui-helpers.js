// Small DOM helpers shared across settings tab builders.

export function makeDivider() {
  const hr = document.createElement("hr");
  hr.className = "settings-divider";
  return hr;
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
