"""Tournament orchestrator — composes ``TournamentStore`` and a ``Runner``.

Owns:
- Single-active-tournament invariant (the store can't tell stale ``running``
  on disk from a real live process; the orchestrator can).
- Startup reconciliation: persisted ``running`` is reset to ``stopped`` on boot.
- Wiring runner events to the store and broadcast tap.
- Per-tournament proxy bookkeeping (engine names, subscribers, broadcast
  secret). Single-side observation only — pair detection deferred.

Web-agnostic: takes ids + a broadcast callback, so the CLI wrapper can reuse it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

import chess

from .rescheck import RescheckError, check_template
from .runner import RunSpec, Runner
from .uci_parse import parse_uci_line

if TYPE_CHECKING:
    from ..config import Settings
from .store import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_STOPPED,
    Tournament,
    TournamentStore,
)


log = logging.getLogger(__name__)


# Broadcast events the orchestrator emits upstream. Mirrors the runner's
# event vocabulary plus a ``status_change`` event the WebSocket layer
# uses to refresh the per-row status badge.
BroadcastCallback = Callable[[str, dict], Awaitable[None]]


# Per-tournament event history depth. Big enough to cover all the
# fastchess startup chatter (engine init, opening probes) plus a few
# completed games, so a workspace opened mid-tournament still gets
# a useful tail.
_EVENT_HISTORY_MAX = 200


# Two engines of one game share a FEN at the rendezvous: the thinker
# (registered via ``position``) and the waiter (registered via
# ``bestmove`` at the resulting FEN). ``info`` from the thinker fans
# out to the waiter's subscribers — that's the opponent's PV arrow.
_DEBUG_PAIRING   = os.environ.get("SV_DEBUG_PAIRING",   "0") == "1"


def _opposite_side(side: str) -> str:
    return "black" if side == "white" else "white"


# fastchess `-output format=fastchess` stdout lines. See spec §
# "Pair lifecycle: confirmation and dissolution" for the role
# these play in pair_id ↔ game-N stamping and dissolution.
_FASTCHESS_STARTED_RE = re.compile(
    r"Started game (\d+)(?: of \d+)? \((.+?) vs (.+?)\)"
)
_FASTCHESS_FINISHED_RE = re.compile(
    r"Finished game (\d+)(?: of \d+)? \((.+?) vs (.+?)\): "
    r"(1-0|0-1|1/2-1/2|\*)"
    r"(?:\s*\{([^}]*)\})?"
)

# Fallback result/termination when a pair is force-dissolved without
# a `Finished` line (proxy crash, runner shutdown mid-game).
_RESULT_UNKNOWN = "*"
_TERMINATION_UNKNOWN = "unknown"

# Upper bound on the bidirectional FIFO queues that match
# `Started N` ↔ pair confirmation. In steady state queues should hold
# only a few entries (≈ concurrency); growth past this means a leak —
# typically book-line collisions that prevented pair confirmation
# (Started enqueued, no confirmation arrives). Without a bound, the
# oldest leaked entry stays at the head and gets matched to *unrelated*
# subsequent confirmations, stamping pairs with progressively stale Ns
# and breaking dissolution. We evict the oldest with a warning.
_MATCH_QUEUE_MAX = 4096


def _fanout(subs: set[asyncio.Queue], payload: dict) -> None:
    """Slow-consumer policy: drop oldest, keep newest."""
    for q in list(subs):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass


def wrap_event_for_bus(kind: str, payload: dict) -> dict:
    """Map an orchestrator raw event to its EventBus / WS shape.

    Used by both the live broadcast path (in ``app.py``) and the
    history-replay REST endpoint so the two stay in lock-step. The
    workspace consumes both in a single shape: ``tournament_status``
    or ``tournament_update`` with ``payload.kind`` carrying the
    runner's original event kind.
    """
    if kind == "status_change":
        return {"kind": "tournament_status", "payload": payload}
    return {"kind": "tournament_update", "payload": {"kind": kind, **payload}}


class TournamentBusyError(Exception):
    """Raised when ``start`` is called while another tournament is running."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass
class OrchestratorConfig:
    """Composition wiring decided at construction. Kept tiny on purpose —
    the orchestrator is a thin coordinator, not a god object."""
    store: TournamentStore
    runner: Runner


class Orchestrator:
    """Single-active coordinator over a ``TournamentStore`` + a ``Runner``."""

    def __init__(self, store: TournamentStore, runner: Runner) -> None:
        self._store = store
        self._runner = runner
        self._active_id: str | None = None
        self._broadcast: BroadcastCallback | None = None

        # Slice 9b: live observation pipeline. WS subscribers attach
        # per-proxy and receive that engine's UCI line stream.
        self._proxy_subscribers: dict[str, set[asyncio.Queue]] = {}
        # Display name reported by each proxy on session start. Used to
        # label rows / buttons in the workspace UI. Cleared on session
        # end and on tournament teardown.
        self._proxy_engine_names: dict[str, str] = {}
        # Per-proxy snapshot of the most-recent stateful UCI lines:
        # ``position`` (current board), ``go`` (current clocks), and
        # ``info`` (current eval/depth/PV). Replayed to a subscriber on
        # connect so a window opened mid-game gets an instant snapshot
        # of the engine's state instead of waiting for the next event
        # (which under long time controls can be ≥10s away).
        self._proxy_snapshot: dict[str, dict[str, str]] = {}
        # Pairing detection. Map from FEN → list of ``(proxy_id, color)``
        # tuples. The thinker registers via ``position`` at F (its own
        # color); the waiter registers via ``bestmove`` at F-after-m
        # (still its own color — engine identity, not side-to-move).
        # Two engines of one game share a FEN with opposite colors.
        self._pairing_map: dict[str, list[tuple[str, str]]] = {}
        # ``(fen, my_color)``. my_color is the engine's color in the
        # current game (identity), not side-to-move at fen — that's
        # what makes the rendezvous lookup distinguish thinker from
        # waiter (same fen, opposite colors).
        self._pairing_state: dict[str, tuple[str, str] | None] = {}
        # Locked from side-to-move on the first ``position`` after
        # ``ucinewgame`` (that's the engine's first turn).
        self._pairing_color: dict[str, str | None] = {}
        # Derived from _pairing_map: one frozenset per FEN bucket with ≥2 entries.
        # Diffed on each update to detect new pairings and orphaned proxies.
        self._current_groups: set[frozenset] = set()
        # Game-level confirmed pairs: proxy_id → peer_proxy_id (bidirectional).
        # Added when a new unambiguous (size-2) group first appears; removed
        # when a proxy becomes orphaned (absent from _pairing_state).
        self._confirmed_pairs: dict[str, str] = {}
        # Stable game identity for each confirmed pair. frozenset(pid_a, pid_b) → uuid str.
        # A new UUID is minted at confirmation time so the client can key windows
        # to game identity rather than proxy identity (proxies can re-pair).
        self._pair_ids: dict[frozenset, str] = {}
        # Reverse: pair_id → frozenset(pid_a, pid_b). Used for snapshot replay.
        self._pair_proxies: dict[str, frozenset] = {}
        # fastchess game-N tracking. _started_games: FIFO of (N, white,
        # black) from `Started game N` stdout lines awaiting pair
        # confirmation. _unstamped_pairs: complementary FIFO of
        # (pair_id, white, black) for confirmed pairs awaiting their
        # `Started N`. Whichever signal arrives second consumes from
        # the other queue. Race rationale: under concurrency, UCI
        # rendezvous (HTTP) can land before the corresponding stdout
        # line is drained. _pair_game_n / _game_n_pair: bidirectional
        # map between pair_id and N, established when both signals
        # have matched.
        self._started_games: deque[tuple[int, str, str]] = deque()
        self._unstamped_pairs: deque[tuple[str, str, str]] = deque()
        self._pair_game_n: dict[str, int] = {}
        self._game_n_pair: dict[int, str] = {}
        # Pairs whose UCI side ended (ucinewgame / proxy_ended) but whose
        # `Finished` line hasn't arrived yet. Held here so dissolution
        # waits for the authoritative result; force-drained on shutdown.
        self._pending_dissolve: set[str] = set()
        # WS subscribers keyed by pair_id. Same lifecycle as proxy subscribers
        # but scoped to the game: sentinel sent when the pair is dissolved.
        self._game_subscribers: dict[str, set[asyncio.Queue]] = {}
        # Per-tournament secret embedded in the proxy --broadcast-url so
        # only proxies belonging to the active tournament can post.
        # Cleared on tournament termination.
        self._proxy_secret: str | None = None
        # Broadcast URL the proxy POSTs to. Set by the FastAPI app on
        # construction; ``None`` means "no proxy wrapping" (tests, CLI).
        self._proxy_broadcast_url: str | None = None
        # Optional reference to the live Settings instance. Read at
        # ``start()`` time so per-tournament runs see the current values
        # (user may have edited the Defaults tab between starts).
        self._settings: Settings | None = None
        # Per-tournament ring buffer of recent events. Replayed by the
        # REST endpoint to a workspace opened after restart so the user
        # sees the fastchess startup chatter instead of an empty log.
        # Capture happens in ``_emit`` regardless of subscribers.
        self._event_history: dict[str, deque[dict]] = {}
        # Monotonic per-orchestrator sequence stamped on every emitted
        # event. Lets the workspace dedupe between REST backfill and the
        # WS firehose when both deliver the same event around open time.
        self._event_seq: int = 0

    def _reset_pairing_state(self) -> None:
        """Drop all per-tournament pairing/observation state. Used on
        start rollback and on terminal runner events."""
        self._proxy_engine_names.clear()
        self._proxy_snapshot.clear()
        self._pairing_map.clear()
        self._pairing_state.clear()
        self._pairing_color.clear()
        self._current_groups = set()
        self._confirmed_pairs.clear()
        self._pair_ids.clear()
        self._pair_proxies.clear()
        self._started_games.clear()
        self._unstamped_pairs.clear()
        self._pair_game_n.clear()
        self._game_n_pair.clear()
        self._pending_dissolve.clear()
        self._game_subscribers.clear()

    def set_proxy_broadcast_url(self, url: str | None) -> None:
        """Configure the URL proxies POST to. The orchestrator passes
        this into the ``RunSpec`` when starting fastchess so each
        engine is wrapped in the proxy script."""
        self._proxy_broadcast_url = url

    def set_settings(self, settings: Settings | None) -> None:
        """Provide the live ``Settings`` instance. Used only as a fallback
        for tournaments created before ``engine_defaults`` was snapshotted
        into ``state.json``; new tournaments use their frozen snapshot."""
        self._settings = settings

    # ---- public surface ----------------------------------------------------

    def set_broadcast(self, broadcast: BroadcastCallback | None) -> None:
        """Install (or clear) the upstream broadcast callback. Used by the
        REST/WS layer in Slice 5; tests pass ``None`` and inspect the
        store directly."""
        self._broadcast = broadcast

    def active_id(self) -> str | None:
        """Id of the currently-running tournament, or ``None``."""
        return self._active_id

    async def start(self, tournament_id: str) -> Tournament:
        """Start the given tournament. Raises:

          - ``TournamentNotFoundError`` if the id does not exist.
          - ``TournamentBusyError`` if another tournament is running.
        """
        if self._active_id is not None or self._runner.is_running():
            raise TournamentBusyError(
                f"another tournament is running: {self._active_id!r}"
            )

        # Confirms existence and gets the frozen template.
        t = self._store.get(tournament_id)

        # Prefer the tournament's frozen snapshot; for tournaments created
        # before snapshotting was introduced, fall back to live Settings so
        # existing dirs still launch.
        s = self._settings
        ed = t.engine_defaults or {}
        def _ed(key: str):
            if key in ed:
                return ed[key]
            return getattr(s, f"engine_default_{key}", None)

        # Resource recheck — values were resolved + folded into the
        # template by the client at create time; we re-verify against
        # the current host (covers stale tournaments started after the
        # machine specs changed, and direct-API misuse).
        # Failure surfaces like a runner_crash: STATUS_FAILED + last_error.
        try:
            check_template(t.template)
        except RescheckError as e:
            last_error = {
                "rc": None,
                "stderr_tail": [str(e)],
                "rescheck": {"reason": e.reason, **e.details},
                "at": _now(),
            }
            failed = self._store.update_status(
                t.id, STATUS_FAILED, stopped_at=_now(), last_error=last_error,
            )
            await self._emit_status(failed)
            raise

        # Defense in depth: previous terminal event clears state; this
        # is a no-op in the normal stop -> start (resume) flow but
        # guards against any leak from prior runs.
        self._reset_pairing_state()
        # Mark active *before* spawning so a concurrent ``start`` call
        # racing against this one is rejected by the busy check above.
        self._active_id = t.id
        self._proxy_secret = secrets.token_urlsafe(24)
        spec = RunSpec(
            tournament=t,
            binary_path=getattr(self._runner, "binary_path", "") or "",
            work_dir=self._store._dir(t.id),
            pgn_path=self._store.pgn_path(t.id),
            config_path=self._store.config_path(t.id),
            log_path=self._store.logs_dir(t.id) / "fastchess.log",
            proxy_broadcast_url=self._proxy_broadcast_url,
            proxy_secret=self._proxy_secret,
            engine_default_threads=_ed("threads"),
            engine_default_hash_mb=_ed("hash_mb"),
            engine_default_syzygy_path=_ed("syzygy_path"),
            engine_default_book_path=_ed("book_path"),
            engine_default_book_plies=_ed("book_plies"),
            engine_default_book_order=_ed("book_order"),
        )
        try:
            # Clear any prior last_error on (re)start — the user has
            # acted on the diagnostic by retrying.
            updated = self._store.update_status(
                t.id, STATUS_RUNNING, started_at=_now(), last_error=None,
            )
            await self._emit_status(updated)
            await self._runner.start(spec, self._on_runner_event)
        except Exception:
            # Roll back the active claim so a failed start doesn't lock
            # out the next attempt.
            self._active_id = None
            self._proxy_secret = None
            self._reset_pairing_state()
            self._store.update_status(t.id, STATUS_STOPPED, stopped_at=_now())
            raise

        return updated

    async def stop(self, tournament_id: str) -> Tournament:
        """Stop the given tournament if it's the active one. No-op if
        it isn't. Idempotent."""
        if self._active_id == tournament_id and self._runner.is_running():
            await self._runner.stop()
        # If it isn't the active one, nothing to do here; the store
        # status is already terminal.
        return self._store.get(tournament_id)

    def reconcile_on_startup(self) -> list[Tournament]:
        """Mark persisted ``running`` rows as ``failed`` with a synthetic
        last_error — server died mid-tournament; can't claim a clean
        stop. Resume via Start (config.json still on disk)."""
        stale = self._store.find_by_status(STATUS_RUNNING)
        out: list[Tournament] = []
        for t in stale:
            last_error = {
                "rc": None,
                "stderr_tail": [
                    "Server was killed or crashed while this tournament was running. "
                    "fastchess and any engine processes have been reaped; press Start to resume."
                ],
                "at": _now(),
            }
            updated = self._store.update_status(
                t.id, STATUS_FAILED, stopped_at=_now(), last_error=last_error,
            )
            log.info(
                "reconcile: tournament %s was 'running' on disk; marking 'failed'",
                t.id,
            )
            out.append(updated)
        return out

    # ---- runner event sink -------------------------------------------------

    async def _on_runner_event(self, kind: str, payload: dict) -> None:
        """Receives events from the Runner.

        Maps:
          - ``done``         → status=done, active_id cleared
          - ``stopped``      → status=stopped, active_id cleared
          - ``runner_crash`` → status=failed + last_error persisted,
                               active_id cleared (no Resume in Phase 1)
          - ``started``      → no status change (we set RUNNING in start())
          - others           → forwarded as-is to broadcast
        """
        active_id = self._active_id

        if kind in ("done", "stopped", "runner_crash"):
            if active_id is not None:
                if kind == "done":
                    terminal_status = STATUS_DONE
                elif kind == "runner_crash":
                    terminal_status = STATUS_FAILED
                else:
                    terminal_status = STATUS_STOPPED
                last_error = None
                if kind == "runner_crash":
                    last_error = {
                        "rc": payload.get("rc"),
                        "stderr_tail": payload.get("stderr_tail", []),
                        "at": _now(),
                    }
                try:
                    updated = self._store.update_status(
                        active_id, terminal_status, stopped_at=_now(),
                        last_error=last_error,
                    )
                    await self._emit_status(updated)
                except Exception:
                    log.exception("failed to persist terminal status for %s", active_id)
                finally:
                    self._active_id = None
                    # Slice 9b: tear down the live-observation state so
                    # stale proxies (if any survive past fastchess exit)
                    # can't post and any open WS subscribers get a clean
                    # "ended" signal. Drain pending pairs first so their
                    # game subscribers see an `ended` frame.
                    self._proxy_secret = None
                    await self._force_dissolve_pending()
                    self._reset_pairing_state()
                    self._close_all_proxy_subscribers()

        if kind == "runner_log":
            await self._handle_runner_log(payload)

        # Always forward the runner event upstream — UI consumers want
        # ``runner_crash`` etc. distinct from a plain status change.
        await self._emit(kind, {"tournament_id": active_id, **payload})

    async def _emit_status(self, t: Tournament) -> None:
        await self._emit("status_change", {
            "tournament_id": t.id,
            "status": t.status,
            "started_at": t.started_at,
            "stopped_at": t.stopped_at,
            "last_error": t.last_error,
        })

    async def _emit(self, kind: str, payload: dict) -> None:
        # Stamp + record before broadcasting so the history captures
        # events even when no WS subscriber is attached at the moment
        # of emit (workspace opened late, restart race, etc.).
        # ``_seq`` lets the workspace dedupe between live + backfill;
        # ``_ts`` is the server-side emission time used for display.
        self._event_seq += 1
        payload = {**payload, "_seq": self._event_seq, "_ts": _now()}
        tid = payload.get("tournament_id")
        if tid:
            hist = self._event_history.setdefault(
                tid, deque(maxlen=_EVENT_HISTORY_MAX)
            )
            hist.append({"kind": kind, "payload": payload})
        if self._broadcast is None:
            return
        try:
            await self._broadcast(kind, payload)
        except Exception:
            log.exception("broadcast callback raised for %s", kind)

    def event_history(self, tournament_id: str) -> list[dict]:
        """Recent emitted events for a tournament, oldest first.

        Each item: ``{"kind": str, "payload": dict}`` where ``kind`` is
        the raw runner kind (``runner_log``, ``status_change``, …) and
        ``payload`` carries ``_seq`` + ``_ts`` stamps.
        """
        return list(self._event_history.get(tournament_id, []))

    def clear_event_history(self, tournament_id: str) -> None:
        """Drop the buffered history for a tournament. Called by the
        DELETE endpoint so torn-down tournaments don't leak history."""
        self._event_history.pop(tournament_id, None)

    # ---- Slice 9b: live-observation pipeline -------------------------------

    def proxy_secret(self) -> str | None:
        """Per-tournament secret embedded in the proxy broadcast URL.
        ``None`` when no tournament is running."""
        return self._proxy_secret

    def active_proxies(self) -> list[dict]:
        """Snapshot of currently-active proxies (engine processes wrapped
        by the broadcast tap). Used by the API ``GET /api/tournaments/{id}``
        so a workspace opening mid-tournament can seed its Schedule rows
        even if it missed the forward-going ``proxy_started`` events.

        Each entry: ``{"proxy_id": str, "engine_name": str | None}``.
        Sorted by ``proxy_id`` for stable ordering across calls."""
        return [
            {"proxy_id": pid, "engine_name": name}
            for pid, name in sorted(self._proxy_engine_names.items())
        ]

    def active_pairings(self) -> list[dict]:
        """Snapshot of currently-confirmed proxy pairs. Same shape as the
        ``proxy_paired`` WS event so the workspace can seed ``livePairings``
        on mount without having to have caught all prior WS events.

        Each entry: ``{proxy_a, engine_a, side_a, proxy_b, engine_b, side_b}``.
        Deduped (bidirectional map → one entry per pair)."""
        seen: set[frozenset] = set()
        out: list[dict] = []
        for pid_a, pid_b in self._confirmed_pairs.items():
            key = frozenset((pid_a, pid_b))
            if key in seen:
                continue
            seen.add(key)
            white_pid, black_pid = self._white_black_for_group(key)
            out.append({
                "pair_id":  self._pair_ids.get(key, ""),
                "proxy_a":  white_pid,
                "engine_a": self._proxy_engine_names.get(white_pid),
                "side_a":   "white",
                "proxy_b":  black_pid,
                "engine_b": self._proxy_engine_names.get(black_pid),
                "side_b":   "black",
            })
        return out

    def engine_name_for(self, proxy_id: str) -> str | None:
        """Display name reported by a proxy on its session start, or
        ``None`` if the proxy never announced (or has ended)."""
        return self._proxy_engine_names.get(proxy_id)

    def verify_proxy_secret(self, presented: str | None) -> bool:
        if self._proxy_secret is None or presented is None:
            return False
        # Constant-time comparison.
        import hmac
        return hmac.compare_digest(self._proxy_secret, presented)

    async def ingest_proxy_lines(
        self, proxy_id: str, lines: list[str]
    ) -> None:
        """Called by the ``/internal/proxy`` endpoint with a batch of
        UCI lines from one proxy. Updates the per-proxy snapshot
        (always — independent of subscribers) and fans out to any
        subscribed WS clients. Drives the pairing map so that ``info``
        lines from the thinking engine also fan out to the
        opposite-color subscribers (the live opponent's view)."""
        snap = self._proxy_snapshot.setdefault(proxy_id, {})
        own_subs = self._proxy_subscribers.get(proxy_id)
        for line in lines:
            stripped = line.lstrip()
            parsed: dict | None = None
            paired_subs: set[asyncio.Queue] = set()
            thinking_side: str | None = None

            if stripped.startswith("position "):
                snap["position"] = line
                snap.pop("info", None)
                parsed = parse_uci_line(stripped)
                if parsed is not None and "fen" in parsed and "side_to_move" in parsed:
                    if self._pairing_color.get(proxy_id) is None:
                        self._pairing_color[proxy_id] = parsed["side_to_move"]
                    color = self._pairing_color[proxy_id]
                    if color is not None:
                        new_pairs, orphaned = self._pairing_register(proxy_id, parsed["fen"], color)
                        await self._emit_group_events(new_pairs, orphaned)
            elif stripped.startswith("ucinewgame"):
                self._pairing_unregister(proxy_id)
                new_pairs, orphaned = self._recompute_groups()
                self._pairing_color[proxy_id] = None
                await self._emit_group_events(new_pairs, orphaned)
            elif stripped.startswith("go "):
                snap["go"] = line
            elif stripped.startswith("bestmove "):
                parsed = parse_uci_line(stripped)
                new_pairs, orphaned = self._pairing_apply_bestmove(proxy_id, parsed)
                await self._emit_group_events(new_pairs, orphaned)
            elif stripped.startswith("info "):
                if " score " in stripped or " pv " in stripped:
                    snap["info"] = line
                state = self._pairing_state.get(proxy_id)
                if state is not None:
                    fen, my_color = state
                    paired_subs = self._paired_subscribers(proxy_id, fen, my_color)
                    thinking_side = my_color

            if own_subs:
                payload: dict = {"proxy_id": proxy_id, "line": line}
                if parsed is not None:
                    payload["parsed"] = parsed
                _fanout(own_subs, payload)
            if paired_subs:
                payload = {
                    "proxy_id": proxy_id,
                    "line": line,
                    "paired": True,
                    "thinking_side": thinking_side,
                    "engine_name": self._proxy_engine_names.get(proxy_id),
                }
                _fanout(paired_subs, payload)
            # Fan out to game subscribers (keyed by pair_id, not proxy_id).
            peer = self._confirmed_pairs.get(proxy_id)
            if peer:
                pair_id = self._pair_ids.get(frozenset((proxy_id, peer)))
                if pair_id:
                    game_subs = self._game_subscribers.get(pair_id)
                    if game_subs:
                        state = self._pairing_state.get(proxy_id)
                        game_payload: dict = {
                            "proxy_id":     proxy_id,
                            "line":         line,
                            "thinking_side": state[1] if state else None,
                            "engine_name":  self._proxy_engine_names.get(proxy_id),
                        }
                        if parsed is not None:
                            game_payload["parsed"] = parsed
                        _fanout(game_subs, game_payload)

    def _recompute_groups(self) -> tuple[set[frozenset], set[str]]:
        """Derive the current set of pairing groups from ``_pairing_map``.

        A group is the frozenset of all proxy_ids sharing one FEN bucket
        (requires ≥ 2 entries). Updates ``_current_groups`` in place and
        returns ``(new_pairs, orphaned)``:

        - ``new_pairs``: unambiguous (size-2) groups that are newly confirmed
          this game — callers should emit ``proxy_paired`` for each.
        - ``orphaned``: proxies that were in a now-removed group but are no
          longer registered at *any* FEN (i.e. absent from ``_pairing_state``).
          Between-move transitions keep the proxy registered at a new FEN, so
          they produce no orphans and no ``proxy_unpaired`` event.
        """
        new_groups: set[frozenset] = set()
        for bucket in self._pairing_map.values():
            if len(bucket) >= 2:
                new_groups.add(frozenset(pid for pid, _ in bucket))

        added   = new_groups - self._current_groups
        removed = self._current_groups - new_groups
        self._current_groups = new_groups

        new_pairs: set[frozenset] = set()
        for group in added:
            if len(group) == 2:
                pid_a, pid_b = tuple(group)
                state_a = self._pairing_state.get(pid_a)
                state_b = self._pairing_state.get(pid_b)
                name_a = self._proxy_engine_names.get(pid_a)
                name_b = self._proxy_engine_names.get(pid_b)
                # Require opposite colors and neither proxy already confirmed.
                # Reject same-engine-name pairs as phantoms from book-line
                # collisions (4-bucket decay leaving two same-engine proxies
                # that aren't actually playing each other in fastchess).
                # TODO: lift this when self-play is supported — see spec
                # § "Self-play (deferred)". Self-play needs orchestrator-
                # level engine-name disambiguation before reaching fastchess.
                if (state_a and state_b and state_a[1] != state_b[1]
                        and pid_a not in self._confirmed_pairs
                        and pid_b not in self._confirmed_pairs
                        and name_a and name_b and name_a != name_b):
                    self._confirmed_pairs[pid_a] = pid_b
                    self._confirmed_pairs[pid_b] = pid_a
                    pair_id = str(uuid.uuid4())
                    self._pair_ids[group] = pair_id
                    self._pair_proxies[pair_id] = group
                    self._stamp_pair_game_n(pair_id, pid_a, pid_b, state_a[1])
                    new_pairs.add(group)
                    n = self._pair_game_n.get(pair_id)
                    if _DEBUG_PAIRING:
                        log.info(
                            "pair confirmed tag=%s game_n=%s white=%s(%s) black=%s(%s) "
                            "queues=started:%d/unstamped:%d/pairs:%d",
                            pair_id[:8], n,
                            self._proxy_engine_names.get(
                                pid_a if state_a[1] == "white" else pid_b, "?"),
                            (pid_a if state_a[1] == "white" else pid_b)[:8],
                            self._proxy_engine_names.get(
                                pid_b if state_a[1] == "white" else pid_a, "?"),
                            (pid_b if state_a[1] == "white" else pid_a)[:8],
                            len(self._started_games), len(self._unstamped_pairs),
                            len(self._pair_proxies),
                        )
                        if n is None:
                            log.info(
                                "pair confirmed tag=%s awaiting Started N; "
                                "_started_games head=%s",
                                pair_id[:8],
                                list(self._started_games)[:3],
                            )
                elif name_a and name_b and name_a == name_b and _DEBUG_PAIRING:
                    log.info(
                        "pair candidate rejected (same engine name): "
                        "%s(%s) vs %s(%s) -- phantom from book-line collision",
                        name_a, pid_a[:8], name_b, pid_b[:8],
                    )
            elif _DEBUG_PAIRING:
                fen = (self._pairing_state.get(next(iter(group))) or ("?",))[0]
                log.debug(
                    "pairing: ambiguous size=%d fen=%.30s %s",
                    len(group),
                    fen,
                    [(p[:8], self._proxy_engine_names.get(p, "?")) for p in group],
                )

        orphaned: set[str] = set()
        for group in removed:
            for pid in group:
                if pid not in self._pairing_state:
                    orphaned.add(pid)
        if orphaned and _DEBUG_PAIRING:
            log.info(
                "pairing: orphaned %s",
                [(p[:8], self._proxy_engine_names.get(p, "?")) for p in orphaned],
            )

        return new_pairs, orphaned

    def _pairing_register(self, proxy_id: str, fen: str, side: str) -> tuple[set[frozenset], set[str]]:
        """Re-register proxy at a new FEN, then recompute pairing groups.

        Returns ``(new_pairs, orphaned)`` from ``_recompute_groups``."""
        self._pairing_unregister(proxy_id)
        bucket = self._pairing_map.setdefault(fen, [])
        bucket.append((proxy_id, side))
        self._pairing_state[proxy_id] = (fen, side)
        if _DEBUG_PAIRING:
            self._pairing_assert_invariants()
            if len(bucket) > 2:
                log.warning(
                    "pairing: bucket >2 fen=%s entries=%s",
                    fen,
                    [(pid[:8], s) for pid, s in bucket],
                )
        return self._recompute_groups()

    def _pairing_unregister(self, proxy_id: str) -> None:
        prev = self._pairing_state.pop(proxy_id, None)
        if prev is None:
            return
        fen, _side = prev
        bucket = self._pairing_map.get(fen)
        if bucket is None:
            return
        bucket[:] = [(pid, s) for (pid, s) in bucket if pid != proxy_id]
        if not bucket:
            del self._pairing_map[fen]

    def _pairing_apply_bestmove(
        self, proxy_id: str, parsed: dict | None
    ) -> tuple[set[frozenset], set[str]]:
        """Re-register at post-move FEN (waiting for opponent)."""
        state = self._pairing_state.get(proxy_id)
        if state is None or not parsed:
            return set(), set()
        move_uci = parsed.get("move")
        if not move_uci or move_uci == "(none)":
            return set(), set()
        fen, my_color = state
        try:
            board = chess.Board(fen)
            board.push_uci(move_uci)
        except (ValueError, chess.IllegalMoveError, chess.InvalidMoveError):
            return set(), set()
        return self._pairing_register(proxy_id, board.fen(), my_color)

    async def _emit_group_events(
        self, new_pairs: set[frozenset], orphaned: set[str]
    ) -> None:
        """Emit ``proxy_paired`` for new pairs; mark orphans pending.

        Dissolution is deferred to the authoritative ``Finished game N``
        line — see spec §"Pair lifecycle"."""
        for group in new_pairs:
            white_pid, black_pid = self._white_black_for_group(group)
            await self._emit("proxy_paired", {
                "tournament_id": self._active_id,
                "pair_id": self._pair_ids.get(group, ""),
                # `_a` is always white; `_b` is always black. See
                # `_white_black_for_group` — frozenset iteration is
                # nondeterministic so we sort by side explicitly.
                "proxy_a": white_pid,
                "engine_a": self._proxy_engine_names.get(white_pid, white_pid),
                "side_a": "white",
                "proxy_b": black_pid,
                "engine_b": self._proxy_engine_names.get(black_pid, black_pid),
                "side_b": "black",
            })
        seen: set[str] = set()
        for pid in orphaned:
            if pid in seen:
                continue
            peer = self._confirmed_pairs.pop(pid, None)
            if peer is None:
                continue
            self._confirmed_pairs.pop(peer, None)
            seen.update((pid, peer))
            # Free the proxy↔proxy link so the surviving proxy can
            # re-pair on its next ucinewgame. _pair_ids / _pair_proxies
            # / _pair_game_n stay until `Finished N` (or force-dissolve)
            # actually cleans them up.
            pair_id = self._pair_ids.get(frozenset((pid, peer)), "")
            if pair_id:
                self._pending_dissolve.add(pair_id)
                if _DEBUG_PAIRING:
                    log.info(
                        "pair pending tag=%s game_n=%s pid=%s peer=%s",
                        pair_id[:8], self._pair_game_n.get(pair_id),
                        pid[:8], peer[:8],
                    )

    def _cap_match_queue(self, q: deque, name: str) -> None:
        """Bound the bidirectional FIFO queues. Past `_MATCH_QUEUE_MAX`
        the oldest entry is almost certainly leaked (book-line collision
        that never confirmed) and would cause stale-N stamping; drop it
        with a warning carrying the dropped value for diagnosis."""
        while len(q) > _MATCH_QUEUE_MAX:
            dropped = q.popleft()
            log.warning(
                "%s queue cap exceeded; dropped oldest=%s len=%d",
                name, dropped, len(q),
            )

    def _white_black_for_group(self, group: frozenset) -> tuple[str, str]:
        """Return (white_pid, black_pid) for a confirmed pair. Frozenset
        iteration order is nondeterministic; this resolves orientation
        from `_pairing_state[pid][1]` so payload `_a`/`_b` fields can
        consistently mean white/black."""
        pids = tuple(group)
        if len(pids) != 2:
            return pids[0] if pids else "", pids[1] if len(pids) > 1 else ""
        pid_a, pid_b = pids
        side_a = (self._pairing_state.get(pid_a) or ("", "?"))[1]
        return (pid_a, pid_b) if side_a == "white" else (pid_b, pid_a)

    def _stamp_pair_game_n(
        self, pair_id: str, pid_a: str, pid_b: str, side_a: str
    ) -> None:
        """Match a freshly-confirmed pair to the head of `_started_games`
        whose `(white, black)` engine names match (FIFO). If no Started
        line has arrived yet, queue the pair as unstamped — a later
        `_match_started_to_unstamped` call will stamp it."""
        white_pid, black_pid = (pid_a, pid_b) if side_a == "white" else (pid_b, pid_a)
        white = self._proxy_engine_names.get(white_pid)
        black = self._proxy_engine_names.get(black_pid)
        if not white or not black:
            return
        for i, (n, w, b) in enumerate(self._started_games):
            if w == white and b == black:
                del self._started_games[i]
                self._pair_game_n[pair_id] = n
                self._game_n_pair[n] = pair_id
                return
        self._unstamped_pairs.append((pair_id, white, black))
        self._cap_match_queue(self._unstamped_pairs, "unstamped_pairs")

    def _match_started_to_unstamped(self, n: int, white: str, black: str) -> str | None:
        """Inverse of `_stamp_pair_game_n`: a `Started N` line lands;
        try to bind it to an already-confirmed pair awaiting its N
        (FIFO by direction). Returns the pair_id stamped, or ``None``
        if no unstamped pair matches (caller should queue the entry)."""
        for i, (pair_id, w, b) in enumerate(self._unstamped_pairs):
            if w == white and b == black:
                del self._unstamped_pairs[i]
                self._pair_game_n[pair_id] = n
                self._game_n_pair[n] = pair_id
                return pair_id
        return None

    async def _dissolve_pair(
        self, pair_id: str, result: str, termination: str | None
    ) -> None:
        """Single dissolution path. Drops bookkeeping for the pair,
        sends the game-WS sentinel, and emits ``proxy_unpaired`` +
        ``game_finished``. Idempotent — a duplicate call is a no-op."""
        proxies = self._pair_proxies.pop(pair_id, None)
        if proxies is None:
            return
        white_pid, black_pid = self._white_black_for_group(proxies)
        self._confirmed_pairs.pop(white_pid, None)
        self._confirmed_pairs.pop(black_pid, None)
        self._pair_ids.pop(proxies, None)
        n = self._pair_game_n.pop(pair_id, None)
        if n is not None:
            self._game_n_pair.pop(n, None)
        # Drop from unstamped queue if still waiting for its Started.
        for i, (pid_, _, _) in enumerate(self._unstamped_pairs):
            if pid_ == pair_id:
                del self._unstamped_pairs[i]
                break
        self._pending_dissolve.discard(pair_id)
        game_subs = self._game_subscribers.pop(pair_id, None)
        if _DEBUG_PAIRING:
            log.info(
                "pair dissolved tag=%s game_n=%s result=%s termination=%s game_subs=%d",
                pair_id[:8], n, result, termination,
                len(game_subs) if game_subs else 0,
            )
        if game_subs:
            for q in game_subs:
                try:
                    q.put_nowait({
                        "proxy_id": white_pid,
                        "ended": True,
                        "result": result,
                        "termination": termination,
                    })
                except asyncio.QueueFull:
                    pass
        # TODO: `proxy_unpaired` is redundant with `game_finished` — same
        # trigger, overlapping payload. Kept for now as a debug signal
        # (client filters it out of the visible event log but retains it
        # in `_event_history`, useful for diagnosing pair-lifecycle
        # asymmetries). Consider dropping once the `Finished N` path is
        # battle-tested; would simplify both server emit and client
        # subscriber wiring.
        await self._emit("proxy_unpaired", {
            "tournament_id": self._active_id,
            "pair_id": pair_id,
            "proxy_id": white_pid,
            "peer_id": black_pid,
        })
        await self._emit("game_finished", {
            "tournament_id": self._active_id,
            "pair_id": pair_id,
            "game_n": n,
            # `_a` = white, `_b` = black. Mirrors proxy_paired contract.
            "proxy_a": white_pid,
            "proxy_b": black_pid,
            "engine_a": self._proxy_engine_names.get(white_pid),
            "engine_b": self._proxy_engine_names.get(black_pid),
            "result": result,
            "termination": termination,
        })

    async def _force_dissolve_pending(self) -> None:
        """Drain pairs whose `Finished` line never arrived (proxy crash,
        tournament killed mid-game). Called at terminal runner events."""
        # Snapshot keys so dissolution mutations don't disturb iteration.
        pending_ids = list(self._pair_ids.values())
        for pair_id in pending_ids:
            await self._dissolve_pair(
                pair_id, _RESULT_UNKNOWN, _TERMINATION_UNKNOWN,
            )

    def _on_runner_log_line(self, line: str) -> tuple[str, dict] | None:
        """Parse a fastchess stdout line for `Started game N` /
        `Finished game N`. Returns ``("started", ...)`` /
        ``("finished", ...)`` payload or ``None``."""
        m = _FASTCHESS_STARTED_RE.search(line)
        if m is not None:
            return ("started", {
                "n": int(m.group(1)),
                "white": m.group(2),
                "black": m.group(3),
            })
        m = _FASTCHESS_FINISHED_RE.search(line)
        if m is not None:
            return ("finished", {
                "n": int(m.group(1)),
                "white": m.group(2),
                "black": m.group(3),
                "result": m.group(4),
                "termination": (m.group(5) or "").strip() or None,
            })
        return None

    async def _handle_runner_log(self, payload: dict) -> None:
        """Inspect a `runner_log` event for game-N markers (stdout only)."""
        if payload.get("stream") != "out":
            return
        line = payload.get("line", "")
        parsed = self._on_runner_log_line(line)
        if parsed is None:
            return
        kind, info = parsed
        if kind == "started":
            n, white, black = info["n"], info["white"], info["black"]
            stamped = self._match_started_to_unstamped(n, white, black)
            if stamped is None:
                self._started_games.append((n, white, black))
                self._cap_match_queue(self._started_games, "started_games")
            elif _DEBUG_PAIRING:
                log.info(
                    "pair late-stamped tag=%s game_n=%d white=%s black=%s",
                    stamped[:8], n, white, black,
                )
            return
        # finished
        n = info["n"]
        pair_id = self._game_n_pair.get(n)
        if pair_id is None:
            log.warning(
                "fastchess Finished game %d (%s vs %s) had no confirmed pair; "
                "pending=%s known_game_ns=%s",
                n, info["white"], info["black"],
                [(p[:8], self._pair_game_n.get(p)) for p in self._pending_dissolve],
                sorted(self._game_n_pair.keys())[:8],
            )
            return
        await self._dissolve_pair(pair_id, info["result"], info["termination"])

    def _paired_subscribers(
        self, proxy_id: str, fen: str, side: str
    ) -> set[asyncio.Queue]:
        """WS queues of opposite-color proxies at ``fen`` (the waiter)."""
        bucket = self._pairing_map.get(fen)
        if not bucket:
            return set()
        target_side = _opposite_side(side)
        out: set[asyncio.Queue] = set()
        for pid, s in bucket:
            if pid == proxy_id or s != target_side:
                continue
            subs = self._proxy_subscribers.get(pid)
            if subs:
                out.update(subs)
        return out

    def _pairing_assert_invariants(self) -> None:
        """SV_DEBUG_PAIRING-gated. Only proxy-uniqueness is invariant:
        with concurrency > 1 fastchess runs multiple game-pairs and
        two same-color proxies from different pairs can legitimately
        share a FEN. Logs + continues rather than crashing ingest."""
        seen: set[str] = set()
        for _fen, bucket in self._pairing_map.items():
            for pid, _color in bucket:
                if pid in seen:
                    log.error("pairing invariant: proxy %s registered at multiple FENs; map=%s", pid, self._pairing_map)
                    return
                seen.add(pid)

    async def proxy_session_started(self, proxy_id: str, engine_name: str) -> None:
        """Called when a proxy reports it has started up. Records the
        engine's display name and broadcasts a ``proxy_started`` event
        so the workspace's Schedule can add a row for it."""
        self._proxy_engine_names[proxy_id] = engine_name
        log.debug("proxy session started: %s (%s)", proxy_id, engine_name)
        await self._emit("proxy_started", {
            "tournament_id": self._active_id,
            "proxy_id": proxy_id,
            "engine_name": engine_name,
        })

    async def proxy_session_ended(self, proxy_id: str) -> None:
        """Called when a proxy session has exited (clean or otherwise).
        Closes any subscribers and drops bookkeeping for this proxy."""
        self._proxy_engine_names.pop(proxy_id, None)
        self._proxy_snapshot.pop(proxy_id, None)
        self._pairing_unregister(proxy_id)
        new_pairs, orphaned = self._recompute_groups()
        self._pairing_color.pop(proxy_id, None)
        subs = self._proxy_subscribers.pop(proxy_id, None)
        if subs:
            for q in subs:
                # Sentinel: signal end of stream to subscribers.
                try:
                    q.put_nowait({"proxy_id": proxy_id, "ended": True})
                except asyncio.QueueFull:
                    pass
        await self._emit("proxy_ended", {
            "tournament_id": self._active_id,
            "proxy_id": proxy_id,
        })
        await self._emit_group_events(new_pairs, orphaned)

    def subscribe_to_proxy(self, proxy_id: str) -> asyncio.Queue:
        """WS handler calls this; returns a bounded queue that receives
        per-line dicts ``{proxy_id, line}`` and a final
        ``{proxy_id, ended: True}`` when the proxy session ends.

        Replays the proxy's current snapshot (latest ``position`` /
        ``go`` / ``info``) onto the queue so a window opened mid-game
        gets an instant render of the engine's state instead of having
        to wait for the engine's next event (≥10s under long TC)."""
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._proxy_subscribers.setdefault(proxy_id, set()).add(q)
        snap = self._proxy_snapshot.get(proxy_id)
        if snap:
            for kind in ("position", "go", "info"):
                line = snap.get(kind)
                if line is None:
                    continue
                try:
                    q.put_nowait({"proxy_id": proxy_id, "line": line})
                except asyncio.QueueFull:
                    pass
        return q

    def unsubscribe_from_proxy(self, proxy_id: str, queue: asyncio.Queue) -> None:
        subs = self._proxy_subscribers.get(proxy_id)
        if subs is not None:
            subs.discard(queue)
            if not subs:
                self._proxy_subscribers.pop(proxy_id, None)

    def subscribe_to_game(self, pair_id: str) -> asyncio.Queue:
        """WS handler calls this for game-scoped subscriptions.

        The queue receives the same ``{proxy_id, line}`` payloads as the
        proxy subscriber but is closed (via ``{ended: True}`` sentinel)
        when the pair dissolves, not when the proxy session ends."""
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._game_subscribers.setdefault(pair_id, set()).add(q)
        # Replay snapshot for both proxies so a late subscriber gets
        # instant board state without waiting for the next UCI event.
        proxies = self._pair_proxies.get(pair_id, frozenset())
        for pid in proxies:
            snap = self._proxy_snapshot.get(pid)
            if snap:
                for kind in ("position", "go", "info"):
                    raw = snap.get(kind)
                    if raw:
                        try:
                            q.put_nowait({"proxy_id": pid, "line": raw,
                                          "parsed": parse_uci_line(raw)})
                        except asyncio.QueueFull:
                            pass
        return q

    def unsubscribe_from_game(self, pair_id: str, queue: asyncio.Queue) -> None:
        subs = self._game_subscribers.get(pair_id)
        if subs is not None:
            subs.discard(queue)
            if not subs:
                self._game_subscribers.pop(pair_id, None)

    def _close_all_proxy_subscribers(self) -> None:
        """End-of-tournament cleanup — wake all subscribers with the
        ended sentinel and drop the subscription table."""
        for proxy_id, subs in list(self._proxy_subscribers.items()):
            for q in subs:
                try:
                    q.put_nowait({"proxy_id": proxy_id, "ended": True})
                except asyncio.QueueFull:
                    pass
        self._proxy_subscribers.clear()
