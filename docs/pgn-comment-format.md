# PGN Comment Format

Status: decided. Output is cutechess-style tokens, one style, never
mixed; bracket-tag emit is rejected (below). This captures the known
leak, why heuristic fixes are fragile, and the band-aids shipped.

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

## Rejected: bracket tags on emit

`[%eval ...]` was considered as the emit format (keyed, bracketed,
stripped by every PGN-aware reader). Rejected: the tag's point of view
is ambiguous across tools -- Lichess writes white-POV float pawns,
ChessBase writes STM-POV `cp,depth` -- so a reader can't know which
one it is looking at. A custom key (`[%sv ...]`) is non-standard and
confuses other readers. Output stays cutechess-style, and never mixes
styles within a file, not even per ply. `[%eval]` is read on import
only.

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
token, not the bare time). The clock reader (`_cutechess_time_seconds`)
reads that same trailing token -- one shared regex -- so a commented ply
keeps its spent time on reload instead of zeroing the reconstructed clocks.

An eval with no depth (a Lichess `[%eval]` import re-serialized after an
annotation or play-from-here; rarely, an engine whose last scored `info`
line has no `depth`) is written as the bare cutechess token, `+0.34` or
`+0.34 1.2s`, STM POV like every other token. The reader accepts exactly
that shape -- signed two-decimal pawns or `[+-]M<n>`, time with unit, at
the end of the comment (`_DEPTHLESS_EVAL_RE`) -- after the `/depth` form.
Known cost: prose that ends in a bare `+0.50` reads as an eval. External
cutechess/fastchess output always carries `/depth`, so only our own files
and such prose are affected.
