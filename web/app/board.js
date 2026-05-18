import {
  Chessboard,
  COLOR,
  INPUT_EVENT_TYPE,
  FEN,
} from "../vendor/cm-chessboard/src/Chessboard.js";
import { PositionAnimationsQueue } from "../vendor/cm-chessboard/src/view/PositionAnimationsQueue.js";
import { MARKER_TYPE, Markers } from "../vendor/cm-chessboard/src/extensions/markers/Markers.js";
import { ARROW_TYPE, Arrows } from "../vendor/cm-chessboard/src/extensions/arrows/Arrows.js";
import {
  PROMOTION_DIALOG_RESULT_TYPE,
  PromotionDialog,
} from "../vendor/cm-chessboard/src/extensions/promotion-dialog/PromotionDialog.js";
import { PositionEditor } from "../vendor/cm-chessboard-position-editor/src/PositionEditor.js";
import { resolveBoardStyle } from "./board-styles.js";

export function mountBoard({ element, onMove, styleId }) {
  const s = resolveBoardStyle(styleId);
  const board = new Chessboard(element, {
    position: FEN.start,
    assetsUrl: "./vendor/cm-chessboard/assets/",
    style: { cssClass: s.cssClass, showCoordinates: true, pieces: { file: s.piecesFile } },
    extensions: [{ class: Markers }, { class: Arrows }, { class: PromotionDialog }],
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

  function setPosition(fen, lastMoveUci, animate = true) {
    board.setPosition(fen, animate);
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
      // Disarm any handler the PositionEditor may have left active so the
      // library's internal guard doesn't throw "moveInput already enabled".
      board.disableMoveInput();
      board.enableMoveInput((event) => {
        if (event.type === INPUT_EVENT_TYPE.validateMoveInput) {
          const piece = event.piece || "";
          const toRank = event.squareTo.charAt(1);
          const isPromotion =
            piece.charAt(1) === "p" &&
            ((piece.charAt(0) === "w" && toRank === "8") ||
             (piece.charAt(0) === "b" && toRank === "1"));
          if (isPromotion) {
            // cm-chessboard's PromotionDialog uses a "*" event delegate that
            // fires the callback once per ancestor of the click target, so
            // guard against duplicate sends.
            let resolved = false;
            board.showPromotionDialog(event.squareTo, piece.charAt(0), (result) => {
              if (resolved) return;
              resolved = true;
              if (result && result.type === PROMOTION_DIALOG_RESULT_TYPE.pieceSelected) {
                const promo = result.piece.charAt(1);
                onMove(event.squareFrom + event.squareTo + promo);
              }
            });
            // Don't optimistically accept — server broadcasts the final position.
            // Returning true would leave the pawn on the last rank under the dialog,
            // and the trailing mousedown re-hit-tests onto it, re-arming move input.
            return false;
          }
          const uci = event.squareFrom + event.squareTo;
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

  // Two arrow types share one visual style (default) but differ by class
  // tag so each can be replaced independently — own thinker vs opponent
  // (paired). Same color: arrows already differ by origin square; a
  // distinct color (esp. danger/red) would falsely read as an error.
  function setArrow(fromUci, toUci) {
    if (typeof board.removeArrows === "function") {
      board.removeArrows(ARROW_TYPE.default);
    }
    if (!fromUci || !toUci) return;
    if (typeof board.addArrow === "function") {
      board.addArrow(ARROW_TYPE.default, fromUci, toUci);
    }
  }

  function setOpponentArrow(fromUci, toUci) {
    if (typeof board.removeArrows === "function") {
      board.removeArrows(ARROW_TYPE.success);
    }
    if (!fromUci || !toUci) return;
    if (typeof board.addArrow === "function") {
      board.addArrow(ARROW_TYPE.success, fromUci, toUci);
    }
  }

  function clearArrows() {
    if (typeof board.removeArrows === "function") board.removeArrows();
  }

  // Castling-corner squares (rook start squares) for highlight markers.
  const CASTLING_SQUARES = { wK: "h1", wQ: "a1", bK: "h8", bQ: "a8" };

  let editMode = false;
  let castlingRights = { wK: false, wQ: false, bK: false, bQ: false };
  let editPositionChangeCb = null;
  let positionEditorLoaded = false;

  function _applyCastlingMarkers() {
    board.removeMarkers(MARKER_TYPE.dot);
    for (const [right, square] of Object.entries(CASTLING_SQUARES)) {
      if (castlingRights[right]) {
        board.addMarker(MARKER_TYPE.dot, square);
      }
    }
  }

  function enterEditMode(onPositionChange, seed) {
    if (editMode) return;
    editMode = true;
    editPositionChangeCb = onPositionChange ?? null;
    if (seed && seed.castling) {
      castlingRights = { ...seed.castling };
    }
    board.disableMoveInput();
    board.removeMarkers();
    if (!positionEditorLoaded) {
      positionEditorLoaded = true;
      // addExtension calls PositionEditor constructor which calls enableMoveInput.
      board.addExtension(PositionEditor, {
        autoSpecialMoves: false,
        onPositionChange: () => {
          if (!editMode) return;
          _applyCastlingMarkers();
          editPositionChangeCb?.();
        },
      });
    } else {
      const ext = board.getExtension(PositionEditor);
      ext.props.enabled = true;
      board.enableMoveInput(ext.moveInputHandler);
    }
    _applyCastlingMarkers();
  }

  function exitEditMode() {
    if (!editMode) return;
    editMode = false;
    editPositionChangeCb = null;
    castlingRights = { wK: false, wQ: false, bK: false, bQ: false };
    board.removeMarkers(MARKER_TYPE.dot);
    board.disableMoveInput();
    if (positionEditorLoaded) {
      board.getExtension(PositionEditor).props.enabled = false;
    }
  }

  function toggleCastlingRight(right) {
    if (!editMode) return;
    castlingRights[right] = !castlingRights[right];
    _applyCastlingMarkers();
  }

  function getCastlingRights() {
    return { ...castlingRights };
  }

  // cm-chessboard's getPosition() returns ONLY the piece-placement field
  // ("rnbqkbnr/..."). The full 6-field FEN (with STM, castling, etc.) lives
  // in the server payload upstream.
  function getPiecePlacement() {
    return board.getPosition();
  }

  function cancelAnimations() {
    board.positionAnimationsQueue.destroy();
    board.positionAnimationsQueue = new PositionAnimationsQueue(board);
    board.setPosition(board.getPosition(), false);
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

  return {
    setSide, setPosition, enableInput, forceResize, cancelAnimations,
    setArrow, setOpponentArrow, clearArrows,
    enterEditMode, exitEditMode, toggleCastlingRight, getCastlingRights, getPiecePlacement,
  };
}
