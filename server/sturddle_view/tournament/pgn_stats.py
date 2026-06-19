"""PGN-based standings, Elo, and SPRT computation.

``games.pgn`` is the source of truth (not the runner's stdout summary).
Stop wipes the PGN on next Start, so every tournament run computes
standings over its own monotone PGN -- no cross-run reconciliation.
Reads are cached per-path keyed by (mtime, size).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import chess
import chess.pgn

from ..play.canonical_hash import canonical_hash_from_game
from ..chess.pgn_walk import walk_mainline
from ..chess.results import (
    BLACK_WIN as _BLACK_WIN,
    DECISIVE_RESULTS,
    WHITE_WIN as _WHITE_WIN,
)
from ..openings import OpeningBook

log = logging.getLogger(__name__)

_DECISIVE_RESULTS = DECISIVE_RESULTS

# PGN tag line: [Name "value"]. Non-greedy value match — we don't honor
# \"-escapes; the four headers we read never contain quotes in fastchess output.
_TAG_RE = re.compile(r'\[(\w+)\s+"(.*?)"\]\s*$')
_TAG_RE_BLOCK = re.compile(r'^\[(\w+)\s+"(.*?)"\]', re.MULTILINE)

# PGN header tag names.
_TAG_WHITE = "White"
_TAG_BLACK = "Black"
_TAG_RESULT = "Result"
_TAG_ROUND = "Round"
_TAG_TERMINATION = "Termination"

# Tags we actually use; ignore the rest to skip a dict write per line.
_WANTED_TAGS = frozenset({_TAG_WHITE, _TAG_BLACK, _TAG_RESULT, _TAG_ROUND})

# Placeholder for a missing tag value (White/Black/Round).
_UNKNOWN = "?"
# The non-decisive Result tag (ongoing game); never passes the decisive filter.
_NONDECISIVE = "*"

# games-list / summary wire keys.
_KEY_WHITE = "white"
_KEY_BLACK = "black"
_KEY_RESULT = "result"
_KEY_OPENING = "opening"


@dataclass
class EngineRecord:
    name: str
    wins: int = 0
    losses: int = 0
    draws: int = 0
    # Logistic Elo: head-to-head score-percentage Elo. Well-defined for
    # 2-engine tournaments and (per-challenger vs leader) for gauntlets.
    # ``elo_margin_95`` is the half-width of the 95% normal CI on Elo,
    # propagated from the per-game W/L/D score variance.
    elo: float | None = None
    elo_margin_95: float | None = None
    # ordo-style joint-fit Elo: iterative Elo-update over the full game
    # graph, mean-centered. Matches the output of ordo (https://github.com/
    # michiguel/Ordo) with -a 0 -M -D to within rounding. Populated for
    # any tour with >= 2 engines. None for engines purged from the fit
    # (all-wins / all-losses).
    elo_ordo: float | None = None
    elo_ordo_margin_95: float | None = None

    @property
    def games(self) -> int:
        return self.wins + self.losses + self.draws

    @property
    def points(self) -> float:
        return self.wins + 0.5 * self.draws

    @property
    def score_pct(self) -> float:
        if self.games == 0:
            return 0.0
        return self.points / self.games

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "wins": self.wins,
            "losses": self.losses,
            "draws": self.draws,
            "games": self.games,
            "points": self.points,
            "score_pct": self.score_pct,
            "elo": self.elo,
            "elo_margin_95": self.elo_margin_95,
            "elo_ordo": self.elo_ordo,
            "elo_ordo_margin_95": self.elo_ordo_margin_95,
        }


@dataclass
class Standings:
    engines: list[EngineRecord] = field(default_factory=list)
    games: int = 0

    def to_dict(self) -> dict:
        return {
            "games": self.games,
            "engines": [e.to_dict() for e in sorted(
                self.engines, key=lambda r: (-r.points, r.name)
            )],
        }


# SPRT status wire strings. Serialized in the sprt.status field; the
# client switches on the same values to render the conclusion banner.
SPRT_H0 = "H0"
SPRT_H1 = "H1"
SPRT_CONTINUE = "continue"


@dataclass
class SprtResult:
    """Outcome of a Sequential Probability Ratio Test on the PGN to date.

    ``status`` is one of:
      - ``SPRT_H1``       — accept H1 (engine A is stronger by `elo1` or more)
      - ``SPRT_H0``       — accept H0 (engine A is no stronger than `elo0`)
      - ``SPRT_CONTINUE`` — neither bound reached; keep playing
    """
    llr: float
    lower_bound: float
    upper_bound: float
    status: str
    pairs: int
    elo0: float
    elo1: float
    model: str

    def to_dict(self) -> dict:
        return {
            "llr": self.llr,
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
            "status": self.status,
            "pairs": self.pairs,
            "elo0": self.elo0,
            "elo1": self.elo1,
            "model": self.model,
        }


# Cache: pgn_path -> (mtime_ns, size, keyed_games_tuple). Entries are
# 4-tuples (round, white, black, result); ``_iter_games`` strips the round
# for callers that don't need it. PGN is append-only, so (mtime, size) is
# a sound invalidation key. One entry per path keeps memory bounded; the
# entry self-replaces on every change.
_iter_games_cache: dict[
    Path, tuple[int, int, tuple[tuple[str, str, str, str], ...]]
] = {}

# Byte offsets of decisive games: offsets[i] is the file position of the
# (i+1)-th decisive game. Keyed by (mtime_ns, size); same invalidation as
# _iter_games_cache. Lets read_game_record seek directly to game N.
_game_offsets_cache: dict[Path, tuple[int, int, list[int]]] = {}

# pgn_path -> (mtime_ns, size, games). O(1) fast path for unchanged polls.
_games_list_cache: dict[Path, tuple[int, int, list[dict]]] = {}

# (pgn_path, byte_offset) -> finished-game record. The PGN is append-only
# within a run, so an offset's game never changes; a rebuild re-parses only
# the just-finished game. Stop/restart wipes the file -- the orchestrator
# calls forget() at that point, the only event that invalidates this.
_opening_memo: dict[tuple[Path, int], dict] = {}

# Plies replayed per game to identify its opening. ECO lines rarely exceed
# ~12 moves, so 24 plies covers them while bounding replay cost on big PGNs.
_OPENING_PLIES = int(os.environ.get("SV_OPENING_PLIES", "24"))


def _iter_games_keyed(pgn_path: Path):
    """Yield ``(round, white, black, result)`` 4-tuples for each game.

    Same dedup and skip rules as ``_iter_games``; this is the version
    that retains the Round tag for callers that need it (e.g. partial
    pair detection).
    """
    try:
        st = pgn_path.stat()
    except FileNotFoundError:
        _iter_games_cache.pop(pgn_path, None)
        _game_offsets_cache.pop(pgn_path, None)
        return
    cached = _iter_games_cache.get(pgn_path)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        yield from cached[2]
        return
    games = tuple(_iter_games_uncached(pgn_path))
    _iter_games_cache[pgn_path] = (st.st_mtime_ns, st.st_size, games)
    yield from games


def _iter_games(pgn_path: Path):
    """Yield ``(white_name, black_name, result_tag)`` for each game in the PGN.

    Skips games with a missing or non-decisive result tag (`*` etc).
    Tolerates an empty/missing file (yields nothing).

    Every decisive game is yielded in file order; no dedup. ``Round``
    is not a reliable pair ID, so we trust the PGN as written and let
    pair-formation (``_form_pairs``) decide what's an orphan vs a
    complete pair.
    """
    for _round, white, black, result in _iter_games_keyed(pgn_path):
        yield (white, black, result)


def _iter_games_uncached(pgn_path: Path):
    # Header-only scan: standings/games/SPRT only need White/Black/Result/Round.
    # Avoids ``chess.pgn.read_game``'s full move-tree parse (>50x slower on
    # multi-MB PGNs). Section boundary = a non-tag line after we've seen at
    # least one tag in the current game; lines before any tag are skipped.
    # NOTE: trade-off -- a `;`-comment line between tag block and moves
    # would emit early. fastchess never emits those.
    cur: dict[str, str] = {}
    in_tags = False

    def emit():
        if not cur:
            return None
        result = cur.get(_TAG_RESULT, _NONDECISIVE)
        if result in _DECISIVE_RESULTS:
            white = cur.get(_TAG_WHITE, _UNKNOWN)
            black = cur.get(_TAG_BLACK, _UNKNOWN)
            round_tag = cur.get(_TAG_ROUND, "")
            value = (round_tag, white, black, result)
            cur.clear()
            return value
        cur.clear()
        return None

    with pgn_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _TAG_RE.match(line)
            if m is not None:
                in_tags = True
                name = m.group(1)
                if name in _WANTED_TAGS:
                    cur[name] = m.group(2)
            elif in_tags:
                v = emit()
                if v is not None:
                    yield v
                in_tags = False
        v = emit()
        if v is not None:
            yield v


# Indices into the 4-tuples yielded by ``_iter_games_keyed`` (round, white,
# black, result). Used by ``_form_pairs`` callers that also need the
# original index back into the input sequence.
def _form_pairs(
    keyed: list[tuple[str, str, str, str]],
    *,
    paired: bool = True,
) -> tuple[list[tuple[int, int]], list[int]]:
    """Identify color-flipped pairs and orphans by structure, not by Round.

    Input: list of (round, white, black, result) entries in file order
    (no dedup -- see ``_iter_games_uncached``).

    Bucketing: each entry lands in a bucket keyed by
    ``(round, frozenset({white, black}))``. Within each bucket, games are
    paired greedily by color-flip -- each ``A-as-white`` game claims one
    unmatched ``B-as-white`` game in its bucket.

    Returns ``(pairs, orphans)`` where:
    - ``pairs`` is a list of ``(i_first_color, i_second_color)`` index
      tuples into the input. Earlier entry in the input is ``i_first_color``.
    - ``orphans`` is a list of input indices that did not pair.

    Entries with no Round tag (or ``Round == "?"``) cannot be bucketed
    and are reported as orphans.

    If ``paired=False`` the function returns ``([], [])``: single-game
    tours have no pair concept, so no game is an orphan. Callers that
    need to drop unpaired games must not invoke this with ``paired=False``.
    """
    if not paired or not keyed:
        return [], []

    # Bucket entries by (round, engine-set); preserve file-order within
    # each bucket so the matching is deterministic.
    buckets: dict[tuple[str, frozenset[str]], list[int]] = {}
    no_round: list[int] = []
    for i, (round_tag, white, black, _result) in enumerate(keyed):
        if not round_tag or round_tag == _UNKNOWN:
            no_round.append(i)
            continue
        key = (round_tag, frozenset((white, black)))
        buckets.setdefault(key, []).append(i)

    pairs: list[tuple[int, int]] = []
    orphans: list[int] = list(no_round)

    for indices in buckets.values():
        # Within a bucket, split by which engine is White. Pair greedily:
        # the i-th `A-as-white` game pairs with the i-th `B-as-white` game,
        # in file order. Surplus from either side becomes orphans.
        if len(indices) == 1:
            orphans.append(indices[0])
            continue
        # Use the first entry's white name as the partition pivot. Any
        # third name would mean the bucket key is wrong -- impossible by
        # construction (the frozenset has at most 2 members).
        first_white = keyed[indices[0]][1]
        side_a: list[int] = []  # games where first_white is White
        side_b: list[int] = []  # games where the other engine is White
        for i in indices:
            if keyed[i][1] == first_white:
                side_a.append(i)
            else:
                side_b.append(i)
        n = min(len(side_a), len(side_b))
        for k in range(n):
            ia, ib = side_a[k], side_b[k]
            # Earlier file-order index first, for stable downstream behavior.
            if ia < ib:
                pairs.append((ia, ib))
            else:
                pairs.append((ib, ia))
        orphans.extend(side_a[n:])
        orphans.extend(side_b[n:])

    return pairs, orphans


def read_game_pgn(pgn_path: Path, game_n: int) -> str | None:
    """Return the PGN text of the 1-based Nth completed game, or None.

    Counts only games with a decisive Result, matching ``pgn_tail``'s
    ``game_n`` so a Replay click resolves to the same game the
    reconciliation event identified.
    """
    record = read_game_record(pgn_path, game_n)
    return record["pgn"] if record else None


def _build_game_offsets(pgn_path: Path, f) -> list[int]:
    """Scan open text file and return byte offsets of decisive games."""
    offsets: list[int] = []
    f.seek(0)
    while True:
        offset = f.tell()
        headers = chess.pgn.read_headers(f)
        if headers is None:
            break
        if headers.get(_TAG_RESULT, _NONDECISIVE) in _DECISIVE_RESULTS:
            offsets.append(offset)
    return offsets


def _get_game_offsets(pgn_path: Path, st) -> list[int]:
    cached = _game_offsets_cache.get(pgn_path)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    with pgn_path.open("r", encoding="utf-8", errors="replace") as f:
        offsets = _build_game_offsets(pgn_path, f)
    _game_offsets_cache[pgn_path] = (st.st_mtime_ns, st.st_size, offsets)
    return offsets


def _read_game_at_offset(f, offset: int):
    f.seek(offset)
    return chess.pgn.read_game(f)


def read_game_record(pgn_path: Path, game_n: int) -> dict | None:
    """Return PGN + final-position metadata for the Nth completed game.

    Result keys: ``pgn``, ``hash``, ``final_fen``, ``last_move`` (uci or None),
    ``engine_white``, ``engine_black``, ``result``, ``termination``.
    Used to rehydrate a frozen tournament game window with no live WS.
    ``hash`` matches the SHA-256 produced by recent_imports so the client
    can compare against the currently viewed game's view_hash.
    """
    if game_n < 1:
        return None
    try:
        st = pgn_path.stat()
    except FileNotFoundError:
        return None
    offsets = _get_game_offsets(pgn_path, st)
    if game_n > len(offsets):
        return None
    with pgn_path.open("r", encoding="utf-8", errors="replace") as f:
        game = _read_game_at_offset(f, offsets[game_n - 1])
    if game is None:
        return None
    last_move_uci: str | None = None
    board: chess.Board | None = None
    for node, board, _ in walk_mainline(game):
        last_move_uci = node.move.uci()
    if board is None:
        board = game.board()
    pgn_hash = canonical_hash_from_game(game)
    pgn_text = str(game)
    white = game.headers.get(_TAG_WHITE, _UNKNOWN)
    black = game.headers.get(_TAG_BLACK, _UNKNOWN)
    # _get_game_offsets only indexes decisive games, so Result is always decisive here.
    result = game.headers[_TAG_RESULT]
    summary = {
        _KEY_WHITE: white if white != _UNKNOWN else None,
        _KEY_BLACK: black if black != _UNKNOWN else None,
        _KEY_RESULT: result,
        "side_to_move": None,
    }
    return {
        "pgn": pgn_text,
        "hash": pgn_hash,
        "summary": summary,
        "final_fen": board.fen(),
        "last_move": last_move_uci,
        "engine_white": game.headers.get(_TAG_WHITE, ""),
        "engine_black": game.headers.get(_TAG_BLACK, ""),
        _KEY_RESULT: game.headers.get(_TAG_RESULT, _NONDECISIVE),
        "termination": game.headers.get(_TAG_TERMINATION, ""),
    }


def count_partial_pairs(pgn_path: Path, *, paired: bool = True) -> int:
    """Number of orphan games -- games in the PGN whose ``(round, engine-set)``
    bucket has no color-flip partner.

    The historical "partial pair" name persists for API stability; the
    semantic is now per-orphan, not per-(round, engine-pair). For the
    common case (one orphan = one missing color in one round) the count
    matches the old definition. Multi-orphan rounds (e.g. two games of
    the same color in one bucket) are counted once per orphan, not once
    per round.

    Typically caused by an interrupted Stop on Windows
    (KILL_ON_JOB_CLOSE has no grace period) where game 1 made it to
    disk but game 2 was in flight.

    Single-game tours (``paired=False``) have no pair concept and
    always return 0.
    """
    if not paired:
        return 0
    keyed = list(_iter_games_keyed(pgn_path))
    if not keyed:
        return 0
    _pairs, orphans = _form_pairs(keyed, paired=True)
    return len(orphans)


def _game_record_at_offset(f, offset: int) -> dict | None:
    game = _read_game_at_offset(f, offset)
    if game is None:
        return None
    uci: list[str] = []
    for move in game.mainline_moves():
        uci.append(move.uci())
        if len(uci) >= _OPENING_PLIES:
            break
    opening = OpeningBook.load().lookup(uci)
    return {
        _KEY_WHITE: game.headers.get(_TAG_WHITE, _UNKNOWN),
        _KEY_BLACK: game.headers.get(_TAG_BLACK, _UNKNOWN),
        # _get_game_offsets only indexes decisive games -> Result always set.
        _KEY_RESULT: game.headers[_TAG_RESULT],
        _KEY_OPENING: opening.name if opening is not None else "",
    }


def forget(pgn_path: Path) -> None:
    """Drop all cached games-list state for ``pgn_path``. The orchestrator
    calls this when it wipes the PGN for a restart, since the offset-keyed
    memo's only invariant -- append-only bytes -- breaks across a wipe."""
    _games_list_cache.pop(pgn_path, None)
    for k in [k for k in _opening_memo if k[0] == pgn_path]:
        del _opening_memo[k]


def compute_games_list(pgn_path: Path) -> list[dict]:
    """One dict per completed game (white, black, result, opening) in PGN
    order. Indexing matches read_game_record -- both driven by
    _get_game_offsets -- so a row's position is its replay game number.
    """
    try:
        st = pgn_path.stat()
    except FileNotFoundError:
        forget(pgn_path)
        return []
    cached = _games_list_cache.get(pgn_path)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    offsets = _get_game_offsets(pgn_path, st)
    with pgn_path.open("r", encoding="utf-8", errors="replace") as f:
        games = []
        for offset in offsets:
            rec = _opening_memo.get((pgn_path, offset))
            if rec is None:
                rec = _game_record_at_offset(f, offset)
                if rec is None:
                    continue
                _opening_memo[(pgn_path, offset)] = rec
            games.append(rec)
    _games_list_cache[pgn_path] = (st.st_mtime_ns, st.st_size, games)
    return games


def elo_from_score(score: float) -> float | None:
    """Convert a 0..1 score to logistic Elo difference.

    Returns ``None`` for a perfect 0 or 1 score (Elo undefined / infinite).
    Standard formula: ``-400 * log10(1/score - 1)``.
    """
    if score <= 0.0 or score >= 1.0:
        return None
    return -400.0 * math.log10(1.0 / score - 1.0)


def elo_margin_from_wld(wins: int, losses: int, draws: int) -> float | None:
    """95% Elo half-width from a W/L/D record (head-to-head only).

    Per-game scores x_i ∈ {1, 0, 0.5}. With sample variance V on x_i,
    SE(score) = sqrt(V/n). Propagate to Elo via dElo/dscore = 400/(ln(10)·s·(1−s)).
    Returns ``None`` if score is 0/1 or n<2 (CI undefined).
    """
    n = wins + losses + draws
    if n < 2:
        return None
    s = (wins + 0.5 * draws) / n
    if s <= 0.0 or s >= 1.0:
        return None
    var = (wins * (1 - s) ** 2 + losses * (0 - s) ** 2 + draws * (0.5 - s) ** 2) / (n - 1)
    if var <= 0.0:
        return 0.0
    se_score = math.sqrt(var / n)
    delo_dscore = 400.0 / (math.log(10.0) * s * (1.0 - s))
    return 1.96 * se_score * delo_dscore


# ordo's BETA: P(score) = 1/(1 + exp((rB - rA)*BETA)).
# Calibrated so a 202-Elo gap gives 76% expectancy -- matches ordo's
# default -z 202 and `xpect(a, b, beta) = 1/(1+exp((b-a)*beta))` in xpect.c.
_ORDO_INV_BETA = 202.0 / math.log(0.76 / 0.24)  # ~175.25
_ORDO_BETA = 1.0 / _ORDO_INV_BETA


def _ordo_connected_groups(
    engine_names: list[str],
    encounters: list[tuple[str, str, float, int]],
) -> list[list[str]]:
    """Return connected components of the engine-vs-engine match graph.

    Two engines are connected if they played at least one game (in
    either direction). Ratings are only comparable within a component;
    across components the rating difference is undefined.
    """
    idx = {name: i for i, name in enumerate(engine_names)}
    parent = list(range(len(engine_names)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for w, b, _ws, _np in encounters:
        union(idx[w], idx[b])
    groups: dict[int, list[str]] = {}
    for i, name in enumerate(engine_names):
        groups.setdefault(find(i), []).append(name)
    return list(groups.values())


def _ordo_iterative_fit(
    engine_names: list[str],
    encounters: list[tuple[str, str, float, int]],
) -> dict[str, float]:
    """Joint Elo fit by iterative score-deviation update, mean-centered.

    Replicates ordo's algorithm (Ballicora, https://github.com/michiguel/Ordo,
    `rating.c::adjust_rating`). For each engine, the expected score under
    current ratings is compared to the obtained score; ratings are stepped
    toward closing the gap with a saturating multiplier; the outer loop
    halves the step size whenever the global deviation stops improving.

    ``encounters`` is a list of ``(white, black, white_score, played)``
    aggregated by color-ordered pair. The fit is order-independent.

    Returns ``{name: elo}`` mean-centered to zero. Assumes all engines in
    ``engine_names`` are in one connected component -- caller is responsible
    for splitting by component if needed.

    No white-advantage term (we don't currently estimate one). Matches ordo's
    output when run with ``-a 0 -M -D`` and the database has white_adv = 0
    (the default), which is the typical case for engine tournaments.
    """
    n = len(engine_names)
    if n < 2:
        return {}
    idx = {name: i for i, name in enumerate(engine_names)}

    obtained = [0.0] * n
    played = [0] * n
    for w, b, ws, np_ in encounters:
        iw, ib = idx[w], idx[b]
        obtained[iw] += ws
        obtained[ib] += np_ - ws
        played[iw] += np_
        played[ib] += np_

    r = [0.0] * n
    delta = 200.0
    kappa = 0.05

    def compute_dev(ratings: list[float]) -> tuple[float, list[float]]:
        expected = [0.0] * n
        for w, b, _ws, np_ in encounters:
            iw, ib = idx[w], idx[b]
            wperf = np_ / (1.0 + math.exp((ratings[ib] - ratings[iw]) * _ORDO_BETA))
            expected[iw] += wperf
            expected[ib] += np_ - wperf
        dev = sum((expected[j] - obtained[j]) ** 2 for j in range(n))
        return dev, expected

    # Outer loop halves ``delta`` whenever an inner step makes things
    # worse; convergence is reached when ``delta`` shrinks below 0.001 Elo.
    # 80 halvings cover any practical input.
    for _outer in range(80):
        for _inner in range(20000):
            dev, expected = compute_dev(r)
            new_r = list(r)
            for j in range(n):
                d = obtained[j] - expected[j]
                if played[j] == 0:
                    continue
                ratio = abs(d) / (kappa * played[j] + abs(d))
                step = delta * (1.0 if d > 0 else -1.0) * ratio
                new_r[j] += step
            m = sum(new_r) / n
            new_r = [x - m for x in new_r]
            dev2, _ = compute_dev(new_r)
            if dev2 >= dev:
                break
            r = new_r
        delta *= 0.5
        if delta < 0.001:
            break

    return {engine_names[i]: r[i] for i in range(n)}


def _ordo_fit_margins(
    engine_names: list[str],
    encounters: list[tuple[str, str, float, int]],
    ratings: dict[str, float],
) -> dict[str, float | None]:
    """95% Wald CI half-width for each engine's rating, from the Fisher
    information of the score-likelihood treated as binomial p_i = E[score_i].

    Per-game Fisher info contribution to (r_i, r_j) is:
      ``I_ii += BETA**2 * p * (1 - p)``
      ``I_ij -= BETA**2 * p * (1 - p)``
      ``I_jj += BETA**2 * p * (1 - p)``

    The mean-zero constraint is folded in by dropping the last engine's
    row/column from the info matrix and inverting the reduced (n-1) x (n-1)
    matrix; the last engine's variance is derived from the constraint
    ``r_{n-1} = -sum_{j<n-1} r_j``.

    Note: ordo uses a bootstrap simulation (resampling games, refitting)
    that produces CI roughly 1.4--2x tighter than this Wald form. We do
    not replicate the bootstrap because it is O(N_sims) more expensive.
    The Wald form is asymptotically equivalent and more conservative;
    the ratio is roughly constant per tour so cross-comparison with
    ordo's CI is unambiguous up to a scale factor.
    """
    n = len(engine_names)
    if n < 2:
        return {name: None for name in engine_names}
    if n == 2:
        margins = {}
        info = 0.0
        for w, b, _ws, np_ in encounters:
            ra = ratings[w]
            rb = ratings[b]
            p = 1.0 / (1.0 + math.exp((rb - ra) * _ORDO_BETA))
            info += np_ * _ORDO_BETA * _ORDO_BETA * p * (1.0 - p)
        if info <= 0.0:
            return {name: None for name in engine_names}
        se_diff = 1.0 / math.sqrt(info)
        m = 1.96 * se_diff / 2.0
        for name in engine_names:
            margins[name] = m
        return margins

    idx = {name: i for i, name in enumerate(engine_names)}
    info = [[0.0] * n for _ in range(n)]
    for w, b, _ws, np_ in encounters:
        iw, ib = idx[w], idx[b]
        ra = ratings[w]
        rb = ratings[b]
        p = 1.0 / (1.0 + math.exp((rb - ra) * _ORDO_BETA))
        c = np_ * _ORDO_BETA * _ORDO_BETA * p * (1.0 - p)
        info[iw][iw] += c
        info[ib][ib] += c
        info[iw][ib] -= c
        info[ib][iw] -= c

    # Drop last row/col to fold in mean-zero constraint.
    size = n - 1
    aug = [row[:size] + [1.0 if i == j else 0.0 for j in range(size)]
           for i, row in enumerate(info[:size])]
    # Gauss-Jordan inversion in-place.
    for col in range(size):
        piv = col
        for r2 in range(col, size):
            if abs(aug[r2][col]) > abs(aug[piv][col]):
                piv = r2
        if abs(aug[piv][col]) < 1e-12:
            return {name: None for name in engine_names}
        aug[col], aug[piv] = aug[piv], aug[col]
        pv = aug[col][col]
        aug[col] = [v / pv for v in aug[col]]
        for r2 in range(size):
            if r2 == col:
                continue
            f = aug[r2][col]
            if f == 0.0:
                continue
            aug[r2] = [aug[r2][k] - f * aug[col][k] for k in range(2 * size)]
    inv = [row[size:] for row in aug]

    margins: dict[str, float | None] = {}
    for i in range(size):
        v = inv[i][i]
        margins[engine_names[i]] = 1.96 * math.sqrt(v) if v >= 0 else None
    # Last engine's variance under constraint: Var(-sum others) = sum_{i,j} Cov(r_i, r_j)
    var_last = 0.0
    for i in range(size):
        for j in range(size):
            var_last += inv[i][j]
    margins[engine_names[size]] = 1.96 * math.sqrt(var_last) if var_last >= 0 else None
    return margins


def ordo_fit(
    engine_names: list[str],
    encounters: list[tuple[str, str, float, int]],
    *,
    wins: dict[str, int] | None = None,
    losses: dict[str, int] | None = None,
) -> dict[str, tuple[float | None, float | None]]:
    """Joint mean-centered Elo fit replicating ordo's output.

    Returns ``{name: (elo, margin_95)}`` for every engine in
    ``engine_names``. Engines with all wins / all losses (against the
    rest of the pool) are purged from the joint fit: their entries are
    ``(None, None)``, matching ordo's ``-G`` purge behavior. Disconnected
    components are fit independently and each anchored to its own mean.

    ``wins``/``losses`` are name -> count dicts; required for purge
    detection. If omitted, no engine is purged.
    """
    if not engine_names:
        return {}

    # Purge candidates: an engine is "all wins" if it never lost, "all
    # losses" if it never won. These have divergent rating under MLE; we
    # exclude them from the joint fit and surface (None, None).
    purged: set[str] = set()
    if wins is not None and losses is not None:
        for n in engine_names:
            if (wins.get(n, 0) > 0 and losses.get(n, 0) == 0) or \
               (losses.get(n, 0) > 0 and wins.get(n, 0) == 0):
                purged.add(n)

    # Remaining engines + encounters not involving purged engines.
    remaining = [n for n in engine_names if n not in purged]
    if not remaining:
        return {n: (None, None) for n in engine_names}
    remaining_set = set(remaining)
    encs = [
        (w, b, ws, np_)
        for (w, b, ws, np_) in encounters
        if w in remaining_set and b in remaining_set
    ]

    # Connected-component decomposition; fit each separately.
    components = _ordo_connected_groups(remaining, encs)
    result: dict[str, tuple[float | None, float | None]] = {
        n: (None, None) for n in purged
    }
    for comp in components:
        comp_set = set(comp)
        comp_encs = [(w, b, ws, np_) for (w, b, ws, np_) in encs
                     if w in comp_set and b in comp_set]
        if len(comp) == 1:
            # Singleton: no opponent in remaining pool, rating undefined.
            result[comp[0]] = (None, None)
            continue
        ratings = _ordo_iterative_fit(comp, comp_encs)
        margins = _ordo_fit_margins(comp, comp_encs, ratings)
        for name in comp:
            result[name] = (ratings.get(name), margins.get(name))
    return result


def games_played_from_config(config_path: Path) -> int | None:
    """Sum of W+L+D across fastchess's stats; authoritative game count.
    Returns None on missing/unparseable file; 0 on empty stats. Callers
    fall back to the PGN count when None (pre-first-autosave)."""
    if not config_path.exists():
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    stats = data.get("stats")
    if not isinstance(stats, dict):
        return None
    total = 0
    try:
        for entry in stats.values():
            if not isinstance(entry, dict):
                continue
            total += int(entry.get("wins") or 0)
            total += int(entry.get("losses") or 0)
            total += int(entry.get("draws") or 0)
    except (TypeError, ValueError):
        return None
    return total


def compute_standings(
    pgn_path: Path,
    tournament_type: str = "roundrobin",
) -> Standings:
    """Tally W/L/D per engine across all games in ``games.pgn``.

    The PGN may be empty, missing, or partially-written; results from
    valid games are counted, the rest skipped.
    """
    # wld[a][b] = [wins, losses, draws] for engine a vs engine b
    wld: dict[str, dict[str, list[int]]] = {}
    records: dict[str, EngineRecord] = {}
    games = 0
    # encounters keyed by (white, black) -> [white_score, played]; consumed
    # by the ordo-style joint fit below. White-score = W + 0.5*D, since the
    # fit operates on score-percentage per ordo's xpect.c.
    encounters: dict[tuple[str, str], list[float]] = {}

    def rec(name: str) -> EngineRecord:
        if name not in records:
            records[name] = EngineRecord(name=name)
        return records[name]

    for white, black, result in _iter_games(pgn_path):
        games += 1
        w = rec(white)
        b = rec(black)
        enc = encounters.setdefault((white, black), [0.0, 0])
        enc[1] += 1
        if result == _WHITE_WIN:
            w.wins += 1
            b.losses += 1
            wld.setdefault(white, {}).setdefault(black, [0, 0, 0])[0] += 1
            wld.setdefault(black, {}).setdefault(white, [0, 0, 0])[1] += 1
            enc[0] += 1.0
        elif result == _BLACK_WIN:
            b.wins += 1
            w.losses += 1
            wld.setdefault(black, {}).setdefault(white, [0, 0, 0])[0] += 1
            wld.setdefault(white, {}).setdefault(black, [0, 0, 0])[1] += 1
            # White scored 0.
        else:  # draw
            w.draws += 1
            b.draws += 1
            wld.setdefault(white, {}).setdefault(black, [0, 0, 0])[2] += 1
            wld.setdefault(black, {}).setdefault(white, [0, 0, 0])[2] += 1
            enc[0] += 0.5

    engines = list(records.values())
    if len(engines) == 2:
        # Head-to-head Elo is well-defined for any 2-engine tournament.
        for e in engines:
            e.elo = elo_from_score(e.score_pct) if e.games else None
            e.elo_margin_95 = elo_margin_from_wld(e.wins, e.losses, e.draws)
    elif len(engines) >= 3 and tournament_type == "gauntlet":
        # Leader plays every other engine; auto-detect by max game count.
        # Per-challenger Elo is head-to-head vs the leader only.
        leader = max(engines, key=lambda e: e.games)
        for e in engines:
            vs = wld.get(e.name, {}).get(leader.name)
            if vs is None or not sum(vs):
                continue
            wi, li, di = vs
            score = (wi + 0.5 * di) / sum(vs)
            e.elo = elo_from_score(score)
            e.elo_margin_95 = elo_margin_from_wld(wi, li, di)

    # ordo-style joint fit: populated for every tour with >= 2 engines.
    # Per-engine `elo_ordo` is the mean-centered rating; engines purged
    # from the fit (all-wins/all-losses) get None. Cross-checks against
    # an external ordo run (-a 0 -M -D) to within rounding.
    if len(engines) >= 2:
        names = [e.name for e in engines]
        encs = [(w, b, ws, p) for (w, b), (ws, p) in encounters.items()]
        wins_map = {e.name: e.wins for e in engines}
        losses_map = {e.name: e.losses for e in engines}
        fit = ordo_fit(names, encs, wins=wins_map, losses=losses_map)
        for e in engines:
            elo, margin = fit.get(e.name, (None, None))
            e.elo_ordo = elo
            e.elo_ordo_margin_95 = margin

    return Standings(engines=engines, games=games)


# ---------------------------------------------------------------------------
# SPRT (Sequential Probability Ratio Test) -- pentanomial variant
#
# Games are paired: engine A plays both colors vs B on the same opening,
# so each pair scores in {0, 0.5, 1, 1.5, 2} for A. The five-bin pair
# distribution gives a tighter variance estimate than per-game W/L/D.
#
# Convention: elo0/elo1 are *logistic Elo* (per-game), the same scale as
# everywhere else in the UI. Internally we convert each hypothesis to a
# per-pair mean score offset and run a Gaussian LLR against the observed
# pair scores using the sample pair-variance.
#
# Note: this differs from fastchess's own `-sprt model=normalized`, which
# parameterizes elo0/elo1 in *normalized Elo* units (mean shift divided
# by pair stdev). The two tests reach the same accept/reject decision
# asymptotically, but the LLR magnitudes shown here will not match
# fastchess's stdout for the same elo bounds.
#
# Math summary (per-pair scale, A's per-pair score s_i in [0, 2]):
#   x_i = s_i / 2                                  (per-game score in [0, 1])
#   mu  = (1/N) sum x_i
#   var = (1/(N-1)) sum (x_i - mu)^2               (Bessel-corrected sample var)
#   For hypothesis Hk: mu_k = 0.5 + elo_k * ln(10) / 1600
#   LLR = N * (mu_1 - mu_0) * (mu - (mu_0 + mu_1)/2) / var
#
# Bounds: log(beta/(1-alpha)) and log((1-beta)/alpha).
# Reference: fastchess-cli docs and the Bayesian-Elo project notes.
# ---------------------------------------------------------------------------

_PAIR_BINS = (0.0, 0.5, 1.0, 1.5, 2.0)


def _iter_pairs(
    pgn_path: Path,
    *,
    engine_a: str,
    engine_b: str,
) -> list[tuple[str, str, float]]:
    """Identify color-flipped paired matches via ``_form_pairs``.

    ``Round`` alone is not a reliable pair ID (it can be reused across a
    Pause/Resume boundary), so pairing is structural: games are bucketed
    by ``(round, frozenset({white, black}))`` and matched within each
    bucket by color-flip.

    Only buckets whose engine set is exactly ``{engine_a, engine_b}``
    contribute pairs here. Buckets from other match-ups in a gauntlet
    PGN are ignored.

    ``engine_a`` is the candidate (the "new" engine being tested) and
    ``engine_b`` is the baseline. Both are taken from the tournament
    config in creation order, not inferred from PGN file order --
    under concurrency the first-completed game may be from any round.

    Returns ``[(engine_a, engine_b, a_score_in_pair), ...]`` in
    ``_form_pairs`` order (first-seen Round, then first-seen file index
    within a bucket).
    """
    keyed = list(_iter_games_keyed(pgn_path))
    if not keyed:
        return []

    pair_indices, orphans = _form_pairs(keyed, paired=True)
    a_name, b_name = engine_a, engine_b
    engines = {a_name, b_name}

    for oi in orphans:
        rd, w, b, _r = keyed[oi]
        if {w, b} != engines:
            # Orphan from a different match-up (gauntlet): not our problem,
            # not a partial we should warn about for the A-vs-B SPRT.
            continue
        log.debug(
            "SPRT %s: round %s has orphan game (expected 2-game color-flipped "
            "pair) -- skipped",
            pgn_path.name, rd or _UNKNOWN,
        )

    pairs: list[tuple[str, str, float]] = []
    for i, j in pair_indices:
        _rdi, wi, bi, ri = keyed[i]
        _rdj, wj, bj, rj = keyed[j]
        if {wi, bi} != engines:
            # Different engine set (e.g. another match-up in a gauntlet).
            log.warning(
                "SPRT %s: round %s skipped (engines %s vs %s, "
                "expected %s vs %s)",
                pgn_path.name, _rdi or _UNKNOWN, wi, bi, a_name, b_name,
            )
            continue
        score = 0.0
        for white, black, result in ((wi, bi, ri), (wj, bj, rj)):
            if result == _WHITE_WIN:
                score += 1.0 if white == a_name else 0.0
            elif result == _BLACK_WIN:
                score += 1.0 if black == a_name else 0.0
            else:
                score += 0.5
        pairs.append((a_name, b_name, score))
    return pairs


def _sprt_bounds(alpha: float, beta: float) -> tuple[float, float]:
    """Wald boundaries for an SPRT at significance ``alpha`` and power ``1-beta``."""
    return math.log(beta / (1.0 - alpha)), math.log((1.0 - beta) / alpha)


_SUPPORTED_SPRT_MODELS = frozenset({"normalized", "pentanomial", "logistic"})


def compute_sprt(
    pgn_path: Path,
    params: dict,
    *,
    engine_a: str,
    engine_b: str,
) -> SprtResult:
    """Compute SPRT LLR + decision over the games in ``pgn_path``.

    ``engine_a`` / ``engine_b`` are the candidate and baseline names from
    the tournament config (engine[0] / engine[1] in creation order).
    Required: under concurrency the PGN's first-completed game is not a
    reliable indicator of which engine is the candidate.

    ``params`` keys:
      - ``elo0``  (float, required)
      - ``elo1``  (float, required)
      - ``alpha`` (float, default 0.05)
      - ``beta``  (float, default 0.05)
      - ``model`` (str,  default "normalized"; also accepts "pentanomial"
        as an alias, and "logistic" for the trinomial W/D/L model)
    """
    elo0 = float(params["elo0"])
    elo1 = float(params["elo1"])
    alpha = float(params.get("alpha", 0.05))
    beta = float(params.get("beta", 0.05))
    model = params.get("model", "normalized")
    # "pentanomial" is the UI-facing name; "normalized" is the internal alias.
    if model == "pentanomial":
        model = "normalized"
    if model not in _SUPPORTED_SPRT_MODELS:
        raise NotImplementedError(f"SPRT model {model!r} not implemented")
    if elo0 >= elo1:
        raise ValueError(f"SPRT requires elo0 < elo1; got elo0={elo0}, elo1={elo1}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"SPRT alpha must be in (0, 1); got {alpha}")
    if not 0.0 < beta < 1.0:
        raise ValueError(f"SPRT beta must be in (0, 1); got {beta}")

    lower, upper = _sprt_bounds(alpha, beta)

    if model == "logistic":
        return _compute_sprt_logistic(
            pgn_path,
            elo0=elo0, elo1=elo1, lower=lower, upper=upper,
            engine_a=engine_a, engine_b=engine_b,
        )

    pairs = _iter_pairs(pgn_path, engine_a=engine_a, engine_b=engine_b)
    n = len(pairs)

    # Per-pair score in [0, 1]: pair_total / 2.
    if n == 0:
        return SprtResult(
            llr=0.0,
            lower_bound=lower,
            upper_bound=upper,
            status=SPRT_CONTINUE,
            pairs=0,
            elo0=elo0,
            elo1=elo1,
            model=model,
        )

    scores = [s / 2.0 for _, _, s in pairs]
    mu = sum(scores) / n

    if n < 2:
        # Variance ill-defined; emit LLR=0 and keep playing.
        return SprtResult(
            llr=0.0,
            lower_bound=lower,
            upper_bound=upper,
            status=SPRT_CONTINUE,
            pairs=n,
            elo0=elo0,
            elo1=elo1,
            model=model,
        )

    # Pentanomial variance: sample variance of per-pair score around μ.
    var = sum((s - mu) ** 2 for s in scores) / (n - 1)
    if var <= 0.0:
        # All pairs scored identically -- the sample carries no spread
        # and so no information about the hypothesis. Returning a
        # variance-floored LLR would invent significance; treat as the
        # n<2 case instead.
        return SprtResult(
            llr=0.0,
            lower_bound=lower,
            upper_bound=upper,
            status=SPRT_CONTINUE,
            pairs=n,
            elo0=elo0,
            elo1=elo1,
            model=model,
        )

    # Logistic-Elo per-game score offset: dscore ~= elo * ln(10) / 1600
    # (linearization of 1/(1+10^(-elo/400)) at score=0.5). Same offset
    # applies in per-pair score units, since mu is already per-game.
    def elo_to_score(elo: float) -> float:
        return 0.5 + elo * math.log(10.0) / 1600.0

    s0 = elo_to_score(elo0)
    s1 = elo_to_score(elo1)

    # Gaussian LLR with variance estimated from the sample (conventional
    # SPRT shortcut, not a strict Wald test):
    #   LLR = n * (s1 - s0) * (mu - (s0 + s1)/2) / var
    llr = n * (s1 - s0) * (mu - (s0 + s1) / 2.0) / var

    if llr >= upper:
        status = SPRT_H1
    elif llr <= lower:
        status = SPRT_H0
    else:
        status = SPRT_CONTINUE

    return SprtResult(
        llr=llr,
        lower_bound=lower,
        upper_bound=upper,
        status=status,
        pairs=n,
        elo0=elo0,
        elo1=elo1,
        model=model,
    )


# ---------------------------------------------------------------------------
# Logistic SPRT (per-game W/D/L trinomial)
#
# Standard cutechess-cli style: per-game LLR with a trinomial model
# parameterized by the observed draw rate. For hypothesis Hk:
#     score_k = 1 / (1 + 10^(-elo_k/400))
#     Pw_k    = score_k - d_obs/2
#     Pl_k    = 1 - score_k - d_obs/2
#     Pd_k    = d_obs
# LLR = w*log(Pw1/Pw0) + l*log(Pl1/Pl0)   (draw term cancels: Pd1 == Pd0)
#
# If either Pw_k or Pl_k is non-positive (extreme elo bounds vs observed
# draw rate), the trinomial is degenerate and the sample carries no
# usable information; emit LLR=0 / continue, matching the pentanomial
# zero-variance handling.
# ---------------------------------------------------------------------------


def _compute_sprt_logistic(
    pgn_path: Path,
    *,
    elo0: float,
    elo1: float,
    lower: float,
    upper: float,
    engine_a: str,
    engine_b: str,
) -> SprtResult:
    """Compute logistic-model SPRT over individual W/D/L games.

    Game count (not pair count) is what's reported in ``pairs`` here -- the
    field is reused so the UI doesn't need a model-specific branch. The
    label "pairs" remains accurate for the pentanomial/normalized path; for
    logistic it counts decided games, which is the appropriate analogue.

    ``engine_a`` / ``engine_b`` identify the candidate and baseline from
    tournament config; same reasoning as ``compute_sprt``.
    """
    games = list(_iter_games(pgn_path))
    if not games:
        return SprtResult(
            llr=0.0, lower_bound=lower, upper_bound=upper,
            status=SPRT_CONTINUE, pairs=0,
            elo0=elo0, elo1=elo1, model="logistic",
        )

    a_name, b_name = engine_a, engine_b
    wins = losses = draws = 0
    for white, black, result in games:
        if {white, black} != {a_name, b_name}:
            log.warning(
                "SPRT %s: game skipped (engines %s vs %s, expected %s vs %s)",
                pgn_path.name, white, black, a_name, b_name,
            )
            continue
        if result == _WHITE_WIN:
            if white == a_name:
                wins += 1
            else:
                losses += 1
        elif result == _BLACK_WIN:
            if black == a_name:
                wins += 1
            else:
                losses += 1
        else:
            draws += 1

    n = wins + losses + draws
    if n == 0:
        return SprtResult(
            llr=0.0, lower_bound=lower, upper_bound=upper,
            status=SPRT_CONTINUE, pairs=0,
            elo0=elo0, elo1=elo1, model="logistic",
        )

    d_obs = draws / n
    s0 = 1.0 / (1.0 + math.pow(10.0, -elo0 / 400.0))
    s1 = 1.0 / (1.0 + math.pow(10.0, -elo1 / 400.0))
    pw0, pl0 = s0 - d_obs / 2.0, 1.0 - s0 - d_obs / 2.0
    pw1, pl1 = s1 - d_obs / 2.0, 1.0 - s1 - d_obs / 2.0
    if min(pw0, pl0, pw1, pl1) <= 0.0:
        return SprtResult(
            llr=0.0, lower_bound=lower, upper_bound=upper,
            status=SPRT_CONTINUE, pairs=n,
            elo0=elo0, elo1=elo1, model="logistic",
        )

    llr = wins * math.log(pw1 / pw0) + losses * math.log(pl1 / pl0)
    if llr >= upper:
        status = SPRT_H1
    elif llr <= lower:
        status = SPRT_H0
    else:
        status = SPRT_CONTINUE

    return SprtResult(
        llr=llr, lower_bound=lower, upper_bound=upper,
        status=status, pairs=n,
        elo0=elo0, elo1=elo1, model="logistic",
    )
