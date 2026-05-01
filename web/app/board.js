import {
  Chessboard,
  COLOR,
  INPUT_EVENT_TYPE,
  FEN,
} from "../vendor/cm-chessboard/src/Chessboard.js";
import { MARKER_TYPE, Markers } from "../vendor/cm-chessboard/src/extensions/markers/Markers.js";
import { ARROW_TYPE, Arrows } from "../vendor/cm-chessboard/src/extensions/arrows/Arrows.js";
import { resolveBoardStyle } from "./board-styles.js";

export function mountBoard({ element, onMove, styleId }) {
  const s = resolveBoardStyle(styleId);
  const board = new Chessboard(element, {
    position: FEN.start,
    assetsUrl: "./vendor/cm-chessboard/assets/",
    style: { cssClass: s.cssClass, showCoordinates: true, pieces: { file: s.piecesFile } },
    extensions: [{ class: Markers }, { class: Arrows }],
  });

  let myColor = COLOR.white;
  let inputEnabled = false;

  function setSide(side) {
    const next = side === "black" ? COLOR.black : COLOR.white;
    if (next === myColor) return;
    myColor = next;
    board.setOrientation(myColor);
    if (inputEnabled) {
      board.disableMoveInput();
      inputEnabled = false;
      enableInput(true);
    }
  }

  function setPosition(fen, lastMoveUci) {
    board.setPosition(fen, true);
    board.removeMarkers();
    if (lastMoveUci && lastMoveUci.length >= 4) {
      const from = lastMoveUci.slice(0, 2);
      const to = lastMoveUci.slice(2, 4);
      board.addMarker(MARKER_TYPE.frame, from);
      board.addMarker(MARKER_TYPE.frame, to);
    }
  }

  function enableInput(yes) {
    if (yes === inputEnabled) return;
    inputEnabled = yes;
    if (yes) {
      board.enableMoveInput((event) => {
        if (event.type === INPUT_EVENT_TYPE.validateMoveInput) {
          const uci = event.squareFrom + event.squareTo + (event.promotion || "");
          // Optimistic acceptance — server validates and rebroadcasts position.
          onMove(uci);
          return true;
        }
        return true;
      }, myColor);
    } else {
      board.disableMoveInput();
    }
  }

  function setArrow(fromUci, toUci) {
    if (typeof board.removeArrows === "function") board.removeArrows();
    if (!fromUci || !toUci) return;
    if (typeof board.addArrow === "function") {
      board.addArrow(ARROW_TYPE.default, fromUci, toUci);
    }
  }

  function clearArrows() {
    if (typeof board.removeArrows === "function") board.removeArrows();
  }

  function forceResize() {
    // cm-chessboard has no public resize API; fall back to its private view.
    // Defensive: tolerate any future structural changes in the library.
    try {
      const v = board.view;
      if (v && typeof v.handleResize === "function") v.handleResize();
    } catch {
      // ignore — worst case the board stays at the previous size until the
      // next genuine container resize.
    }
  }

  return { setSide, setPosition, enableInput, forceResize, setArrow, clearArrows };
}
