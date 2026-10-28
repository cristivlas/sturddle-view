// Arrows extension stamping a numbered dark-circle badge on arrows whose type
// has a `label` (MultiPV line numbers), drawn after all arrows so none covers
// one. Uses Arrows internals: re-verify on a cm-chessboard upgrade (board.js).

import { Arrows } from "../vendor/cm-chessboard/src/extensions/arrows/Arrows.js";
import { Svg } from "../vendor/cm-chessboard/src/lib/Svg.js";

const BADGE_CLASS = "arrow-badge";
// Badge radius and font size, as fractions of the square size.
const BADGE_RADIUS_FRAC = 0.17;
const BADGE_FONT_FRAC = 0.22;
// Badge center along the arrow: 0 = start square center, 1 = end.
const BADGE_POS_FRAC = 0.5;

export class NumberedArrows extends Arrows {
  onRedrawBoard() {
    super.onRedrawBoard();
    for (const arrow of this.arrows) {
      if (arrow.type.label != null) this.drawBadge(arrow);
    }
  }

  drawBadge(arrow) {
    const view = this.chessboard.view;
    const from = view.squareToPoint(arrow.from);
    const to = view.squareToPoint(arrow.to);
    const size = Math.min(view.squareWidth, view.squareHeight);
    const x = from.x + (to.x - from.x) * BADGE_POS_FRAC + view.squareWidth / 2;
    const y = from.y + (to.y - from.y) * BADGE_POS_FRAC + view.squareHeight / 2;
    const g = Svg.addElement(this.arrowGroup, "g", { class: BADGE_CLASS });
    Svg.addElement(g, "circle", { cx: x, cy: y, r: size * BADGE_RADIUS_FRAC });
    const text = Svg.addElement(g, "text", { x, y, "font-size": size * BADGE_FONT_FRAC });
    text.textContent = String(arrow.type.label);
  }
}
