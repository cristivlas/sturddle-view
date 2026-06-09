import {
  Chessboard,
  COLOR,
  INPUT_EVENT_TYPE,
  FEN,
} from "../vendor/cm-chessboard/src/Chessboard.js";
import { PositionAnimationsQueue } from "../vendor/cm-chessboard/src/view/PositionAnimationsQueue.js";
import { Svg } from "../vendor/cm-chessboard/src/lib/Svg.js";
import { MARKER_TYPE, Markers } from "../vendor/cm-chessboard/src/extensions/markers/Markers.js";
import { ARROW_TYPE, Arrows } from "../vendor/cm-chessboard/src/extensions/arrows/Arrows.js";
import {
  PROMOTION_DIALOG_RESULT_TYPE,
  PromotionDialog,
} from "../vendor/cm-chessboard/src/extensions/promotion-dialog/PromotionDialog.js";
import { PositionEditor } from "../vendor/cm-chessboard-position-editor/src/PositionEditor.js";
import { resolveBoardStyle } from "./board-styles.js";
import { SIDE } from "./chess-consts.js";

// Pinned cm-chessboard version this patch was verified against. On upgrade,
// re-verify the queue methods still match the expected shape (see
// _patchAnimationsQueue) before bumping.
const CM_CHESSBOARD_PINNED_VERSION = "8.12.7";
let _patchAssertedOnce = false;

// cm-chessboard's PositionAnimationsQueue schedules a requestAnimationFrame
// even when `animated=false`, then calls Svg.removeElement on `disappear`
// elements after one tick. If the host DOM is torn down (perspective unmount,
// WinBox hide) between the enqueue and the rAF, removeElement logs warnings
// for every parentless piece. Patch the two enqueue methods to snap
// synchronously via redrawPieces/redrawBoard when not animated -- no rAF, no
// post-teardown DOM walk.
function _patchAnimationsQueue(board) {
  const q = board.positionAnimationsQueue;
  if (!_patchAssertedOnce) {
    _patchAssertedOnce = true;
    const shapeOk =
      typeof q.enqueuePositionChange === "function" &&
      typeof q.enqueueTurnBoard === "function" &&
      typeof board.view?.redrawPieces === "function" &&
      typeof board.view?.redrawBoard === "function";
    if (!shapeOk) {
      console.warn(
        `[sturddle] cm-chessboard ${CM_CHESSBOARD_PINNED_VERSION} animation-queue patch: ` +
        `expected API shape not found. The snap-redraw monkey-patch in board.js ` +
        `may be stale -- re-verify against the current library version.`
      );
      return;
    }
  }
  q.enqueuePositionChange = function (positionFrom, positionTo, animated) {
    if (positionFrom.getFen() === positionTo.getFen()) {
      return this.enqueue(() => Promise.resolve());
    }
    if (!animated) {
      return this.enqueue(() => new Promise((resolve) => {
        if (this.chessboard.view) {
          this.chessboard.view.redrawPieces(positionTo.squares);
        }
        resolve();
      }));
    }
    return PositionAnimationsQueue.prototype.enqueuePositionChange.call(
      this, positionFrom, positionTo, animated,
    );
  };
  q.enqueueTurnBoard = function (position, color, animated) {
    if (!animated) {
      return this.enqueue(() => new Promise((resolve) => {
        if (this.chessboard.view) {
          this.chessboard.state.orientation = color;
          this.chessboard.view.redrawBoard();
          this.chessboard.view.redrawPieces(position.squares);
        }
        resolve();
      }));
    }
    return PositionAnimationsQueue.prototype.enqueueTurnBoard.call(
      this, position, color, animated,
    );
  };
}

// cm-chessboard's PositionsAnimation captures `disappear` element refs at
// animation start, then calls Svg.removeElement on them when the animation
// completes. A concurrent redraw (resize-driven redrawPieces, sprite swap,
// orientation flip) wipes the pieces layer mid-animation, leaving those
// refs detached. The vanilla removeElement warns "without parentNode" for
// every parentless ref. Silently skip -- the visual outcome is identical.
(function _silenceParentlessRemove() {
  const orig = Svg.removeElement;
  Svg.removeElement = function (element) {
    if (element && element.parentNode) orig.call(Svg, element);
  };
})();

const ASSETS_URL = "./vendor/cm-chessboard/assets/";

// Why: cm-chessboard's <use href="file.svg#wp"> path triggers a Chromium bug
// where the parsed-symbol shadow tree is reused across sprite files that share
// fragment IDs (every set defines wp/bk/..). Switching standard <-> staunty
// leaves stale glyphs until a hard reload. We inject each board's sprite as
// local <defs> on its own SVG and rewrite <use> to local-ref `#piece`.
const _spriteCache = new Map(); // piecesFile -> Promise<string defs HTML>

function _loadSpriteDefs(piecesFile) {
  let p = _spriteCache.get(piecesFile);
  if (!p) {
    p = fetch(ASSETS_URL + piecesFile)
      .then((r) => r.text())
      .then((text) => {
        const doc = new DOMParser().parseFromString(text, "image/svg+xml");
        // Sprite SVGs use top-level <g id="wp">, not <symbol>. Grab any
        // direct child of <svg> that has an id attribute.
        const root = doc.documentElement;
        const defs = Array.from(root.children).filter((el) => el.id);
        return defs.map((el) => el.outerHTML).join("");
      });
    _spriteCache.set(piecesFile, p);
  }
  return p;
}

async function _installLocalSprite(board, piecesFile) {
  const defsHtml = await _loadSpriteDefs(piecesFile);
  const svg = board.view?.svg;
  if (!svg || !svg.isConnected) return;
  const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
  defs.innerHTML = defsHtml;
  svg.insertBefore(defs, svg.firstChild);
  // The library already emits local "#piece" refs under assetsCache=true,
  // but if a future upgrade changes that, normalize stragglers here.
  for (const u of svg.querySelectorAll('use[href*=".svg#"]')) {
    const href = u.getAttribute("href");
    const hash = href.lastIndexOf("#");
    if (hash >= 0) u.setAttribute("href", href.slice(hash));
  }
  // Future drawPiece calls (animations, edit-mode adds) keep using local refs.
  board.view.getSpriteUrl = () => "";
}

export function mountBoard({ element, onMove, styleId }) {
  const s = resolveBoardStyle(styleId);
  const board = new Chessboard(element, {
    position: FEN.start,
    assetsUrl: ASSETS_URL,
    style: { cssClass: s.cssClass, showCoordinates: true, pieces: { file: s.piecesFile } },
    extensions: [{ class: Markers }, { class: Arrows }, { class: PromotionDialog }],
  });
  _installLocalSprite(board, s.piecesFile);
  _patchAnimationsQueue(board);

  let myColor = COLOR.white;
  let inputEnabled = false;

  function setSide(side) {
    const next = side === SIDE.BLACK ? COLOR.black : COLOR.white;
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

  // AI agent's declared move; uses ARROW_TYPE.secondary paired with a
  // local purple override in styles.css so it reads distinctly from
  // the navy default/PV arrow.
  function setRecommendArrow(fromUci, toUci) {
    if (typeof board.removeArrows === "function") {
      board.removeArrows(ARROW_TYPE.secondary);
    }
    if (!fromUci || !toUci) return;
    if (typeof board.addArrow === "function") {
      board.addArrow(ARROW_TYPE.secondary, fromUci, toUci);
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
    _patchAnimationsQueue(board);
    board.setPosition(board.getPosition(), false);
  }

  function destroy() {
    board.positionAnimationsQueue.destroy();
    board.destroy();
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

  function isInputEnabled() { return inputEnabled; }

  return {
    setSide, setPosition, enableInput, isInputEnabled, forceResize, cancelAnimations, destroy,
    setArrow, setOpponentArrow, setRecommendArrow, clearArrows,
    enterEditMode, exitEditMode, toggleCastlingRight, getCastlingRights, getPiecePlacement,
  };
}
