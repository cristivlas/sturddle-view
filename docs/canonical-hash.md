# Canonical Hash for PGN / FEN Imports

## Problem

`recent_imports` hashes the raw imported bytes (`sha256` over the
trimmed text). Identical games coming from different sources --
chess.com vs lichess vs an engine vs a hand re-formatted paste --
hash differently, so the store treats them as distinct entries
and the "same game?" features (Viewing match toast, replace-game
confirm, frozen-window dedup) silently miss.

Goal: two semantically identical games hash equal regardless of
source formatting.

## Constraint

**No data loss.** Verbatim storage of the original import text is
untouched. Canonicalization is for hashing only; the on-disk
blob remains exactly what the user pasted.

## Canonical form -- PGN

1. Parse with `chess.pgn.read_game`.
2. Emit deterministically via `StringExporter` (default settings).
3. Sort headers alphabetically by name before emit (verbatim
   storage preserves source order; only the hash sees the sorted
   form).
4. Keep all headers, all comments, all NAGs, all variations.
5. Post-process: inside every `{...}` comment, collapse any run of
   whitespace (spaces, tabs, newlines, CRLF) to a single space.
   Brackets and `[%cmd args]` extensions survive intact.
6. Hash = `sha256` of the resulting UTF-8 bytes.

### Notes / risks

- `[%clk ...]`, `[%eval ...]`, `[%emt ...]` and other `[%cmd]`
  extensions are preserved by python-chess and untouched by our
  whitespace collapse (the collapse runs *between tokens*, not
  on brackets).
- Multi-line comments collapse to single line for hashing only.
  Display still wraps as originally written (verbatim storage).
- python-chess merges adjacent `{a}{b}` blocks into `{a b}`.
  Acceptable -- the merge is deterministic.
- Comment ordering within a merged block follows source order.
  `{a}{b}` and `{b}{a}` hash differently (correct; annotation
  order is part of identity).

## Canonical form -- FEN

1. Parse via `chess.Board(text)`.
2. Re-emit `board.fen()` -- python-chess fills all six fields
   with their canonical defaults (`- - 0 1`) when missing.
3. Hash = `sha256` of the re-emitted UTF-8 bytes.

No comments / annotations in FEN, no header sort, no whitespace
nuance. Single-line, deterministic.

## Validation

TDD. Each rule above gets a paired test:

- PGN: same game from two sources with different header order
  hashes equal.
- PGN: same game with line-wrapped vs unwrapped comments hashes
  equal.
- PGN: same comment with tabs vs spaces hashes equal.
- PGN: same game with vs without annotations hashes *unequal*
  (NO DATA LOSS contract).
- PGN: same game with reordered annotations hashes unequal.
- FEN: short FEN (`startpos`-like, missing trailing fields)
  hashes equal to its fully-qualified form.
- FEN: extra whitespace around fields hashes equal.
- Round-trip: hashing the same text twice is stable.

## Scope boundary

This work changes *how* the hash is computed, not what the hash
*means* in the store. The store still uses `hash` as the content
key; canonicalization just makes more content collide as "same".

## Status

- [x] PGN canonicalizer + unit tests
      (`server/sturddle_view/play/canonical_hash.py`,
      `server/tests/test_canonical_hash.py`)
- [x] FEN canonicalizer + unit tests (same files)
- [x] Wire into `recent_imports.save` and
      `api/game._hash_import_text` (both call sites converge on
      `canonical_hash`)
- [ ] Wipe store before deploy (existing hashes are stale)
- [x] Full suite green (1013 passed, 33 skipped)
