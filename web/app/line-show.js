// State machine that plays a PV line on the board, then hands it back.
// See docs/pv-play-spec.md. Owns two abort signals (generation, queue-epoch),
// the outside-state record, and the Esc/pointerdown/pointerup/pointercancel/
// blur listeners. Esc/pointerdown are installed only while a show is active;
// pointerup/pointercancel/blur must outlive a canceling pointerdown (they're
// what resolves its gesture guard), so they come down separately once idle.

const PV_PLAY_PLY_PAUSE_MS = 400;
const PV_PLAY_END_HOLD_MS = 1200;

function deferred() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

// Cancelable: a dropped pause must not leave its timer running to term for
// nothing once the race that awaited it has already moved on.
function delay(ms) {
  let handle;
  const promise = new Promise((r) => { handle = setTimeout(r, ms); });
  promise.cancel = () => clearTimeout(handle);
  return promise;
}

export function createLineShow(primitives) {
  let active = false;
  let generation = 0;
  let genDeferred = deferred();
  let epochDeferred = deferred();

  let frames = null;
  let pvUci = null;
  let onStepCb = null;
  let onEndCb = null;
  let isExemptCb = null;

  // Live from activation until applied. `arrows` maps ARROW_TYPE object ->
  // [{from,to}] snapshot (identity keys, per board.js's arrow-type singletons).
  let record = null;

  let pendingGesturePointerId = null;
  let pendingGestureDone = null;

  // Bumped by playLine whenever it claims the record (fresh or inherited).
  // A pending apply scheduled before the bump (an outside flight or a
  // cancel/return snap still in flight) captures the token at schedule time
  // and no-ops if it's stale by the time it would fire -- a retarget that
  // lands mid-flight must not let that older apply unlock input or drop
  // markers out from under the new show.
  let applyToken = 0;

  function bumpGeneration() {
    generation++;
    const old = genDeferred;
    genDeferred = deferred();
    old.resolve();
  }

  function bumpQueueEpoch() {
    const old = epochDeferred;
    epochDeferred = deferred();
    old.resolve();
  }

  function snapshotArrows() {
    const arrows = primitives.getArrows();
    const byType = new Map();
    for (const a of arrows) {
      if (!byType.has(a.type)) byType.set(a.type, []);
      byType.get(a.type).push({ from: a.from, to: a.to });
    }
    return byType;
  }

  function captureRecord() {
    record = {
      live: true,
      retired: false,
      placement: primitives.currentPlacement(),
      lastMoveUci: primitives.lastMoveUci(),
      input: primitives.isInputEnabledRaw(),
      arrows: snapshotArrows(),
    };
  }

  // Outside input/arrow requests during a live record update it in place, so
  // apply can never overwrite a later outside request.
  function onOutsideInput(enabled) {
    if (record && record.live) record.input = enabled;
  }

  function onOutsideArrows(type, pairs) {
    if (!record || !record.live) return;
    if (!pairs.length) record.arrows.delete(type);
    else record.arrows.set(type, pairs.slice());
  }

  function onOutsideArrowsClear() {
    if (record && record.live) record.arrows.clear();
  }

  function applyRecord(myToken) {
    if (myToken !== applyToken) return; // superseded by a later playLine
    if (!record || !record.live) return;
    record.live = false;
    if (primitives.isEditing()) return;
    if (!record.retired) {
      primitives.markMove(record.lastMoveUci);
      for (const [type, pairs] of record.arrows) {
        for (const p of pairs) primitives.addArrow(type, p.from, p.to);
      }
    }
    primitives.enableInputRaw(record.input);
  }

  function waitGestureThenApply(flight) {
    const myToken = applyToken;
    Promise.resolve(flight).then(() => {
      if (myToken !== applyToken) return; // superseded by a later playLine
      if (!pendingGestureDone) { applyRecord(myToken); return; }
      pendingGestureDone.promise.then(() => applyRecord(myToken));
    });
  }

  function armGestureGuard(pointerId) {
    pendingGesturePointerId = pointerId;
    pendingGestureDone = deferred();
  }

  function resolveGestureGuard(pointerId) {
    if (pendingGesturePointerId !== pointerId && pointerId !== undefined) return;
    pendingGesturePointerId = null;
    const g = pendingGestureDone;
    pendingGestureDone = null;
    g?.resolve();
    maybeRemoveGestureListeners();
  }

  // -- listeners (installed only while a show is active) --------------------

  function onKeydown(e) {
    if (e.key !== "Escape" || !active) return;
    e.stopPropagation();
    cancelLine();
  }

  function onPointerDownCapture(e) {
    if (isExemptCb && isExemptCb(e.target)) return;
    armGestureGuard(e.pointerId);
    cancelLine();
  }

  function onPointerUp(e) { resolveGestureGuard(e.pointerId); }
  function onPointerCancel(e) { resolveGestureGuard(e.pointerId); }
  function onBlur() { resolveGestureGuard(undefined); }

  // Esc/pointerdown-capture are the show's triggers: live only while a show
  // is active. Gesture listeners (pointerup/pointercancel/blur) must outlive
  // that -- they're what resolves the guard a canceling pointerdown armed --
  // so they come down separately, only once no show is active AND no gesture
  // is still pending (see maybeRemoveGestureListeners).
  function installListeners() {
    document.addEventListener("keydown", onKeydown, true);
    document.addEventListener("pointerdown", onPointerDownCapture, true);
    document.addEventListener("pointerup", onPointerUp, true);
    document.addEventListener("pointercancel", onPointerCancel, true);
    window.addEventListener("blur", onBlur);
  }

  function removeTriggerListeners() {
    document.removeEventListener("keydown", onKeydown, true);
    document.removeEventListener("pointerdown", onPointerDownCapture, true);
  }

  function removeGestureListeners() {
    document.removeEventListener("pointerup", onPointerUp, true);
    document.removeEventListener("pointercancel", onPointerCancel, true);
    window.removeEventListener("blur", onBlur);
  }

  function maybeRemoveGestureListeners() {
    if (!active && !pendingGestureDone) removeGestureListeners();
  }

  // -- show loop --------------------------------------------------------------

  // Races a queue entry's flight against both abort signals. Returns "done"
  // when the flight settled, "reset" when a queue-epoch bump won (re-issue),
  // or "cancelled" when the generation moved on (caller must bail).
  async function raceEntry(flight) {
    const genP = genDeferred.promise;
    const epochP = epochDeferred.promise;
    return Promise.race([
      Promise.resolve(flight).then(() => "done"),
      genP.then(() => "cancelled"),
      epochP.then(() => "reset"),
    ]);
  }

  async function stepPlacement(placement, myGen, animate = true) {
    for (;;) {
      const outcome = await raceEntry(primitives.setPositionAnimated(placement, animate));
      if (generation !== myGen) return false;
      if (outcome === "reset") continue; // same-FEN no-op if the reset already landed it
      return true;
    }
  }

  async function pause(ms, myGen) {
    const d = delay(ms);
    await Promise.race([d, genDeferred.promise]);
    d.cancel();
    return generation === myGen;
  }

  async function runLoop(myGen) {
    // Outside's last-move markers belong to the real board, not the PV --
    // drop them up front, whether or not frame 0 needs its own animation.
    primitives.markMove(null);
    if (frames[0] !== primitives.currentPlacement()) {
      if (!(await stepPlacement(frames[0], myGen))) return;
    }
    for (let i = 1; i < frames.length; i++) {
      const uci = pvUci[i - 1];
      onStepCb?.(i - 1);
      if (!(await stepPlacement(frames[i], myGen))) return;
      primitives.markMove(uci);
      const isLast = i === frames.length - 1;
      if (!(await pause(isLast ? PV_PLAY_END_HOLD_MS : PV_PLAY_PLY_PAUSE_MS, myGen))) return;
    }
    await doReturn(myGen);
  }

  async function doReturn(myGen) {
    const myToken = applyToken;
    if (!(await stepPlacement(record.placement, myGen))) return;
    endShow();
    applyRecord(myToken);
  }

  function endShow() {
    active = false;
    removeTriggerListeners();
    maybeRemoveGestureListeners();
    onEndCb?.();
  }

  // -- public API ---------------------------------------------------------

  // Returns false when refused (editing, or no playable frames) so a caller
  // that already applied its own "now playing" visuals (a row highlight, say)
  // can undo them -- nothing here will ever call opts.onEnd in that case.
  function playLine(nextFrames, opts = {}) {
    if (primitives.isEditing()) return false;
    if (!nextFrames || nextFrames.length < 1) return false;
    const wasActive = active;
    const prevOnEnd = onEndCb;
    bumpGeneration();
    // Claiming the record (fresh or inherited) invalidates any apply already
    // scheduled for it -- an outside flight or a cancel/return snap still in
    // flight from before this call must not land once this show is running.
    applyToken++;
    // A live record survives a cancel that hasn't applied yet (its snap or
    // apply bails on the generation bump above) -- a retarget must inherit
    // that same record, not recapture the show's own artificial state
    // (locked input, hidden arrows) as if it were the true outside state.
    const freshRecord = !isRecordLive();
    const wasRetired = !freshRecord && record.retired;
    if (freshRecord) {
      captureRecord();
    } else if (wasRetired) {
      // Arrow redirection stops once a record is retired (the outside caller
      // owns them from its notification on), so record.arrows is stale from
      // before that -- re-snapshot the board's current arrows before this
      // show clears and re-hides them, or it would draw that stale set back
      // instead of (or on top of) whatever the outside caller actually drew.
      record.arrows = snapshotArrows();
      record.retired = false;
    }
    // Idempotent: harmless to call again on an inherited-record retarget
    // where a click-cancel already tore the trigger listeners down.
    installListeners();
    if (freshRecord || wasRetired) primitives.clearArrows();
    active = true;
    frames = nextFrames;
    pvUci = opts.pvUci || [];
    onStepCb = opts.onStep || null;
    onEndCb = opts.onEnd || null;
    isExemptCb = opts.isExempt || null;
    primitives.enableInputRaw(false);
    if (wasActive) prevOnEnd?.();
    const myGen = generation;
    runLoop(myGen);
    return true;
  }

  function cancelLine() {
    if (!active) return;
    bumpGeneration();
    active = false;
    removeTriggerListeners();
    maybeRemoveGestureListeners();
    onEndCb?.();
    doCancelSnap();
  }

  // The cancel snap goes through the queue like any other entry: a reset
  // mid-flight either already landed it (force-completed, same-FEN re-issue)
  // or dropped it (queued behind a foreign entry), in which case it's
  // re-issued on the fresh queue -- same pattern as stepPlacement.
  async function doCancelSnap() {
    const myGen = generation;
    for (;;) {
      const outcome = await raceEntry(primitives.setPositionAnimated(record.placement, false));
      if (generation !== myGen) return; // superseded by a retarget/destroy
      if (outcome === "reset") continue;
      break;
    }
    waitGestureThenApply(Promise.resolve());
  }

  function onOutsidePosition(placement, lastMoveUci) {
    if (!active && !(record && record.live)) return;
    // Bump unconditionally: this also aborts a cancel-snap or return still in
    // flight (they bail out on the next generation check), so only the
    // outside caller's own flight ever paints the new position.
    bumpGeneration();
    if (active) {
      active = false;
      removeTriggerListeners();
      maybeRemoveGestureListeners();
      onEndCb?.();
    }
    if (record) {
      record.retired = true;
      record.placement = placement;
      record.lastMoveUci = lastMoveUci;
    }
  }

  function onOutsideFlight(promise) {
    if (!record || !record.live) return;
    waitGestureThenApply(promise);
  }

  function onOutsideQueueReset() {
    bumpQueueEpoch();
  }

  function destroy() {
    bumpGeneration();
    applyToken++;
    active = false;
    removeTriggerListeners();
    removeGestureListeners();
    resolveGestureGuard(pendingGesturePointerId);
    record = null;
    onEndCb?.();
    onEndCb = null;
  }

  function isRecordLive() { return !!(record && record.live); }
  function getRecordedInput() { return record && record.live ? record.input : null; }
  // Input stays redirected to the record until applied, retired or not (the
  // real lock only lifts at apply time). Arrows differ: once an outside
  // position set retires the record, that caller owns arrows from its
  // notification on (board_update clears them, applyEngineInfo sets its own),
  // so they must reach the board directly, not be swallowed into a record
  // whose arrows will never be drawn again.
  function shouldRedirectArrows() { return !!(record && record.live && !record.retired); }

  return {
    playLine, cancelLine,
    onOutsidePosition, onOutsideFlight, onOutsideQueueReset,
    onOutsideInput, onOutsideArrows, onOutsideArrowsClear,
    isRecordLive, getRecordedInput, shouldRedirectArrows,
    destroy,
  };
}

export { PV_PLAY_PLY_PAUSE_MS, PV_PLAY_END_HOLD_MS };
