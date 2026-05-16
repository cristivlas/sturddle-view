/**
 * Author and copyright: Stefan Haack (https://shaack.com)
 * Repository: https://github.com/shaack/cm-chessboard-position-editor
 * License: MIT, see file 'LICENSE'
 */
import {Extension, EXTENSION_POINT} from "../../../cm-chessboard/src/model/Extension.js"
import {Svg} from "../../../cm-chessboard/src/lib/Svg.js"
import {Utils} from "../../../cm-chessboard/src/lib/Utils.js"
import {COLOR, PIECE} from "../../../cm-chessboard/src/Chessboard.js"

const DISPLAY_STATE = {
    hidden: "hidden",
    displayRequested: "displayRequested",
    shown: "shown"
}

export class SelectPieceDialog extends Extension {

    constructor(chessboard) {
        super(chessboard)
        this.registerExtensionPoint(EXTENSION_POINT.afterRedrawBoard, this.extensionPointRedrawBoard.bind(this))
        chessboard.showSelectPieceDialog = this.showSelectPieceDialog.bind(this)
        chessboard.cancelSelectPieceDialog = this.cancelSelectPieceDialog.bind(this)
        chessboard.isSelectPieceDialogShown = this.isSelectPieceDialogShown.bind(this)
        this.selectPieceDialogGroup = Svg.addElement(chessboard.view.interactiveTopLayer, "g", {class: "select-piece-dialog-group"})
        this.state = {
            displayState: DISPLAY_STATE.hidden,
            callback: null,
            dialogParams: {
                square: null,
                color: null
            }
        }
    }

    showSelectPieceDialog(square, callback) {
        this.state.dialogParams.square = square
        this.state.callback = callback
        this.setDisplayState(DISPLAY_STATE.displayRequested)
        setTimeout(() => {
                this.chessboard.view.positionsAnimationTask.then(() => {
                    this.setDisplayState(DISPLAY_STATE.shown)
                })
            }
        )
    }

    cancelSelectPieceDialog() {
        this.state.callback(null)
        this.setDisplayState(DISPLAY_STATE.hidden)
    }

    isSelectPieceDialogShown() {
        return this.state.displayState === DISPLAY_STATE.shown ||
            this.state.displayState === DISPLAY_STATE.displayRequested
    }

    extensionPointRedrawBoard() {
        this.redrawDialog()
    }

    drawPieceButton(piece, point) {
        const squareWidth = this.chessboard.view.squareWidth
        const squareHeight = this.chessboard.view.squareHeight
        Svg.addElement(this.selectPieceDialogGroup,
            "rect", {
                x: point.x, y: point.y, width: squareWidth, height: squareHeight,
                class: "select-piece-dialog-button",
                "data-piece": piece
            })
        this.chessboard.view.drawPiece(this.selectPieceDialogGroup, piece, point)
    }

    redrawDialog() {
        while (this.selectPieceDialogGroup.firstChild) {
            this.selectPieceDialogGroup.removeChild(this.selectPieceDialogGroup.firstChild)
        }
        if (this.state.displayState === DISPLAY_STATE.shown) {
            const turned = this.chessboard.getOrientation() === COLOR.black
            const squareWidth = this.chessboard.view.squareWidth
            const squareHeight = this.chessboard.view.squareHeight
            const squareCenterPoint = this.chessboard.view.squareToPoint(this.state.dialogParams.square)
            squareCenterPoint.x = squareCenterPoint.x + squareWidth / 2
            squareCenterPoint.y = squareCenterPoint.y + squareHeight / 2
            const rank = parseInt(this.state.dialogParams.square.charAt(1), 10)
            let offsetX = 0
            if (squareCenterPoint.x + squareWidth * 2 > this.chessboard.view.width) {
                offsetX = -squareWidth * 2
            }
            let offsetY = 0
            if (!turned && rank < 7) {
                offsetY = -squareHeight * (7 - rank)
            } else if (turned && rank > 2) {
                offsetY = squareHeight * (2 - rank)
            }
            Svg.addElement(this.selectPieceDialogGroup,
                "rect", {
                    x: squareCenterPoint.x + offsetX,
                    y: squareCenterPoint.y + offsetY,
                    width: squareWidth * 2,
                    height: squareHeight * 6,
                    class: "select-piece-dialog"
                })
            const drawPiece = (piece, x, y) => {
                this.drawPieceButton(piece, {
                    x: squareCenterPoint.x + offsetX + squareWidth * x,
                    y: squareCenterPoint.y + offsetY + squareHeight * y
                })
            }
            drawPiece(PIECE["wp"], 0, 0)
            drawPiece(PIECE["wn"], 0, 1)
            drawPiece(PIECE["wb"], 0, 2)
            drawPiece(PIECE["wr"], 0, 3)
            drawPiece(PIECE["wq"], 0, 4)
            drawPiece(PIECE["wk"], 0, 5)
            drawPiece(PIECE["bp"], 1, 0)
            drawPiece(PIECE["bn"], 1, 1)
            drawPiece(PIECE["bb"], 1, 2)
            drawPiece(PIECE["br"], 1, 3)
            drawPiece(PIECE["bq"], 1, 4)
            drawPiece(PIECE["bk"], 1, 5)
        }
    }

    onClickPiece(event) {
        if (event.button !== 2) {
            if (event.target.dataset.piece) {
                this.state.callback({square: this.state.dialogParams.square, piece: event.target.dataset.piece})
                this.setDisplayState(DISPLAY_STATE.hidden)
            } else {
                this.onCancel(event)
            }
        }
    }

    onCancel(event) {
        if (this.state.displayState === DISPLAY_STATE.shown) {
            event.preventDefault()
            this.cancelSelectPieceDialog()
        }
    }

    contextMenu(event) {
        event.preventDefault()
        this.setDisplayState(DISPLAY_STATE.hidden)
        this.state.callback(null)
    }

    setDisplayState(displayState) {
        this.state.displayState = displayState
        if (displayState === DISPLAY_STATE.shown) {
            this.clickDelegate = Utils.delegate(this.chessboard.view.svg,
                "click",
                "*",
                this.onClickPiece.bind(this))
            this.contextMenuListener = this.contextMenu.bind(this)
            this.chessboard.view.svg.addEventListener("contextmenu", this.contextMenuListener)
        } else if (displayState === DISPLAY_STATE.hidden) {
            this.clickDelegate.remove()
            this.chessboard.view.svg.removeEventListener("contextmenu", this.contextMenuListener)
        }
        this.redrawDialog()
    }
}
