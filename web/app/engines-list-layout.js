// Pure-DOM layout helpers for the Settings > Engines list. No list/CRUD
// state -- only column resize (the list sizer lives in list-wrap-sizer.js).
// Split out of mountEngineList so the controller core stays under the cap.

import { attachColumnResize, makePctApplySizes } from "./col-resize.js";

const DEFAULT_PCTS = [20, 14, 66];
const MIN_COL_PCT = 8;

export function attachEngineColResize(container, colPctsKey) {
  const headTableEl = container.querySelector(".engines-head-table");
  const bodyTableEl = container.querySelector(".engines-body-table");
  const headColEls = Array.from(headTableEl.querySelectorAll("col"));
  const bodyColEls = Array.from(bodyTableEl.querySelectorAll("col"));
  const wrapEl = container.querySelector(".engines-table-wrap");
  const grips = Array.from(headTableEl.querySelectorAll(".th-grip"));
  const colPcts = DEFAULT_PCTS.slice();

  attachColumnResize({
    table: headTableEl,
    grips,
    overlayHost: wrapEl,
    storageKey: colPctsKey,
    sizes: colPcts,
    unit: "pct",
    applySizes: makePctApplySizes([headColEls, bodyColEls], MIN_COL_PCT),
  });
}
