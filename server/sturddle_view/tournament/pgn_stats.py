"""PGN-based standings, Elo, and SPRT computation.

``games.pgn`` is the source of truth (not the runner's stdout summary), so
Stop/Resume across the same PGN yields correct cumulative numbers.
Reads the PGN; results are cached per-path keyed by (mtime, size).
"""
from __future__ import annotations

import gzip
import json
import logging
import math
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


# Result tags we recognize. Anything else (`*`, missing, malformed) is
# treated as "no result" and the game is skipped from tallies.
_WHITE_WIN = "1-0"
_BLACK_WIN = "0-1"
_DRAW_VALUES = frozenset({"1/2-1/2", "½-½"})

# PGN tag line: [Name "value"]. Non-greedy value match — we don't honor
# \"-escapes; the four headers we read never contain quotes in fastchess output.
_TAG_RE = re.compile(r'\[(\w+)\s+"(.*?)"\]\s*$')
_TAG_RE_BLOCK = re.compile(r'^\[(\w+)\s+"(.*?)"\]', re.MULTILINE)

# Tags we actually use; ignore the rest to skip a dict write per line.
_WANTED_TAGS = frozenset({"White", "Black", "Result", "Round"})


@dataclass
class EngineRecord:
    name: str
    wins: int = 0
    losses: int = 0
    draws: int = 0
    # Populated only when there are exactly two engines in the tournament:
    # then ``score_pct`` is a head-to-head score and Elo is well-defined.
    # ``elo_margin_95`` is the half-width of the 95% normal CI on Elo,
    # propagated from the per-game W/L/D score variance.
    elo: float | None = None
    elo_margin_95: float | None = None

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


@dataclass
class SprtResult:
    """Outcome of a Sequential Probability Ratio Test on the PGN to date.

    ``status`` is one of:
      - ``"H1"``    — accept H1 (engine A is stronger by `elo1` or more)
      - ``"H0"``    — accept H0 (engine A is no stronger than `elo0`)
      - ``"continue"`` — neither bound reached; keep playing
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

    Dedup: when two games share the same ``(Round, White, Black)`` key,
    only the *last* one is yielded. This handles the at-most-one
    duplicate game produced by fastchess's resume mechanism when SIGKILL
    lands between PGN-append and cfg.json-save (see Resume design in
    docs/tournament-spec.md). Games without a ``[Round]`` header bypass
    dedup (no key to collide on) — fastchess always emits Round, so the
    fallback only matters for hand-crafted PGNs.
    """
    for _round, white, black, result in _iter_games_keyed(pgn_path):
        yield (white, black, result)


def _iter_games_uncached(pgn_path: Path):
    # Header-only scan: standings/games/SPRT only need White/Black/Result/Round.
    # Avoids ``chess.pgn.read_game``'s full move-tree parse (>50× slower on
    # multi-MB PGNs). Section boundary = a non-tag line after we've seen at
    # least one tag in the current game; lines before any tag are skipped.
    # NOTE: trade-off — a `;`-comment line between tag block and moves
    # would emit early. fastchess never emits those.
    entries: list[tuple[tuple[str, str, str] | None, tuple[str, str, str, str]]] = []
    cur: dict[str, str] = {}
    in_tags = False

    def emit() -> None:
        if not cur:
            return
        result = cur.get("Result", "*")
        if result == _WHITE_WIN or result == _BLACK_WIN or result in _DRAW_VALUES:
            white = cur.get("White", "?")
            black = cur.get("Black", "?")
            round_tag = cur.get("Round", "")
            value = (round_tag, white, black, result)
            has_round = round_tag and round_tag != "?"
            key = (round_tag, white, black) if has_round else None
            entries.append((key, value))
        cur.clear()

    with pgn_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _TAG_RE.match(line)
            if m is not None:
                in_tags = True
                name = m.group(1)
                if name in _WANTED_TAGS:
                    cur[name] = m.group(2)
            elif in_tags:
                emit()
                in_tags = False
        emit()

    last_value: dict[tuple[str, str, str], tuple[str, str, str, str]] = {
        k: v for k, v in entries if k is not None
    }
    emitted: set[tuple[str, str, str]] = set()
    for key, value in entries:
        if key is None:
            yield value
        elif key not in emitted:
            emitted.add(key)
            yield last_value[key]


_DECISIVE_RESULTS = frozenset({_WHITE_WIN, _BLACK_WIN, *_DRAW_VALUES})


def read_game_pgn(pgn_path: Path, game_n: int) -> str | None:
    """Return the PGN text of the 1-based Nth completed game, or None.

    Counts only games with a decisive Result, matching ``pgn_tail``'s
    ``game_n`` so a Replay click resolves to the same game the
    reconciliation event identified.
    """
    record = read_game_record(pgn_path, game_n)
    return record["pgn"] if record else None


def read_game_record(pgn_path: Path, game_n: int) -> dict | None:
    """Return PGN + final-position metadata for the Nth completed game.

    Result keys: ``pgn``, ``final_fen``, ``last_move`` (uci or None),
    ``engine_white``, ``engine_black``, ``result``, ``termination``.
    Used to rehydrate a frozen tournament game window with no live WS.
    """
    if game_n < 1:
        return None
    import chess.pgn
    seen = 0
    with pgn_path.open("r", encoding="utf-8", errors="replace") as f:
        while True:
            offset = f.tell()
            headers = chess.pgn.read_headers(f)
            if headers is None:
                return None
            if headers.get("Result", "*") not in _DECISIVE_RESULTS:
                continue
            seen += 1
            if seen == game_n:
                f.seek(offset)
                game = chess.pgn.read_game(f)
                if game is None:
                    return None
                board = game.board()
                last_move_uci: str | None = None
                for move in game.mainline_moves():
                    last_move_uci = move.uci()
                    board.push(move)
                return {
                    "pgn": str(game),
                    "final_fen": board.fen(),
                    "last_move": last_move_uci,
                    "engine_white": game.headers.get("White", ""),
                    "engine_black": game.headers.get("Black", ""),
                    "result": game.headers.get("Result", "*"),
                    "termination": game.headers.get("Termination", ""),
                }


def _needs_rewrite(pgn_path: Path) -> bool:
    """Header-only scan: True if the PGN has partial pairs or resume dups."""
    seen: set[tuple[str, str, str]] = set()
    pair_counts: dict[tuple[str, frozenset[str]], int] = {}
    cur: dict[str, str] = {}
    in_tags = False

    def consume() -> bool:
        """Returns True if a duplicate is detected (caller should bail early)."""
        if not cur:
            return False
        result = cur.get("Result", "*")
        if result == _WHITE_WIN or result == _BLACK_WIN or result in _DRAW_VALUES:
            white = cur.get("White", "?")
            black = cur.get("Black", "?")
            round_tag = cur.get("Round", "")
            if round_tag and round_tag != "?":
                key = (round_tag, white, black)
                if key in seen:
                    return True  # duplicate
                seen.add(key)
                pkey = (round_tag, frozenset((white, black)))
                pair_counts[pkey] = pair_counts.get(pkey, 0) + 1
        cur.clear()
        return False

    with pgn_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _TAG_RE.match(line)
            if m is not None:
                in_tags = True
                name = m.group(1)
                if name in _WANTED_TAGS:
                    cur[name] = m.group(2)
            elif in_tags:
                if consume():
                    return True
                in_tags = False
        if consume():
            return True

    return any(n == 1 for n in pair_counts.values())


def rewrite_drop_partial_pairs(
    pgn_path: Path,
    config_path: Path | None = None,
    ts: datetime | None = None,
) -> tuple[int, dict[str, dict[str, int]]]:
    """Drop games belonging to partial pairs (and resume duplicates).

    Two distinct failure modes are handled here:

    1. Partial pairs: Stop killed fastchess after game-1 of a pair but
       before game-2 was written. The lone game is dropped from the PGN
       and subtracted from config.json (via ``patch_config_json``) so
       fastchess replays the full pair on resume.

    2. Resume duplicates: fastchess wrote a game to the PGN but was
       killed before updating config.json. On resume it replays the
       round, producing a second PGN entry. The config patch does NOT
       cover this case (config already under-counts; we must not subtract
       further). Last-wins dedup in this function removes the stale copy;
       no config change is needed.

    If ``config_path`` is given and a rewrite occurs, ``patch_config_json``
    is called in the same thread to keep fastchess's resume counter in sync
    for case 1 only.

    Returns ``(dropped_count, deltas)`` where ``deltas`` maps each
    fastchess pair key (``"White vs Black"``) to a dict of
    ``{"wins": N, "losses": N, "draws": N}`` tallied from the dropped
    games.  If ``dropped_count == 0`` the file is untouched, no backup is
    written, and ``deltas`` is empty.
    """
    if not pgn_path.exists():
        return 0, {}
    # Fast pre-check via header-only scan (~50x faster than chess.pgn).
    # Skip the expensive full-parse below if the file is already clean.
    if not _needs_rewrite(pgn_path):
        return 0, {}

    # Split the PGN into per-game text blocks via line scan -- avoids
    # chess.pgn's full move-tree parse (which dominates rewrite time
    # on multi-MB files). Each block contains its own tags + move
    # section, ready to write back verbatim.
    text = pgn_path.read_text(encoding="utf-8", errors="replace")
    blocks: list[str] = []
    current: list[str] = []
    in_tags = False
    for line in text.splitlines(keepends=True):
        # Use the strict tag pattern, not a leading-`[` test -- some PGN
        # writers emit move-line annotations like `[%clk ...]` that would
        # otherwise be misclassified as a new game's first tag.
        if _TAG_RE.match(line):
            if not in_tags and current:
                blocks.append("".join(current))
                current = []
            in_tags = True
            current.append(line)
        else:
            in_tags = False
            current.append(line)
    if current:
        blocks.append("".join(current))

    games: list[tuple[str, str, str, str, str]] = []
    for block in blocks:
        headers = dict(_TAG_RE_BLOCK.findall(block))
        result = headers.get("Result", "*")
        if result not in (_WHITE_WIN, _BLACK_WIN, *_DRAW_VALUES):
            continue
        games.append((
            headers.get("Round", ""),
            headers.get("White", "?"),
            headers.get("Black", "?"),
            result,
            block,
        ))

    # Dedup pass: for each (round, white, black), keep only the last index.
    last_idx: dict[tuple[str, str, str], int] = {}
    for i, (rd, w, b, _r, _bl) in enumerate(games):
        if rd and rd != "?":
            last_idx[(rd, w, b)] = i

    keep: set[int] = set()
    for i, (rd, w, b, _r, _bl) in enumerate(games):
        if not rd or rd == "?":
            keep.add(i)
        elif last_idx[(rd, w, b)] == i:
            keep.add(i)

    pair_counts: dict[tuple[str, frozenset[str]], int] = {}
    for i in keep:
        rd, w, b, _r, _bl = games[i]
        if not rd or rd == "?":
            continue
        key = (rd, frozenset((w, b)))
        pair_counts[key] = pair_counts.get(key, 0) + 1

    final_keep: set[int] = set()
    for i in keep:
        rd, w, b, _r, _bl = games[i]
        if rd and rd != "?":
            key = (rd, frozenset((w, b)))
            if pair_counts[key] == 1:
                continue
        final_keep.add(i)

    dropped = len(games) - len(final_keep)
    if dropped == 0:
        return 0, {}

    # Tally W/L/D for each dropped game, keyed by fastchess pair key
    # ("White vs Black") so the caller can patch config.json stats.
    deltas: dict[str, dict[str, int]] = {}
    for i in range(len(games)):
        if i not in final_keep:
            _rd, w, b, result, _bl = games[i]
            key = f"{w} vs {b}"
            entry = deltas.setdefault(key, {"wins": 0, "losses": 0, "draws": 0})
            if result == _WHITE_WIN:
                entry["wins"] += 1
            elif result == _BLACK_WIN:
                entry["losses"] += 1
            else:
                entry["draws"] += 1

    ts = ts or datetime.now()
    stamp = ts.strftime("%Y-%m-%dT%H-%M-%S")
    backup = pgn_path.with_name(pgn_path.name + f".{stamp}.bak.gz")
    with pgn_path.open("rb") as src, gzip.open(backup, "wb") as dst:
        shutil.copyfileobj(src, dst)
    tmp = pgn_path.with_suffix(pgn_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        for i in range(len(games)):
            if i in final_keep:
                block = games[i][4]
                f.write(block)
                if not block.endswith("\n\n"):
                    f.write("\n" if block.endswith("\n") else "\n\n")
    os.replace(tmp, pgn_path)
    _iter_games_cache.pop(pgn_path, None)
    if config_path is not None:
        patch_config_json(config_path, deltas, ts=ts)
    return dropped, deltas


def patch_config_json(
    config_path: Path,
    deltas: dict[str, dict[str, int]],
    ts: datetime | None = None,
) -> None:
    """Subtract dropped-game W/L/D from fastchess config.json stats.

    ``deltas`` maps ``"White vs Black"`` pair keys to
    ``{"wins": N, "losses": N, "draws": N}`` tallied from games that
    were removed by ``rewrite_drop_partial_pairs``. Pentanomial counters
    (``penta_*``) are *not* touched. fastchess only writes a pentanomial
    entry when a pair's *second* game completes, so partial pairs (the
    only thing this rewrite drops) never made it into the pentanomial
    in the first place -- there is nothing to subtract. Zeroing them
    was tried (commit b221385) and wiped fastchess's running pentanomial
    on every Pause/Resume, which broke its internal SPRT auto-stop.
    The workspace banner LLR is recomputed from the PGN and remains
    authoritative regardless.

    Written atomically; the original is compressed to a timestamped ``.bak.gz``.
    No-op if ``config_path`` does not exist or ``deltas`` is empty.
    """
    if not deltas or not config_path.exists():
        return
    data = json.loads(config_path.read_text(encoding="utf-8"))
    stats = data.get("stats", {})
    changed = False
    for pair_key, delta in deltas.items():
        # fastchess serializes stats under whichever engine was "first" in
        # its PlayerPairKey -- typically the engine listed first in the
        # command. If the dropped game had the engines in the opposite order
        # (White=engine2), our delta key is reversed vs the stored key.
        # Try the reversed key and swap wins<->losses to match perspective.
        w, _sep, b = pair_key.partition(" vs ")
        rev_key = f"{b} vs {w}"
        if pair_key in stats:
            d = delta
            key = pair_key
        elif rev_key in stats:
            d = {"wins": delta["losses"], "losses": delta["wins"], "draws": delta["draws"]}
            key = rev_key
        else:
            log.warning("patch_config_json: pair key %r not found in stats", pair_key)
            continue
        entry = stats[key]
        entry["wins"] = max(0, entry.get("wins", 0) - d["wins"])
        entry["losses"] = max(0, entry.get("losses", 0) - d["losses"])
        entry["draws"] = max(0, entry.get("draws", 0) - d["draws"])
        changed = True
    if not changed:
        return
    stamp = (ts or datetime.now()).strftime("%Y-%m-%dT%H-%M-%S")
    backup = config_path.with_name(config_path.name + f".{stamp}.bak.gz")
    with config_path.open("rb") as src, gzip.open(backup, "wb") as dst:
        shutil.copyfileobj(src, dst)
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=4), encoding="utf-8")
    os.replace(tmp, config_path)


def count_partial_pairs(pgn_path: Path) -> int:
    """Number of (round, engine pair) instances missing one color-flipped game.

    A complete pair has both `(round, A vs B)` and `(round, B vs A)` in
    the PGN. A partial pair has one of the two; this typically results
    from an interrupted Stop on Windows (KILL_ON_JOB_CLOSE has no grace
    period) where game 1 made it to disk but game 2 was in flight.

    Counted post-dedup, so resume duplicates do not inflate the count.
    Games with no Round tag are skipped (no key to pair on).
    """
    counts: dict[tuple[str, frozenset[str]], int] = {}
    for round_, white, black, _result in _iter_games_keyed(pgn_path):
        if not round_ or round_ == "?":
            continue
        key = (round_, frozenset((white, black)))
        counts[key] = counts.get(key, 0) + 1
    return sum(1 for n in counts.values() if n == 1)


def compute_games_list(pgn_path: Path) -> list[dict]:
    """Return one dict per completed game in PGN order.

    Used by the workspace's Schedule window when no live event stream
    is available. Phase 1 has no proxy broadcast, so this PGN-driven
    list is the only source of "what games has fastchess finished."
    """
    return [
        {"white": w, "black": b, "result": r}
        for (w, b, r) in _iter_games(pgn_path)
    ]


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

    def rec(name: str) -> EngineRecord:
        if name not in records:
            records[name] = EngineRecord(name=name)
        return records[name]

    for white, black, result in _iter_games(pgn_path):
        games += 1
        w = rec(white)
        b = rec(black)
        if result == _WHITE_WIN:
            w.wins += 1
            b.losses += 1
            wld.setdefault(white, {}).setdefault(black, [0, 0, 0])[0] += 1
            wld.setdefault(black, {}).setdefault(white, [0, 0, 0])[1] += 1
        elif result == _BLACK_WIN:
            b.wins += 1
            w.losses += 1
            wld.setdefault(black, {}).setdefault(white, [0, 0, 0])[0] += 1
            wld.setdefault(white, {}).setdefault(black, [0, 0, 0])[1] += 1
        else:  # draw
            w.draws += 1
            b.draws += 1
            wld.setdefault(white, {}).setdefault(black, [0, 0, 0])[2] += 1
            wld.setdefault(black, {}).setdefault(white, [0, 0, 0])[2] += 1

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
    """Group games into color-flipped paired matches keyed by Round tag.

    fastchess assigns the same Round to a pair's two color-flipped games
    (A-vs-B and B-vs-A on the same opening). Under ``-concurrency > 1``
    pair-mates are written in completion order, so they are not adjacent
    in the PGN -- grouping by consecutive index would mispair them.
    Grouping by Round is order-independent.

    A round qualifies if it contains exactly two games, both involving
    the {A, B} engine set, with opposite colors. Partial pairs (one
    game), pre-resume duplicates, and rounds whose two games don't
    color-flip are dropped.

    ``engine_a`` is the candidate (the "new" engine being tested) and
    ``engine_b`` is the baseline. Both are taken from the tournament
    config in creation order, not inferred from PGN file order --
    under concurrency the first-completed game may be from any round.

    Returns ``[(engine_a, engine_b, a_score_in_pair), ...]`` in
    first-seen Round order.
    """
    keyed = list(_iter_games_keyed(pgn_path))
    if not keyed:
        return []
    a_name, b_name = engine_a, engine_b
    engines = {a_name, b_name}

    # Group by Round, preserving first-seen order. Games with no Round
    # tag are skipped: there's no key to pair on.
    round_order: list[str] = []
    by_round: dict[str, list[tuple[str, str, str]]] = {}
    for round_tag, white, black, result in keyed:
        if not round_tag or round_tag == "?":
            log.debug(
                "SPRT %s: game without Round tag skipped (no key to pair on)",
                pgn_path.name,
            )
            continue
        if round_tag not in by_round:
            by_round[round_tag] = []
            round_order.append(round_tag)
        by_round[round_tag].append((white, black, result))

    pairs: list[tuple[str, str, float]] = []
    for round_tag in round_order:
        games = by_round[round_tag]
        if len(games) != 2:
            log.debug(
                "SPRT %s: round %s has %d game(s), expected 2 -- skipped",
                pgn_path.name, round_tag, len(games),
            )
            continue
        (w1, b1, _r1), (w2, b2, _r2) = games
        if {w1, b1} != engines or {w2, b2} != engines:
            log.warning(
                "SPRT %s: round %s skipped "
                "(engines %s vs %s / %s vs %s, expected %s vs %s)",
                pgn_path.name, round_tag, w1, b1, w2, b2, a_name, b_name,
            )
            continue
        if w1 == w2:
            # Unreachable from real PGN input: _iter_games_uncached dedups
            # on (round, white, black), so same-color games in one round
            # collapse to one entry (caught by the len != 2 check above).
            # Kept as a guard against future dedup-key changes.
            log.debug(
                "SPRT %s: round %s skipped (both games same color)",
                pgn_path.name, round_tag,
            )
            continue
        score = 0.0
        for white, black, result in games:
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
            status="continue",
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
            status="continue",
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
            status="continue",
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
        status = "H1"
    elif llr <= lower:
        status = "H0"
    else:
        status = "continue"

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
            status="continue", pairs=0,
            elo0=elo0, elo1=elo1, model="logistic",
        )

    a_name, b_name = engine_a, engine_b
    w = l = d = 0
    for white, black, result in games:
        if {white, black} != {a_name, b_name}:
            log.warning(
                "SPRT %s: game skipped (engines %s vs %s, expected %s vs %s)",
                pgn_path.name, white, black, a_name, b_name,
            )
            continue
        if result == _WHITE_WIN:
            if white == a_name:
                w += 1
            else:
                l += 1
        elif result == _BLACK_WIN:
            if black == a_name:
                w += 1
            else:
                l += 1
        else:
            d += 1

    n = w + l + d
    if n == 0:
        return SprtResult(
            llr=0.0, lower_bound=lower, upper_bound=upper,
            status="continue", pairs=0,
            elo0=elo0, elo1=elo1, model="logistic",
        )

    d_obs = d / n
    s0 = 1.0 / (1.0 + math.pow(10.0, -elo0 / 400.0))
    s1 = 1.0 / (1.0 + math.pow(10.0, -elo1 / 400.0))
    pw0, pl0 = s0 - d_obs / 2.0, 1.0 - s0 - d_obs / 2.0
    pw1, pl1 = s1 - d_obs / 2.0, 1.0 - s1 - d_obs / 2.0
    if min(pw0, pl0, pw1, pl1) <= 0.0:
        return SprtResult(
            llr=0.0, lower_bound=lower, upper_bound=upper,
            status="continue", pairs=n,
            elo0=elo0, elo1=elo1, model="logistic",
        )

    llr = w * math.log(pw1 / pw0) + l * math.log(pl1 / pl0)
    if llr >= upper:
        status = "H1"
    elif llr <= lower:
        status = "H0"
    else:
        status = "continue"

    return SprtResult(
        llr=llr, lower_bound=lower, upper_bound=upper,
        status=status, pairs=n,
        elo0=elo0, elo1=elo1, model="logistic",
    )
