/**
 * Author and copyright: Stefan Haack (https://shaack.com)
 * Repository: https://github.com/shaack/cm-chessboard-position-editor
 * License: MIT, see file 'LICENSE'
 *
 * Local modifications (sturddle-view):
 *   - Added `enabled` prop to gate onSquareClick / moveInput when the
 *     extension is loaded but the app is not in edit mode.
 *   - Exposed `moveInputHandler` on the instance so the host can re-arm
 *     move input after a disableMoveInput() without reloading the ext
 *     (cm-chessboard has no removeExtension API).
 */
import {Extension} from "../../cm-chessboard/src/model/Extension.js"
import {
    PROMOTION_DIALOG_RESULT_TYPE,
    PromotionDialog
} from "../../cm-chessboard/src/extensions/promotion-dialog/PromotionDialog.js"
import {COLOR, INPUT_EVENT_TYPE} from "../../cm-chessboard/src/Chessboard.js"
import {MOVE_CANCELED_REASON} from "../../cm-chessboard/src/view/VisualMoveInput.js"
import {MARKER_TYPE} from "../../cm-chessboard/src/extensions/markers/Markers.js"
import {SelectPieceDialog} from "./extensions/SelectPieceDialog.js"

export const POSITION_CHANGE_TYPE = {
    move: "move",
    capture: "capture",
    castling: "castling",
    promotion: "promotion",
    createPiece: "createPiece",
    removePiece: "removePiece"
}

export class PositionEditor extends Extension {

    constructor(chessboard, props = {}) {
        super(chessboard)
        if (!chessboard.getExtension(PromotionDialog)) {
            chessboard.addExtension(PromotionDialog)
        }
        if (!chessboard.getExtension(SelectPieceDialog)) {
            chessboard.addExtension(SelectPieceDialog)
        }
        this.props = {
            autoSpecialMoves: true,
            onPositionChange: undefined,
            enabled: true,
            markers: {
                addPiece: {...MARKER_TYPE.frame}
            }
        }
        Object.assign(this.props, props)
        this.clickListener = this.onSquareClick.bind(this)
        this.state = {
            selectPieceDialogShown: false,
            promotionDialogShown: false
        }
        this.moveInputHandler = (event) => {
            if (!this.props.enabled) return false
            switch (event.type) {
                case INPUT_EVENT_TYPE.moveInputStarted:
                    return true
                case INPUT_EVENT_TYPE.moveInputCanceled:
                    return this.onMoveInputCanceled(event)
                case INPUT_EVENT_TYPE.validateMoveInput:
                    return this.onValidateMoveInput(event)
                case INPUT_EVENT_TYPE.moveInputFinished:
                    return this.onMoveInputFinished(event)
            }
        }
        this.chessboard.enableMoveInput(this.moveInputHandler)
        this.chessboard.context.addEventListener("click", this.clickListener)
    }

    onMoveInputCanceled(event) {
        if (event.reason === MOVE_CANCELED_REASON.movedOutOfBoard) {
            this.chessboard.setPiece(event.squareFrom, null)
            if (this.props.onPositionChange) {
                this.props.onPositionChange({
                    position: this.chessboard.getPosition(),
                    type: POSITION_CHANGE_TYPE.removePiece
                })
            }
        }
    }

    onValidateMoveInput(event) {
        if (this.chessboard.getPiece(event.squareTo)) {
            this.captured = this.chessboard.getPiece(event.squareTo)
        } else {
            this.captured = null
        }
        return true
    }

    onMoveInputFinished(event) {
        let castling, enPassant, promotion
        if (event.squareTo && this.props.autoSpecialMoves) {
            castling = this.handleCastling(event)
            if (!castling) {
                enPassant = this.handleEnPassant(event)
                if (!enPassant) {
                    promotion = this.handlePromotion(event)
                }
            }
        }
        if (this.props.onPositionChange) {
            let type = POSITION_CHANGE_TYPE.move
            if (castling) {
                type = POSITION_CHANGE_TYPE.castling
            } else if (enPassant || this.captured) {
                type = POSITION_CHANGE_TYPE.capture
            }
            if (!promotion && event.legalMove) {
                this.props.onPositionChange({position: this.chessboard.getPosition(), type: type})
            }
        }
    }

    handlePromotion(event) {
        const piece = this.chessboard.getPiece(event.squareTo)
        const color = piece[0]
        const type = piece[1]
        if (type === "p" &&
            (color === COLOR.white && event.squareTo[1] === "8" || color === COLOR.black && event.squareTo[1] === "1")) {
            this.state.promotionDialogShown = true
            this.chessboard.showPromotionDialog(event.squareTo, color, (result) => {
                if (result.type === PROMOTION_DIALOG_RESULT_TYPE.pieceSelected) {
                    this.chessboard.setPiece(event.squareFrom, null, false)
                    this.chessboard.setPiece(result.square, result.piece, true)
                    if (this.props.onPositionChange) {
                        this.props.onPositionChange({
                            position: this.chessboard.getPosition(),
                            type: POSITION_CHANGE_TYPE.promotion
                        })
                    }
                } else {
                    this.chessboard.movePiece(event.squareTo, event.squareFrom, true)
                }
                setTimeout(() => {
                    this.state.promotionDialogShown = false
                }, 500)
            })
            return true
        }
        return false
    }

    handleCastling(event) {
        const piece = this.chessboard.getPiece(event.squareTo)
        const color = piece[0]
        const type = piece[1]
        if (type === "k") {
            if (event.squareFrom[1] === "1" && event.squareTo[1] === "1" && color === COLOR.white) {
                if (event.squareFrom[0] === "e" && event.squareTo[0] === "g" && this.chessboard.getPiece("h1") === "wr") {
                    this.chessboard.movePiece("h1", "f1", true)
                    return true
                } else if (event.squareFrom[0] === "e" && event.squareTo[0] === "c" && this.chessboard.getPiece("a1") === "wr") {
                    this.chessboard.movePiece("a1", "d1", true)
                    return true
                }
            } else if (event.squareFrom[1] === "8" && event.squareTo[1] === "8" && color === COLOR.black) {
                if (event.squareFrom[0] === "e" && event.squareTo[0] === "g" && this.chessboard.getPiece("h8") === "br") {
                    this.chessboard.movePiece("h8", "f8", true)
                    return true
                } else if (event.squareFrom[0] === "e" && event.squareTo[0] === "c" && this.chessboard.getPiece("a8") === "br") {
                    this.chessboard.movePiece("a8", "d8", true)
                    return true
                }
            }
        }
        return false
    }

    handleEnPassant(event) {
        const piece = this.chessboard.getPiece(event.squareTo)
        const color = piece[0]
        const type = piece[1]
        if (type === "p") {
            const rankFrom = parseInt(event.squareFrom[1], 10)
            const rankTo = parseInt(event.squareTo[1], 10)
            const fileFrom = COLOR.black.charCodeAt(0) - event.squareFrom[0].charCodeAt(0) + 8
            const fileTo = COLOR.black.charCodeAt(0) - event.squareTo[0].charCodeAt(0) + 8
            if (color === COLOR.white && rankFrom === 5 && rankTo === 6 && Math.abs(fileFrom - fileTo) === 1 &&
                !this.captured && this.chessboard.getPiece(event.squareTo[0] + event.squareFrom[1]) === "bp") {
                this.chessboard.setPiece(event.squareTo[0] + event.squareFrom[1], null, true)
                return true
            }
            if (color === COLOR.black && rankFrom === 4 && rankTo === 3 && Math.abs(fileFrom - fileTo) === 1 &&
                !this.captured && this.chessboard.getPiece(event.squareTo[0] + event.squareFrom[1]) === "wp") {
                this.chessboard.setPiece(event.squareTo[0] + event.squareFrom[1], null, true)
                return true
            }
        }
        return false
    }

    onSquareClick(event) {
        if (!this.props.enabled || this.state.promotionDialogShown) {
            return
        }
        const square = event.target.getAttribute("data-square")
        if (square && !this.chessboard.getPiece(square)) {
            if (!this.state.selectPieceDialogShown) {
                this.chessboard.showSelectPieceDialog(square, (result) => {
                    if (result) {
                        this.chessboard.setPiece(square, result.piece, true)
                        if (this.props.onPositionChange) {
                            this.props.onPositionChange({
                                position: this.chessboard.getPosition(),
                                type: POSITION_CHANGE_TYPE.createPiece
                            })
                        }
                    }
                    setTimeout(() => {
                        this.state.selectPieceDialogShown = false
                        this.chessboard.removeMarkers(this.props.markers.addPiece, square)
                    })
                })
                this.chessboard.addMarker(this.props.markers.addPiece, square)
            }
            this.state.selectPieceDialogShown = true
        }
    }
}
