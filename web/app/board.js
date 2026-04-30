import {
  Chessboard,
  COLOR,
  INPUT_EVENT_TYPE,
  FEN,
} from "../vendor/cm-chessboard/src/Chessboard.js";
import { MARKER_TYPE, Markers } from "../vendor/cm-chessboard/src/extensions/markers/Markers.js";

export function mountBoard({ element, onMove }) {
  const board = new Chessboard(element, {
    position: FEN.start,
    assetsUrl: "./vendor/cm-chessboard/assets/",
    style: { cssClass: "default", showCoordinates: true, pieces: { file: "pieces/standard.svg" } },
    extensions: [{ class: Markers }],
  });

  let myColor = COLOR.white;
  let inputEnabled = false;

  function setSide(side) {
    const next = side === "black" ? COLOR.black : COLOR.white;
    if (next === myColor) return;
    myColor = next;
    board.setOrientation(myColor);
    // Re-bind move input so cm-chessboard accepts moves for the new color.
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

  return { setSide, setPosition, enableInput };
}
