"""PGN-based standings, Elo, and SPRT computation.

The runner's stdout summary is **not** the source of truth — these
functions parse ``games.pgn`` directly. A ``Stop`` mid-run preserves
prior games' contribution to standings, and a future Resume that
appends to the same PGN yields correct cumulative numbers without
special handling.

Pure functions; no I/O beyond reading the PGN.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from pathlib import Path

import chess.pgn


# Result tags we recognize. Anything else (`*`, missing, malformed) is
# treated as "no result" and the game is skipped from tallies.
_WHITE_WIN = "1-0"
_BLACK_WIN = "0-1"
_DRAW_VALUES = frozenset({"1/2-1/2", "½-½"})


@dataclass
class EngineRecord:
    name: str
    wins: int = 0
    losses: int = 0
    draws: int = 0

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
            "elo": elo_from_score(self.score_pct) if self.games else None,
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
    if not pgn_path.exists():
        return
    # Two-pass to dedup: first collect all (key_or_None, value), then yield
    # in encounter order with the *last* value for each non-None key.
    entries: list[tuple[tuple[str, str, str] | None, tuple[str, str, str]]] = []
    with pgn_path.open("r", encoding="utf-8", errors="replace") as f:
        while True:
            game = chess.pgn.read_game(f)
            if game is None:
                break
            result = game.headers.get("Result", "*")
            if not (result == _WHITE_WIN or result == _BLACK_WIN or result in _DRAW_VALUES):
                continue
            white = game.headers.get("White", "?")
            black = game.headers.get("Black", "?")
            round_tag = game.headers.get("Round", "")
            value = (white, black, result)
            # python-chess fills missing Seven-Tag Roster headers with "?";
            # treat that as "no round" so dedup only fires on real round tags.
            has_round = round_tag and round_tag != "?"
            key = (round_tag, white, black) if has_round else None
            entries.append((key, value))
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

    return Standings(engines=list(records.values()), games=games)


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
        # All pairs identical (e.g. all 1.0). Still compute a meaningful
        # LLR by giving a tiny floor; otherwise division blows up.
        var = 1e-12
    sigma = math.sqrt(var)

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


def parse_pgn_string(s: str) -> list[tuple[str, str, str]]:
    """Test helper: parse PGN from a string, return ``(white, black, result)`` list."""
    out: list[tuple[str, str, str]] = []
    f = io.StringIO(s)
    while True:
        game = chess.pgn.read_game(f)
        if game is None:
            return out
        result = game.headers.get("Result", "*")
        if result in (_WHITE_WIN, _BLACK_WIN, *_DRAW_VALUES):
            out.append((
                game.headers.get("White", "?"),
                game.headers.get("Black", "?"),
                result,
            ))
