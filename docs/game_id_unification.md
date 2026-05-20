# game_id Unification

No backward-compat. Wipe `<user_data_dir>/imports/` freely.

Module/class/endpoint names below stay as today (`recent_imports`,
`RecentImports`, `/game/recent-imports`). A future rename is
tracked separately; do not mix it with this work.

## Phase 1: Persist `game_id` in the store

Row becomes `{format, summary, ts, file, game_id, refs}`.

`refs` is a list of `game_id`s that reference this row (e.g. a
future cross-game annotation pointing here). Reserved now,
unused until a later phase wires it up. Always `[]` in this
scope.

`refs` is intentionally shallow -- plain `list[str]` of game_ids,
not list-of-objects. Future enrichment (kind, ts, etc.) would
require a schema bump and is out of scope here.

Future (out of scope): on startup, validate every `game_id` in
every `refs` list against the live index; drop stale entries and
WARN-log. Without this, bugs in ref accounting silently retain
otherwise-evictable rows across restarts. Not needed yet because
`refs` is unused in this project; document for the phase that
wires up references.

`save(fmt, text, summary)` -> `save(fmt, text, summary, game_id)`.
First save wins: re-save of an existing hash keeps the original
`game_id` and only bumps `ts`.

Add a `game_id -> hash` reverse index, rebuilt from `index.json` on
startup.

**Active-session pinning.** There is at most one active HVE session
at any time. Its `game_id`, if it matches a stored row, must never
be evicted. The store consults the active HVE for its current
`game_id` before evicting; the matching row is skipped and the next
candidate is considered.

**Refs pinning (forward-compat).** Eviction also skips any row
with a non-empty `refs` list. In this scope `refs` is always `[]`
so the rule is a no-op; codified now so future work that
populates `refs` does not require changing eviction semantics.
Cap becomes a soft floor: when every remaining row is either
active-session-pinned or has non-empty `refs`, the store grows
beyond the cap and logs a WARNING.

**UUID collision is a programming bug.** On `save`, if the supplied
`game_id` already exists in the reverse index but maps to a
*different* hash, hard-assert and crash. uuid4 collision is
~2^-122; observing one means the RNG is broken or the id-generation
code is. Surface it loudly rather than corrupt the store.

**Hash collision on save (different `game_id`, same hash).** First
save wins: the row keeps the original `game_id`; the new `game_id`
is orphaned (no store row). Log a WARNING with both ids, the hash,
and the incoming `game_id`. Normal play should not produce this;
when it does, the cause is either a tournament-determinism bug or
an unusual user setup (e.g. a very long opening book consumed
fully by both engines so every game replays the same moves). The
log line is the only signal we surface in this scope.

### Phase 1 validation

TDD:

- `save` accepts `game_id`, round-trips through `index.json` and
  `list()`.
- Re-`save` of existing hash keeps original `game_id` even when a
  different one is passed.
- Eviction removes the reverse-index entry.
- Eviction skips a row whose `game_id` matches the active HVE
  session's `_game_id` and evicts the next candidate instead.
- `refs` defaults to `[]` on save and round-trips through
  `index.json` and `list()`.
- Eviction skips any row with non-empty `refs` (test by injecting
  a synthetic ref into the index).
- `commit_edit` save call site
  (`api/game.py::edit_commit`) gains the new `game_id` kwarg.
  Unit test: after a position-changing commit, the new row is in
  the store with the active HVE session's current `_game_id`.
- `get_by_id(game_id)` returns the same row as `get(hash)`;
  `None` for unknown / evicted ids.

Existing `save()` call sites in tests gain `game_id=` kwarg.
Direct callers are confined to `tests/test_recent_imports.py`;
API tests hit endpoints and need no signature update.

## Phase 2: Full-UUID `game_id`

- HVE: `uuid.uuid4().hex[:12]` -> `str(uuid.uuid4())`.
- Tournament `pair_id`: already full UUID.
- Autosave filename grows 12 -> 36 chars.

### Phase 2 prerequisite (audit)

Grep audit before flipping HVE to full UUIDs:

- `server/tests/fixtures/`, `server/tests/**/snapshots/`, and any
  committed workspace-state JSON for hard-coded `[a-f0-9]{12}`
  literals that could be `game_id` values. Widen, regenerate, or
  parameterize.
- Non-test code (server + client) for filename-parsing regexes on
  autosave PGNs and any `[a-f0-9]{12}` patterns. Update or confirm
  none exist. Tests do not cover external file-name consumers.

### Phase 2 validation

- New test: `HumanVsEngine._game_id` matches a full UUID regex
  after `new_game`.
- Autosave filename regex tightened to 36-hex.
- PGN body snapshots unaffected (game_id is not in the PGN).

## Phase 3: Import uses stored `game_id`

`POST /game/import`:
- Hash in store -> hand stored `game_id` to `enter_view_mode`.
- Hash new -> mint UUID, pass to both `enter_view_mode` and
  `save()`.

`enter_view_mode` gains optional `game_id`. `new_game` does not.

`POST /game/edit/commit`:
- Unchanged-position branch: already keeps prior `game_id`.
- Changed-position branch: mint new UUID, save with it.

### Phase 3 validation

TDD (extend `test_pgn_export.py` or new file):

- First import mints + stores + returns a fresh `game_id`.
- Second import of same bytes returns the same `game_id`; HVE's
  `_game_id` equals the stored value.
- `commit_edit` changed-position -> different `game_id`, stored.
- `commit_edit` unchanged-position -> same `game_id` (regression
  guard for the recent fix).

## Phase 4: Tournament Replay -> store

No automatic capture. The store grows only when the user takes
action: watching a tournament game, choosing to Replay it in view
after it ends. The existing Replay path already POSTs to
`/game/import` with the tournament's PGN text. The only change:
the Replay caller now also passes the tournament `pair_id` as
`game_id` so the import path hands it to `recent_imports.save`.

Caller file: `web/app/tournament-live-game.js`. POST body becomes
`{format: "pgn", text: pgn, game_id: pair_id}`.

`POST /game/import` accepts an optional `game_id` in its payload.
Semantics:
- New hash + supplied `game_id` -> use it for the new store row
  and the view session.
- New hash + no supplied `game_id` -> mint uuid4.
- Existing hash + supplied `game_id` -> assert match against the
  stored row's id. Mismatch = 409 + WARN log (per the hash-
  collision rule). Match = use stored id.
- Existing hash + no supplied `game_id` -> use the stored id.

`game_id` in the request body is an *assertion* by the client:
"this content has this identity". Server verifies; mismatch is a
bug, not a silent reconcile.

Use case A (organic UI import): client may omit `game_id`
entirely, or include it when picking from recents (where the id
is known). Use case B (tournament Replay): client always
includes `game_id = pair_id`.

### Phase 4 validation

TDD:

- Tournament games that end with no user Replay -> store stays
  empty.
- User Replay of a finished tournament game -> store has one
  entry with `game_id == pair_id`.
- Replaying the same tournament game twice -> single store entry,
  same `game_id`.
- Replaying after a rematch with identical PGN bytes -> first
  Replay's `pair_id` wins; WARNING log emitted per the
  hash-collision rule.

## Phase 5: New API surface

- `GET /game/recent-imports` -- each row gains `game_id`.
- `GET /game/recent-imports/{h}` -- response gains `game_id`.
- `GET /game/recent-imports/by-id/{game_id}` -- same payload as
  the hash lookup. 404 for unknown / evicted.

### Phase 5 validation

TDD (API integration tests):

- Both list and single-hash endpoints include `game_id`.
- `by-id` returns the same payload as the hash route.
- `by-id/{unknown}` -> 404.
- `by-id/{evicted}` -> 404.

## Contracts

These are the hard rules. Future work must preserve them.

- **`game_id` immutability.** Once a `game_id` is assigned to a
  store row, it never changes for that row's lifetime in the
  store. Re-`save` of the same hash keeps the original id.
- **Bijection within the store at any instant.** Each `game_id`
  resolves to exactly one row; each row carries exactly one
  `game_id`. The reverse index is total. Eviction breaks
  bijection only across time, not at any single instant: an
  evicted hash that is re-imported gets a fresh `game_id`; the
  old id is dead forever.
- **`game_id` is sufficient to recover a row.** Any consumer
  holding a `game_id` can fetch the full row (text, summary,
  format, ts) without knowing the hash. This justifies the
  `by-id` endpoint family and any future expansion of it
  (delete-by-id, etc.).
- **`game_id` opacity.** Server-assigned, client treats as an
  opaque string. No slicing, no length assumptions, no
  structural parsing anywhere.
- **`game_id` uniqueness.** Collision (same `game_id` for
  different hashes) is a programming bug, not a data condition.
  Crash on detection.
- **Active session pinning.** The single active HVE session's
  `game_id`, if it matches a stored row, is never evicted.
- **Future-mutating operations decouple `hash` from `game_id`.**
  Annotations / commentary / metadata edits that change the
  stored text invalidate the *hash* (new content -> new hash)
  but MUST preserve the *id*. The store row migrates to the new
  hash; `game_id` stays. Two consequences: (1) hash is a
  content fingerprint, not an identity; (2) any code path that
  conflates the two is wrong.

## Sequencing

Phases 1-5 land independently or in sequence; each is
green-on-its-own and additive.

## Follow-up (post-Phase 3)

Once `/game/import` returns a stable `game_id` matching the
tournament `pair_id`, the cross-perspective "Viewing ..." toast
wiring (`sturddle:activate-perspective` + `perspective-activated`
+ play-side listener) can be torn down. The Replay caller already
holds `pair_id`; the import response confirms it server-side. The
toast and any UI follow-up run at the call site.

Keep one piece of the existing code -- repurposed as a hard
assert: `r.game_id === pairId`. Mismatch is a bug, not a
race; crash loudly.
