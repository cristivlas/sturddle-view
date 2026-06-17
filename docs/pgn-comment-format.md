# PGN Comment Format -- Tabled

Status: tabled. Captures a known bug, why heuristic fixes are fragile,
and the rough shape of a proper rewrite. Decisions still to make are
listed at the bottom. Do not implement piecemeal -- this should land
as a designed feature, not a patch.

## The bug

Save PGN can leak machine tokens into the user-facing comment text.
Minimal repro:

    1. e4 c5 { Ok, black plays Sicilian... -0.24/22 } 2. f4 { Good move! 3.2s }

On the `c5` ply the cutechess `<eval>/<depth>` regex matches the
trailing `-0.24/22` and strips it -- user sees only `"Ok, black plays
Sicilian..."`. On the `f4` ply there is no `eval/depth` prefix, so the
trailing `3.2s` is *not* matched by that regex; the bare-time regex
(`_CUTECHESS_TIME_ONLY_RE`) is anchored to require the whole comment
to be a time token, so it also doesn't fire. The `3.2s` leaks into
the user-visible string: `"Good move! 3.2s"`. (Now mitigated on import --
see "Shipped band-aid" below; the emit-side rewrite remains tabled.)

## Why heuristic detection on import is fragile

The honest reason `cutechess-cli`/`fastchess` output is hard to parse
cleanly: machine metadata (eval, depth, elapsed time) and user prose
share the same free-form comment string, with no key, no delimiter,
and a positional convention that varies by tool/config.

We considered several import-side heuristics:

- **Detect "cutechess-shaped" PGN via the `eval/depth` token, then
  aggressively strip trailing bare times.** Works for files where at
  least one ply has an eval/depth token. Fails when no ply does -- a
  cutechess run configured without eval output emits time-only
  comments everywhere, and there's no signal that says "this is
  machine-generated" vs "this is hand-written prose ending in a
  casual `7s` mention."

- **Header fingerprint (`[GameDuration]`, `Annotator: cutechess`,
  etc.).** Increases coverage, still incomplete. Adds magic-string
  reading.

- **Unconditional trailing `\s+\d+(?:\.\d+)?(ms|s)\s*$` strip.**
  Catches all cases but eats user prose like `"took 7s thinking"`.

None of these are a clean fix. The root problem is the input format,
not our parser.

## Why output-side hacks don't help

We also considered emitting a sentinel `eval/depth` slot when we have
none (e.g. `0/0` or `n/a`) so the existing strip regex would always
fire. Rejected:

- Numeric sentinel (`0/0`) is indistinguishable from a real reading
  of zero at depth zero. Destroys information on round-trip.
- Non-numeric sentinel (`n/a`) shows as visible junk in every external
  PGN viewer that doesn't run our sanitizer (Lichess, chess.com,
  scid, ChessBase). Output gets uglier for the median case to fix
  the edge case.
- Either way it's a write-side workaround for an input-side parsing
  problem, and it only helps PGNs *we* emit. External cutechess PGNs
  (the actual usual source of the bug) still leak on import.

## The proper fix: PGN bracket tags on emit

`[%clk H:MM:SS]` and `[%eval ...]` are PGN-spec bracket extensions.
They are keyed, bracketed, unambiguously machine, and stripped
cleanly by every PGN-aware reader. Lichess, chess.com, ChessBase,
python-chess, and our own parser all already handle them.

The plan: *emit* in this format, keep cutechess regex parsing as a
**legacy import-only path** for external PGNs from cutechess/fastchess
that don't know about us.

Sketch:

- `build_pgn` writer: emit `[%clk]` and `[%eval]` via python-chess's
  `node.set_clock()` / `node.set_eval()` helpers instead of the
  current trailing `<eval>/<depth> <time>` string concat.
- `parse_pgn` reader: extend the eval extractor to read `[%eval]`
  bracket tags as primary; keep `_CUTECHESS_EVAL_RE` as a fallback.
  Read `[%clk]` via `node.clock()` and derive elapsed-per-ply from
  successive values; keep `_cutechess_time_seconds` as fallback.
- `_view_original_text` short-circuit unchanged: imported PGNs
  round-trip verbatim until edited (existing behavior preserved).

## Open decisions (must be made before implementation)

1. **`[%eval]` variant.**
   - Lichess: `[%eval 0.24]` -- float-pawns, white-POV, no depth.
   - ChessBase: `[%eval +24,22]` -- cp-comma-depth, STM-POV by spec.
   - Tradeoff: depth survival vs spec-correct POV/format.

2. **POV on the wire.**
   - In-memory: white-POV.
   - Lichess emits white-POV; ChessBase comma-form is STM-POV by
     spec. Mixing white-POV signs into the comma form risks
     misinterpretation by strict parsers.
   - Decision: stick to white-POV, accept the format choice it forces.

3. **Depth handling, if we go Lichess float.**
   - (a) Drop depth on emit. Cheapest, loses information we have.
   - (b) Emit a non-standard `[%depth N]` alongside `[%eval]`. Local
     extension, external readers strip as unknown bracket tag.
     Clean semantics, slight emit overhead.

4. **`[%clk]` precision.**
   - Spec: `H:MM:SS` integer seconds.
   - Lichess accepts `H:MM:SS.s` for sub-second. Today we emit `3.2s`
     (one decimal of precision).
   - Decision: probably `H:MM:SS.s` to preserve current precision.

5. **Test fixture impact.**
   - Input fixtures (external PGNs) are untouched -- the legacy
     reader still parses them.
   - Output assertions in tests that pin the exact cutechess token
     format need rewriting (~16 sites across ~7 files).
   - Self-generated round-trip fixtures: spot-checked, none exist
     today.

6. **Canonical hash stability.**
   - `canonical_hash` normalizes whitespace and sorts headers but
     does NOT collapse `[%key val]` vs cutechess-token differences.
     Self-emitted PGNs from before vs after will hash differently.
   - Decision needed: do we migrate stored hashes, or accept that
     pre-rewrite play games rehash on next read?

## Rough estimate

Half a day's focused work if decisions above are pre-made. 1-2 days
if surprises in `canonical_hash`, the tournament pipeline, or the
view-mode clock reconstruction need real attention.

## Shipped band-aid (import side only)

The bare-time leak in `{ Good move! 3.2s }`-style comments is now stripped
on import by `_TRAILING_MACHINE_TIME_RE` (import_position.py): it removes a
trailing `<decimal>s` / `<integer>ms` machine token while leaving casual
human mentions like `took 7s` intact (humans write whole seconds without a
decimal; the cutechess writer always emits one). This is a band-aid, not
the rewrite -- emit still uses positional cutechess tokens, so a cutechess
run configured to write bare-integer seconds can still leak, and the proper
`[%clk]`/`[%eval]` emit fix above remains tabled. Eval extraction is
unaffected (it reads `[%eval]` bracket tags and the `eval/depth` cutechess
token, not the bare time).
