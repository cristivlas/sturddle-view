#!/usr/bin/env python3
"""Plot Elo progression across two or more tournaments, game by game.

Reads each tournament's ``games.pgn`` directly and refits the Elo at every
Nth game, producing an HTML table of the resulting curves. Safe to run
against a live tournament -- the PGN is append-only and only read here.

Comparing tournaments this way assumes they ran under similar conditions
(same engines, time control, and opening book consumed sequentially), so
that game index N used the same opening in every tournament.

Usage:
  python scripts/elo_curve.py --list
  python scripts/elo_curve.py -t "Book A" -t "Book B" --step 10 -o elo.html
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "server"))

from sturddle_view.tournament.pgn_stats import (  # noqa: E402
    _iter_games_uncached,
    elo_from_score,
    elo_margin_from_wld,
    ordo_fit,
)
from sturddle_view.tournament.store import (  # noqa: E402
    TournamentStore,
    default_root,
)
from sturddle_view.chess.results import (  # noqa: E402
    BLACK_WIN,
    WHITE_WIN,
)

# Reference modes for --ref. `ordo` is the mean-centered joint fit (what
# Studio shows); `anchored` shifts it onto the absolute scale using the
# engine ratings frozen in state.json; anything else is read as an engine
# name to pin at zero.
REF_ORDO = "ordo"
REF_ANCHORED = "anchored"

DEFAULT_STEP = int(os.environ.get("SV_ELO_CURVE_STEP", "10"))
DEFAULT_OUTPUT = "elo-curve.html"

# state.json / engine-ref keys.
_KEY_NAME = "name"
_KEY_RATING = "rating"

_PGN_NAME = "games.pgn"
_STATE_NAME = "state.json"


class CurveError(Exception):
    """User-facing failure (bad tournament name, unreadable dir)."""


@dataclass
class Checkpoint:
    """One refit: Elo per engine after ``games`` games of a tournament."""
    games: int
    elo: dict[str, float | None]
    margin: dict[str, float | None]


@dataclass
class Series:
    """A tournament's full set of checkpoints plus its engine roster."""
    name: str
    pgn_path: Path
    engines: list[str]
    checkpoints: list[Checkpoint]
    ratings: dict[str, int]


def _iter_games_live(pgn_path: Path):
    """Yield ``(white, black, result)`` for each decisive game in file order.

    Deliberately not ``pgn_stats._iter_games``: that memoizes the whole game
    list keyed by (mtime, size), which is wasted work for a single sequential
    pass and would pin a multi-MB tuple for the life of the process.
    """
    for _round, white, black, result in _iter_games_uncached(pgn_path):
        yield white, black, result


class _Tally:
    """Running W/L/D and encounter counts over a PGN prefix.

    Feeding games in one at a time keeps every checkpoint O(1) to update;
    only the fit itself is recomputed per checkpoint.
    """

    def __init__(self) -> None:
        self.order: list[str] = []
        self.wins: dict[str, int] = {}
        self.losses: dict[str, int] = {}
        self.draws: dict[str, int] = {}
        # (white, black) -> [white_score, played], the shape ordo_fit wants.
        self.encounters: dict[tuple[str, str], list[float]] = {}
        # Head-to-head W/L/D keyed by (engine, opponent), for the 2-engine
        # closed form and for pinning a curve to a named reference engine.
        self.h2h: dict[tuple[str, str], list[int]] = {}
        self.games = 0

    def _seen(self, name: str) -> None:
        if name not in self.wins:
            self.order.append(name)
            self.wins[name] = 0
            self.losses[name] = 0
            self.draws[name] = 0

    def add(self, white: str, black: str, result: str) -> None:
        self._seen(white)
        self._seen(black)
        self.games += 1
        enc = self.encounters.setdefault((white, black), [0.0, 0])
        enc[1] += 1
        wb = self.h2h.setdefault((white, black), [0, 0, 0])
        bw = self.h2h.setdefault((black, white), [0, 0, 0])
        if result == WHITE_WIN:
            self.wins[white] += 1
            self.losses[black] += 1
            enc[0] += 1.0
            wb[0] += 1
            bw[1] += 1
        elif result == BLACK_WIN:
            self.wins[black] += 1
            self.losses[white] += 1
            wb[1] += 1
            bw[0] += 1
        else:
            self.draws[white] += 1
            self.draws[black] += 1
            enc[0] += 0.5
            wb[2] += 1
            bw[2] += 1

    def encounter_list(self) -> list[tuple[str, str, float, int]]:
        return [(w, b, ws, n) for (w, b), (ws, n) in self.encounters.items()]

    def head_to_head(self, engine: str, opponent: str) -> list[int]:
        """Combined W/L/D for ``engine`` vs ``opponent`` across both colors."""
        a = self.h2h.get((engine, opponent), [0, 0, 0])
        b = self.h2h.get((opponent, engine), [0, 0, 0])
        return [a[0] + b[1], a[1] + b[0], a[2] + b[2]]


def _fit_vs_reference(tally: _Tally, ref_engine: str) -> Checkpoint:
    """Head-to-head Elo of every engine against ``ref_engine``.

    The reference sits at exactly 0 by construction, so curves from
    different tournaments share one scale as long as they share the
    reference engine.
    """
    elo: dict[str, float | None] = {}
    margin: dict[str, float | None] = {}
    for name in tally.order:
        if name == ref_engine:
            elo[name] = 0.0
            margin[name] = 0.0
            continue
        w, l, d = tally.head_to_head(name, ref_engine)
        n = w + l + d
        if n == 0:
            elo[name] = None
            margin[name] = None
            continue
        elo[name] = elo_from_score((w + 0.5 * d) / n)
        margin[name] = elo_margin_from_wld(w, l, d)
    return Checkpoint(games=tally.games, elo=elo, margin=margin)


def _fit_joint(tally: _Tally, ratings: dict[str, int], anchored: bool) -> Checkpoint:
    """Mean-centered ordo fit, optionally shifted onto the absolute scale.

    The anchor offset mirrors ``compute_standings``: the mean gap between
    known ratings and fitted values, applied to every fitted engine. Unlike
    the server we do not require a single connected component -- an early
    prefix of a gauntlet is often still disconnected, and refusing to anchor
    there would blank the start of every curve.
    """
    names = list(tally.order)
    if len(names) < 2:
        return Checkpoint(games=tally.games, elo={}, margin={})
    fit = ordo_fit(
        names,
        tally.encounter_list(),
        wins=tally.wins,
        losses=tally.losses,
    )
    elo: dict[str, float | None] = {}
    margin: dict[str, float | None] = {}
    for name in names:
        e, m = fit.get(name, (None, None))
        elo[name] = e
        margin[name] = m
    if anchored and ratings:
        anchors = [n for n in names if elo.get(n) is not None and n in ratings]
        if anchors:
            offset = (
                sum(ratings[n] for n in anchors)
                - sum(elo[n] for n in anchors)
            ) / len(anchors)
            for name in names:
                if elo[name] is not None:
                    elo[name] += offset
    return Checkpoint(games=tally.games, elo=elo, margin=margin)


def _two_engine_checkpoint(tally: _Tally) -> Checkpoint | None:
    """Closed-form head-to-head Elo when exactly two engines have played.

    Matches ``compute_standings``' 2-engine branch and skips the iterative
    fit entirely, which is what makes long two-engine curves cheap.
    """
    if len(tally.order) != 2:
        return None
    elo: dict[str, float | None] = {}
    margin: dict[str, float | None] = {}
    for name in tally.order:
        w, l, d = tally.wins[name], tally.losses[name], tally.draws[name]
        n = w + l + d
        elo[name] = elo_from_score((w + 0.5 * d) / n) if n else None
        margin[name] = elo_margin_from_wld(w, l, d)
    return Checkpoint(games=tally.games, elo=elo, margin=margin)


def build_series(
    name: str,
    pgn_path: Path,
    ratings: dict[str, int],
    *,
    step: int,
    ref: str,
) -> Series:
    """Walk a PGN once, refitting Elo every ``step`` games."""
    tally = _Tally()
    checkpoints: list[Checkpoint] = []

    def snapshot() -> Checkpoint:
        if ref == REF_ORDO or ref == REF_ANCHORED:
            two = _two_engine_checkpoint(tally) if ref == REF_ORDO else None
            if two is not None:
                return two
            return _fit_joint(tally, ratings, anchored=(ref == REF_ANCHORED))
        return _fit_vs_reference(tally, ref)

    for white, black, result in _iter_games_live(pgn_path):
        tally.add(white, black, result)
        if tally.games % step == 0:
            checkpoints.append(snapshot())

    # Always end on the true final position, even mid-step. Without this a
    # live tournament's curve would stop up to step-1 games behind.
    if tally.games and (not checkpoints or checkpoints[-1].games != tally.games):
        checkpoints.append(snapshot())

    return Series(
        name=name,
        pgn_path=pgn_path,
        engines=list(tally.order),
        checkpoints=checkpoints,
        ratings=ratings,
    )


def _engine_refs(state_path: Path) -> list[dict]:
    """Frozen engine refs from a tournament's state.json, or [] if unreadable.

    These are the snapshot taken at creation time rather than the live
    registry: this runs offline, and the snapshot is what the tournament
    actually ran under.
    """
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [ref for ref in payload.get("engines") or [] if isinstance(ref, dict)]


def _ratings_from_state(state_path: Path) -> dict[str, int]:
    """Engine name -> frozen approximate rating, for the anchored fit."""
    out: dict[str, int] = {}
    for ref in _engine_refs(state_path):
        name = ref.get(_KEY_NAME)
        rating = ref.get(_KEY_RATING)
        if name and rating is not None:
            try:
                out[name] = int(rating)
            except (TypeError, ValueError):
                continue
    return out


def _count_games(pgn_path: Path) -> int:
    return sum(1 for _ in _iter_games_live(pgn_path))


def resolve_tournament(store: TournamentStore, spec: str) -> tuple[str, Path, Path]:
    """Resolve a CLI ``--tourney`` argument to (name, pgn_path, state_path).

    A path to a tournament directory is taken literally; anything else is
    matched against tournament names -- exact first, then case-insensitive
    substring. An ambiguous substring is an error rather than a guess.
    """
    as_path = Path(spec)
    if as_path.is_dir():
        return as_path.name, as_path / _PGN_NAME, as_path / _STATE_NAME

    tours = store.list()
    exact = [t for t in tours if t.name == spec]
    matches = exact or [t for t in tours if spec.lower() in t.name.lower()]
    if not matches:
        raise CurveError(
            f"no tournament matching {spec!r} under {store.root} "
            f"(use --list to see available names)"
        )
    if len(matches) > 1:
        names = ", ".join(repr(t.name) for t in matches)
        raise CurveError(f"{spec!r} is ambiguous: matches {names}")
    t = matches[0]
    d = store.dir_for(t.id)
    return t.name, d / _PGN_NAME, d / _STATE_NAME


def list_tournaments(store: TournamentStore) -> int:
    tours = store.list()
    if not tours:
        print(f"no tournaments under {store.root}")
        return 1
    print(f"{'games':>7}  {'status':<9}  name")
    for t in tours:
        pgn = store.dir_for(t.id) / _PGN_NAME
        games = _count_games(pgn) if pgn.exists() else 0
        print(f"{games:>7}  {t.status:<9}  {t.name}")
    return 0


def _validate_ref(ref: str, state_paths: list[Path]) -> None:
    """Reject a --ref engine name that no tournament declares.

    Checked against state.json rather than the PGN so the failure is
    instant; a declared engine with no games yet still passes, and its
    curve simply starts once it has played.
    """
    if ref in (REF_ORDO, REF_ANCHORED):
        return
    known: set[str] = set()
    for p in state_paths:
        known.update(
            ref_dict[_KEY_NAME] for ref_dict in _engine_refs(p) if ref_dict.get(_KEY_NAME)
        )
    if ref in known:
        return
    have = ", ".join(sorted(known)) if known else "none declared"
    raise CurveError(
        f"--ref {ref!r} is not an engine in these tournaments (have: {have})"
    )


def _fmt_cell(elo: float | None, margin: float | None) -> str:
    if elo is None:
        return "&mdash;"
    if margin is None:
        return f"{elo:+.1f}"
    return f"{elo:+.1f} <span class='pm'>&plusmn;{margin:.1f}</span>"


def _shown_engines(s: Series, ref: str) -> list[str]:
    """Engines to render for a series.

    In reference mode the reference engine is pinned at exactly 0 for every
    checkpoint, so its column carries no information and is dropped -- with a
    shared baseline that would otherwise be one constant-zero column per
    tournament. The legend names the reference so the scale stays clear.
    """
    if ref in (REF_ORDO, REF_ANCHORED):
        return s.engines
    return [e for e in s.engines if e != ref]


def _series_payload(series: list[Series], ref: str) -> list[dict]:
    """Checkpoint data as plain JSON, embedded in the page for charting."""
    return [
        {
            "tournament": s.name,
            "engines": shown,
            "points": [
                {
                    "games": c.games,
                    "elo": {e: c.elo.get(e) for e in shown},
                    "margin": {e: c.margin.get(e) for e in shown},
                }
                for c in s.checkpoints
            ],
        }
        for s, shown in ((s, _shown_engines(s, ref)) for s in series)
    ]


def _json_for_script(payload) -> str:
    """Serialize for embedding in a ``<script>`` block.

    An engine name containing ``</script>`` would otherwise close the element
    early and spill the rest of the data into the document as markup. Escaping
    ``</`` prevents that; it stays valid JSON, since ``\\/`` is a legal escape.
    """
    return json.dumps(payload).replace("</", "<\\/")


_HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Elo progression</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #ffffff; --surface: #f7f7f9; --text: #1c1d21; --muted: #6b6d76;
    --border: #dcdde2; --accent: #4a5bd4;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #17181d; --surface: #1f2027; --text: #e6e7ec; --muted: #9a9ca5;
      --border: #32343d; --accent: #8b97ee;
    }}
  }}
  body {{
    margin: 0; padding: 24px; background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  h1 {{ font-size: 19px; margin: 0 0 4px; }}
  .meta {{ color: var(--muted); font-size: 12px; margin-bottom: 20px; }}
  .meta code {{ font-size: 11px; }}
  .scroll {{ overflow-x: auto; border: 1px solid var(--border); border-radius: 8px; }}
  table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
  th, td {{ padding: 6px 12px; text-align: right; white-space: nowrap; }}
  thead th {{
    background: var(--surface); border-bottom: 1px solid var(--border);
    font-weight: 600; font-size: 12px; position: sticky; top: 0;
  }}
  thead tr.groups th {{ text-align: center; border-left: 1px solid var(--border); }}
  tbody td {{ border-top: 1px solid var(--border); }}
  tbody td.games {{ text-align: left; color: var(--muted); }}
  td.grp {{ border-left: 1px solid var(--border); }}
  .pm {{ color: var(--muted); font-size: 11px; }}
  .legend {{ margin-top: 18px; color: var(--muted); font-size: 12px; }}
</style>
</head>
<body>
<h1>Elo progression</h1>
<div class="meta">{meta}</div>
"""

_HTML_TAIL = """<div class="legend">{legend}</div>
<script id="elo-curve-data" type="application/json">{data}</script>
</body>
</html>
"""


def _render_header(series: list[Series], ref: str) -> str:
    groups = "".join(
        f'<th class="grp" colspan="{len(_shown_engines(s, ref))}">'
        f"{html.escape(s.name)}</th>"
        for s in series
    )
    cols = "".join(
        f'<th class="{"grp" if i == 0 else ""}">{html.escape(e)}</th>'
        for s in series
        for i, e in enumerate(_shown_engines(s, ref))
    )
    return (
        "<thead>"
        f'<tr class="groups"><th></th>{groups}</tr>'
        f"<tr><th>games</th>{cols}</tr>"
        "</thead>"
    )


def _render_rows(series: list[Series], ref: str) -> str:
    # Rows are keyed by game index so every tournament lines up on the same
    # opening -- the whole point of the comparison. A tournament with fewer
    # games leaves blanks in its columns rather than shifting rows. Columns
    # are per-tournament: a shared baseline engine gets one column per
    # tournament, which is what makes the two curves directly comparable.
    indices = sorted({c.games for s in series for c in s.checkpoints})
    by_index = [{c.games: c for c in s.checkpoints} for s in series]
    shown = [_shown_engines(s, ref) for s in series]
    rows = []
    for n in indices:
        cells = []
        for si, _s in enumerate(series):
            c = by_index[si].get(n)
            for i, e in enumerate(shown[si]):
                cls = "grp" if i == 0 else ""
                text = _fmt_cell(c.elo.get(e), c.margin.get(e)) if c else ""
                cells.append(f'<td class="{cls}">{text}</td>')
        rows.append(f'<tr><td class="games">{n}</td>{"".join(cells)}</tr>')
    return f"<tbody>{''.join(rows)}</tbody>"


def render_html(series: list[Series], *, ref: str, step: int) -> str:
    meta = (
        f"reference: <code>{html.escape(ref)}</code> &middot; every {step} "
        "games &middot; "
        + " &middot; ".join(
            f"{html.escape(s.name)} "
            f"({s.checkpoints[-1].games if s.checkpoints else 0} games)"
            for s in series
        )
    )
    if ref == REF_ORDO:
        legend = (
            "Mean-centered ordo fit: ratings within a tournament are relative "
            "to that tournament's average, so absolute values are not "
            "comparable across tournaments &mdash; only the gaps are."
        )
    elif ref == REF_ANCHORED:
        legend = (
            "Ordo fit shifted onto the absolute scale using the engine ratings "
            "frozen in each tournament's state.json."
        )
    else:
        legend = (
            f"Head-to-head Elo against <b>{html.escape(ref)}</b>, pinned at 0 "
            "and omitted from the table. Curves from different tournaments "
            "share this scale."
        )
    table = (
        '<div class="scroll"><table>'
        + _render_header(series, ref)
        + _render_rows(series, ref)
        + "</table></div>"
    )
    data = _json_for_script(_series_payload(series, ref))
    return (
        _HTML_HEAD.format(meta=meta)
        + table
        + _HTML_TAIL.format(legend=legend, data=data)
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Plot Elo progression across tournaments, game by game.",
    )
    p.add_argument(
        "-t", "--tourney", action="append", default=[], metavar="NAME",
        help="tournament name (or directory path); repeat to compare several",
    )
    p.add_argument(
        "--step", type=int, default=DEFAULT_STEP, metavar="N",
        help=f"refit Elo every N games (default {DEFAULT_STEP})",
    )
    p.add_argument(
        "--ref", default=REF_ORDO, metavar="MODE",
        help=(
            f"{REF_ORDO} (mean-centered, like Studio), {REF_ANCHORED} "
            "(absolute, using frozen engine ratings), or an engine name to "
            f"pin at 0 (default {REF_ORDO})"
        ),
    )
    p.add_argument(
        "-o", "--output", default=DEFAULT_OUTPUT, metavar="PATH",
        help=f"HTML output path (default {DEFAULT_OUTPUT})",
    )
    p.add_argument(
        "--root", type=Path, default=None, metavar="PATH",
        help="tournaments root (default: the app's data directory)",
    )
    p.add_argument(
        "--list", action="store_true",
        help="list available tournaments and exit",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    store = TournamentStore(args.root or default_root())

    if args.list:
        return list_tournaments(store)
    if not args.tourney:
        print("error: no tournament given (use -t NAME, or --list)", file=sys.stderr)
        return 2
    if args.step < 1:
        print("error: --step must be at least 1", file=sys.stderr)
        return 2

    # Resolve every tournament and validate --ref before walking any PGN:
    # the rosters come from state.json, so a bad reference name fails in
    # milliseconds instead of after refitting a multi-thousand-game curve.
    resolved = []
    for spec in args.tourney:
        name, pgn_path, state_path = resolve_tournament(store, spec)
        if not pgn_path.exists():
            raise CurveError(f"{name!r} has no {_PGN_NAME} yet ({pgn_path})")
        resolved.append((name, pgn_path, state_path))
    _validate_ref(args.ref, [state for _n, _p, state in resolved])

    series: list[Series] = []
    for name, pgn_path, state_path in resolved:
        s = build_series(
            name, pgn_path, _ratings_from_state(state_path),
            step=args.step, ref=args.ref,
        )
        if not s.checkpoints:
            raise CurveError(f"{name!r} has no completed games yet")
        series.append(s)
        print(f"{name}: {s.checkpoints[-1].games} games, "
              f"{len(s.checkpoints)} checkpoints, engines: {', '.join(s.engines)}")

    out = Path(args.output)
    out.write_text(render_html(series, ref=args.ref, step=args.step), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CurveError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
