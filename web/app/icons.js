// Custom inline SVG icons not covered by the Font Awesome Free set used
// elsewhere. Values are the inner <svg> markup only -- callers wrap via
// inlineSvgIcon() from dialogs.js.

const SVG_URL = new URL("../chess-clock.svg", import.meta.url).href;
const CHESS_CLOCK_VIEW_BOX = "0 0 296 296";

async function fetchInnerSvg(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to load icon: ${url} (${res.status})`);
  const text = await res.text();
  const parser = new DOMParser();
  const doc = parser.parseFromString(text, "image/svg+xml");
  const g = doc.querySelector("svg > g");
  return g ? g.outerHTML : "";
}

export const CHESS_CLOCK_SVG_INNER = await fetchInnerSvg(SVG_URL);
export { CHESS_CLOCK_VIEW_BOX };
