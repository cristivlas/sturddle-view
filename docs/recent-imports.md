# Recent Imports — Design Sketch

Status: implemented (stable).

## Problem

The import dialog ("Open position") currently remembers the last 5 imported
PGN/FEN entries in `localStorage` under `sturddle:import:recent`, storing the
full text inline. Tournament PGNs with eval/clock annotations are 20-50 KB
each; five entries already use 100-250 KB. Hard cap limits how much history
we can offer, and the data is per-browser-profile, not portable.

We want:
- A larger history (target: 50 entries).
- Storage moved to the server so it survives browser data clears and is
  shared across browsers/profiles on the same install.
- No duplicate blobs when the user re-imports the same PGN.
- Recall must reproduce the original text verbatim so future viewer
  features (comments, eval/clock overlays, NAGs) work on history entries.

## Storage layout

Under `<user_data_dir>/imports/`:

```
imports/
  index.json                # {hash: {format, summary, ts, file}}
  by-hash/
    <sha256>.pgn            # raw text of the imported PGN (or .fen)
```

- `<sha256>` = SHA-256 hex of the canonical form of the text (see
  [canonical-hash.md](canonical-hash.md)); the stored blob stays verbatim.
- Rows have since grown optional fields beyond the original sketch:
  `game_id` (stable identity; see "game_id contracts" below), `refs`
  (children pinning the row), `parent_game_id` / `fork_ply` (fork links;
  see [x-game-navigation.md](x-game-navigation.md)), and
  `summary["source"]="play"` on auto-saved finished play games (see
  [pgn-export.md](pgn-export.md)).
- `format`: "pgn" | "fen". Both formats are stored under the same scheme
  (FENs are tiny but kept here so "Recent" reflects everything the user
  opened, and the dropdown can flag them with a small badge later).
  Drives which parser to use on recall — avoids the import endpoint's
  format-guessing fallback and the chance of mis-classification on
  edge inputs.
- `summary`: human-readable one-liner (current client builds this).
- `ts`: milliseconds since epoch (matches `Date.now()` for client-cache compat).
- `file`: relative path from `imports/` to the blob.

Index is the canonical metadata; blobs are addressed by hash.

## Import flow (single parse, stateless)

To avoid parsing big PGNs twice, drop the as-you-type validate. The
import endpoint becomes the only parse path; recents are saved as a
side effect of a successful import.

1. User pastes / types / drops a file. Import button enabled when text
   is non-empty. No server round-trips on input changes.
2. User clicks Import. Client POSTs `/game/import {format, text}`.
3. Server parses ONCE:
   - Validates the text via `parse_pgn` / `parse_fen`.
   - Enters view mode with the parsed result.
   - Computes `hash` via `canonical_hash` (see [canonical-hash.md](canonical-hash.md)).
   - Writes the blob to `imports/by-hash/<hash>.<ext>` if new.
   - Upserts the index entry `{hash: {format, summary, ts: now, file}}`.
     Summary is the same string the parser already produces.
   - Evicts oldest index entries if over the server cap.
   - Responds `{game_id, viewing, hash, summary}`.
4. On 400, the server's `detail.message` surfaces in the dialog's
   status label. User edits the text and re-clicks. No background
   validation in between.
5. Client updates its localStorage metadata cache with
   `{hash, format, summary, ts}` (no `text`).

Trade-off: we lose the live "Magnus vs Hikaru, 47 plies" hint
underneath the textarea. Acceptable: most users paste once and click;
the rare format-shopping case sees an error after one extra click.
Big PGNs are no longer parsed on every debounce tick.

## Endpoints

All under `/game/recent-imports`, behind the same auth token as other
`/game/*` routes.

- `GET /game/recent-imports`
  Returns index entries sorted by `ts` desc, capped at the server limit.
  Shape: `[{hash, format, summary, ts}, ...]`. No blobs.

- `GET /game/recent-imports/{hash}`
  Returns `{format, text, summary, ts}`. **Side effect: bumps `ts` to
  now** so frequently revisited entries stay at the top and don't fall
  off when eviction runs. (Trade-off: GET-with-side-effect violates
  pure REST semantics; the alternative — separate POST /touch — adds
  ceremony for no practical gain since the only caller is the dialog
  recall. A future "hover preview" feature must use the index, not
  this endpoint, to avoid bumping ts on hover.)

- `DELETE /game/recent-imports/{hash}` *(optional, for future "Clear
  history" UI)*. Removes the index row and the blob.

There is **no standalone `POST /game/recent-imports`** — saving is a
side effect of two endpoints:

- `POST /game/import` — records the imported text (PGN or FEN).
- `POST /game/edit/commit` — records the accepted FEN. A committed
  edit means the user deliberately built a position they may want
  later; cancel never writes. The pre-edit position is unrelated and
  may or may not already be in recents (it is iff the user reached
  view mode via `/game/import`; the play -> view -> edit path uses
  `/game/view/start`, which is a pure state flip with no save).

Migration (importing existing localStorage entries from older clients)
goes through `/game/import` per entry; the server treats each as a
normal import that happens not to enter view mode (or simply
re-imports them once each — the cost is bounded by the small
migration set).

## Concurrency

A single asyncio lock guards index read/modify/write. Two tabs racing
to POST the same hash become two serialized upserts with the second
just bumping `ts`. Cheap.

## Eviction

After every POST, if `len(index) > cap`, sort by `ts` ascending, drop
entries until size <= cap. Delete the blob first (best effort; orphans
are harmless), then remove the index row. Write index last so a crash
mid-eviction leaves a slightly stale but consistent state.

Cap: 50, constant for now. Re-evaluate after dogfooding.

## Client changes

- localStorage `sturddle:import:recent` becomes a metadata-only cache:
  `[{hash, format, summary, ts}, ...]`. Used to render the dropdown
  before the server responds (offline-tolerant).
- Drop debounced validation. The textarea no longer pings the server
  on input. Import button enables on non-empty text. Errors surface
  in the status label only after Import is clicked.
- On dialog open: fire `GET /game/recent-imports` to refresh the
  cache. Show whatever the local cache has immediately; replace on
  response.
- On recent-pick: `GET /game/recent-imports/{hash}` to fetch the
  blob into the textarea. (Server bumps `ts` as a side effect; client
  refreshes its cache from the GET-all response on next dialog open.)
  The user can then edit and click Import as usual.
- On import-success: cache the `{hash, format, summary, ts}` from the
  response. No separate POST needed.

No migration from pre-0.1.5 localStorage entries (which inlined the
full `text`): the project is unreleased, so any stale rows just fall
out of the cache on next dialog open when the server's
metadata-only list overwrites it.

Display cap (how many rows the dropdown shows) becomes independent
from the server cap. Start at 10; the dropdown is a `wa-select`, so
scrolling more is cheap.

## game_id contracts

Hard rules established when `game_id` was unified across the store,
HVE sessions, and tournament Replay. Future work must preserve them.

- **Immutability.** Once assigned to a store row, a `game_id` never
  changes for that row's lifetime. Re-`save` of the same hash keeps
  the original id.
- **Bijection at any instant.** Each `game_id` resolves to exactly one
  row; each row carries exactly one `game_id`. An evicted hash that is
  re-imported gets a fresh id; the old id is dead forever.
- **Sufficiency.** A `game_id` alone recovers the full row (`by-id`
  endpoint family) without knowing the hash.
- **Opacity.** Server-assigned; clients treat it as an opaque string --
  no slicing, no length assumptions, no structural parsing.
- **Uniqueness.** Same `game_id` for different hashes is a programming
  bug, not a data condition; crash on detection.
- **Active-session pinning.** The single active HVE session's
  `game_id`, if it matches a stored row, is never evicted. Rows with
  non-empty `refs` are also never evicted (cap becomes a soft floor).
- **Hash != identity.** Content edits that change the stored text
  invalidate the *hash* (row migrates to the new hash) but MUST
  preserve the *id*. Any code path conflating the two is wrong.

