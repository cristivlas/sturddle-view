"""PGN-based standings, Elo, and SPRT computation.

``games.pgn`` is the source of truth (not the runner's stdout summary), so
Stop/Resume across the same PGN yields correct cumulative numbers.
Reads the PGN; results are cached per-path keyed by (mtime, size).
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
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


# Cache: pgn_path -> (mtime_ns, size, games_tuple). PGN is append-only,
# so (mtime, size) is a sound invalidation key. One entry per path keeps
# memory bounded; the entry self-replaces on every change.
_iter_games_cache: dict[Path, tuple[int, int, tuple[tuple[str, str, str], ...]]] = {}


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


def _iter_games_uncached(pgn_path: Path):
    # Header-only scan: standings/games/SPRT only need White/Black/Result/Round.
    # Avoids ``chess.pgn.read_game``'s full move-tree parse (>50× slower on
    # multi-MB PGNs). Section boundary = a non-tag line after we've seen at
    # least one tag in the current game; lines before any tag are skipped.
    # NOTE: trade-off — a `;`-comment line between tag block and moves
    # would emit early. fastchess never emits those.
    entries: list[tuple[tuple[str, str, str] | None, tuple[str, str, str]]] = []
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
            value = (white, black, result)
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

    last_value: dict[tuple[str, str, str], tuple[str, str, str]] = {
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
                return str(game) if game is not None else None


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


def compute_standings(pgn_path: Path) -> Standings:
    """Tally W/L/D per engine across all games in ``games.pgn``.

    The PGN may be empty, missing, or partially-written; results from
    valid games are counted, the rest skipped.
    """
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
        elif result == _BLACK_WIN:
            b.wins += 1
            w.losses += 1
        else:  # draw
            w.draws += 1
            b.draws += 1

    engines = list(records.values())
    # Elo is only meaningful as a head-to-head metric. With exactly two
    # engines, every game is A-vs-B so ``score_pct`` *is* the head-to-head
    # score and we attach Elo + 95% CI. With N≥3, score% is "vs field"
    # (mixed strengths) and is not a real Elo — leave both fields None.
    if len(engines) == 2:
        for e in engines:
            e.elo = elo_from_score(e.score_pct) if e.games else None
            e.elo_margin_95 = elo_margin_from_wld(e.wins, e.losses, e.draws)
    return Standings(engines=engines, games=games)


# ---------------------------------------------------------------------------
# SPRT (Sequential Probability Ratio Test)
#
# We implement the *normalized Elo / pentanomial* model used by
# fastchess as the default. Games are paired (engine A plays both
# colors against engine B for the same opening); each pair scores
# {0, 0.5, 1, 1.5, 2} for engine A. The five-bin distribution yields
# a tighter variance estimate than a binomial W/L/D model.
#
# Reference: fastchess-cli docs and the Bayesian-Elo project notes.
# Math summary:
#
#   For each pair, A's score s_i ∈ {0, 0.5, 1, 1.5, 2}.
#   mean μ = (1/N) Σ s_i / 2          (per-game score in [0,1])
#   variance σ² = (1/(N-1)) Σ (s_i/2 - μ)²
#   per-pair variance is σ²·2 (two games), so we use σ²_pair = (Σ(s_i/2-μ)²) / N
#   We work with normalized Elo: elo_norm = (μ - 0.5) / σ_pair * scale
#   where scale converts to the customary unit (see below).
#
# We test H0: elo = elo0 vs H1: elo = elo1 with a likelihood ratio
# under a normal approximation, yielding LLR. Bounds are
# log(beta/(1-alpha)) and log((1-beta)/alpha).
# ---------------------------------------------------------------------------


# Convert "logistic Elo" parameter to a per-pair score-scale offset under the
# normalized-Elo model. fastchess uses a fixed coefficient: the score
# difference (μ − 0.5) corresponds to elo via 200/ln(10) when normalized by
# the pair-stdev, i.e.
#     elo_normalized = (μ − 0.5) * (800 / ln(10)) / σ_pair
# (200 per game * 2 games per pair / σ_pair, in nats vs base-10).
#
# We don't need to convert elo0/elo1 away from this convention; this is the
# convention they're already specified in.

_PAIR_BINS = (0.0, 0.5, 1.0, 1.5, 2.0)


def _iter_pairs(pgn_path: Path) -> list[tuple[str, str, float]]:
    """Group games into back-to-back paired matches.

    fastchess emits games in pairs where consecutive games share the same
    pairing with reversed colors (game 2k and 2k+1). For SPRT we pair them
    and emit ``(engine_a, engine_b, a_score_in_pair)`` for each completed
    pair. Engine A is the *first* engine seen in the PGN (typical SPRT
    setup: A is "new", B is "base").

    Trailing odd game (incomplete pair) is dropped.
    """
    games = list(_iter_games(pgn_path))
    if not games:
        return []
    if len(games) % 2 == 1:
        log.warning(
            "SPRT %s: dropping trailing odd game (total=%d)",
            pgn_path.name, len(games),
        )
    # Engine A = whichever engine appears first (deterministic).
    first_white, first_black, _ = games[0]
    a_name, b_name = first_white, first_black
    pairs: list[tuple[str, str, float]] = []
    for i in range(0, len(games) - 1, 2):
        score = 0.0
        for game in games[i : i + 2]:
            white, black, result = game
            # Skip pair if it doesn't involve our two engines (shouldn't
            # happen in a 2-engine SPRT, but be defensive).
            if {white, black} != {a_name, b_name}:
                log.warning(
                    "SPRT %s: pair at offset %d skipped "
                    "(engines %s vs %s, expected %s vs %s)",
                    pgn_path.name, i, white, black, a_name, b_name,
                )
                break
            if result == _WHITE_WIN:
                score += 1.0 if white == a_name else 0.0
            elif result == _BLACK_WIN:
                score += 1.0 if black == a_name else 0.0
            else:  # draw
                score += 0.5
        else:
            pairs.append((a_name, b_name, score))
    return pairs


def _sprt_bounds(alpha: float, beta: float) -> tuple[float, float]:
    """Wald boundaries for an SPRT at significance ``alpha`` and power ``1-beta``."""
    return math.log(beta / (1.0 - alpha)), math.log((1.0 - beta) / alpha)


def compute_sprt(
    pgn_path: Path,
    params: dict,
) -> SprtResult:
    """Compute SPRT LLR + decision over the games in ``pgn_path``.

    ``params`` keys:
      - ``elo0``  (float, required)
      - ``elo1``  (float, required)
      - ``alpha`` (float, default 0.05)
      - ``beta``  (float, default 0.05)
      - ``model`` (str,  default "normalized" — only model implemented)
    """
    elo0 = float(params["elo0"])
    elo1 = float(params["elo1"])
    alpha = float(params.get("alpha", 0.05))
    beta = float(params.get("beta", 0.05))
    model = params.get("model", "normalized")
    if model != "normalized":
        raise NotImplementedError(f"SPRT model {model!r} not implemented")
    if elo0 >= elo1:
        raise ValueError(f"SPRT requires elo0 < elo1; got elo0={elo0}, elo1={elo1}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"SPRT alpha must be in (0, 1); got {alpha}")
    if not 0.0 < beta < 1.0:
        raise ValueError(f"SPRT beta must be in (0, 1); got {beta}")

    lower, upper = _sprt_bounds(alpha, beta)
    pairs = _iter_pairs(pgn_path)
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

    # Convert elo (logistic, per-game) into per-pair score offset.
    # Per-game score offset for elo Δ: dscore ≈ Δ * ln(10) / 1600.
    # Per-pair: same dscore (we work in per-pair score units for both
    # the observed mean and the hypothesis means).
    def elo_to_score(elo: float) -> float:
        return 0.5 + elo * math.log(10.0) / 1600.0

    s0 = elo_to_score(elo0)
    s1 = elo_to_score(elo1)

    # LLR for two normal hypotheses with common variance:
    #   LLR = n * [(s1 - s0)*(μ - (s0+s1)/2)] / σ²
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
