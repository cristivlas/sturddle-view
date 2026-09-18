// Pure: walk a PV of UCI moves forward from a starting placement, producing
// one placement per frame (frame 0 = the searched position itself). Uses the
// vendored cm-chessboard Position class, which has zero chess semantics
// (dumb square-to-square move only) -- promotion, castling-rook-hop and
// en-passant are handled here.

import { Position } from "../vendor/cm-chessboard/src/model/Position.js";

const SQUARE_RE = /^[a-h][1-8]$/;
const CASTLE_ROOK_HOP = {
  e1g1: { from: "h1", to: "f1" },
  e1c1: { from: "a1", to: "d1" },
  e8g8: { from: "h8", to: "f8" },
  e8c8: { from: "a8", to: "d8" },
};

// A ply is malformed if the token is `0000`, the wrong length, either square
// fails basic shape, the from-square is empty, or the mover's color doesn't
// alternate with the previous ply. Any of these truncates the walk.
function applyPly(position, uci, expectedColor) {
  if (!uci || uci === "0000" || (uci.length !== 4 && uci.length !== 5)) return null;
  const from = uci.slice(0, 2);
  const to = uci.slice(2, 4);
  if (!SQUARE_RE.test(from) || !SQUARE_RE.test(to)) return null;
  const piece = position.getPiece(from);
  if (!piece) return null;
  const color = piece.charAt(0);
  if (expectedColor && color !== expectedColor) return null;

  const isPawn = piece.charAt(1) === "p";
  const isEnPassant = isPawn && from.charAt(0) !== to.charAt(0) && !position.getPiece(to);
  if (isEnPassant) {
    const capturedSquare = to.charAt(0) + from.charAt(1);
    position.setPiece(capturedSquare, null);
  }

  position.movePiece(from, to);

  if (uci.length === 5) {
    const promo = uci.charAt(4).toLowerCase();
    position.setPiece(to, color + promo);
  }

  const hop = CASTLE_ROOK_HOP[uci];
  if (hop && piece.charAt(1) === "k") {
    position.movePiece(hop.from, hop.to);
  }

  return color === "w" ? "b" : "w";
}

// pvFrames(placement, pvUci) -> placements[]. No placement (board source not
// wired yet, or the view unmounted) yields no frames. Truncates at the first
// malformed ply -- the line is shown as far as it makes sense, never as ghosts.
export function pvFrames(placement, pvUci) {
  if (!placement || !Array.isArray(pvUci) || pvUci.length === 0) return [];
  const position = new Position(placement);
  const frames = [position.getFen()];
  let expectedColor = null;
  for (const uci of pvUci) {
    const next = applyPly(position, uci, expectedColor);
    if (next === null) break;
    expectedColor = next;
    frames.push(position.getFen());
  }
  return frames;
}
