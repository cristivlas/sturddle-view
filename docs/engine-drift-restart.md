# Engine-settings drift at tournament (re)start

## Problem

Tournaments freeze everything at create/edit time (template,
`engine_defaults`, per-engine args/env/UCI options). Editing an engine
in Settings > Engines afterwards silently does NOT affect existing
tournaments -- the user starts a tournament believing it runs the
engine's current configuration, but it runs the snapshot.

## UX

On Start/Restart of an idle/stopped/failed tournament, the client
detects drift between the frozen engine refs and the current registry.

- No drift: start exactly as today (existing confirms unchanged).
- Drift: one dialog replaces the plain restart confirm (idle start:
  net-new dialog, there is no confirm today), with a fixed one-line
  message (no per-engine or per-option enumeration) and three choices:
  1. **Update & start** -- re-freeze from the registry, then start.
  2. **Start with saved** -- start the snapshot unchanged.
  3. **Cancel** -- the dialog X/Esc, not a third button: footer keeps
     the standard two-button footprint (`showDialog` resolves null on
     close; header shown for the X, unlike `confirm()`'s no-header).
  When the tournament has recorded games, the dialog carries the
  existing "games will be permanently deleted" warning (both paths
  wipe: update via PATCH, restart via `confirm_wipe`).

Duplicate already re-resolves from the registry in its dialog; DONE
tournaments cannot start or be edited. Neither changes.

## Detection rule (client-side, per spec's client-owns-resolution split)

For each frozen ref, match a registry entry by id, then name, then cmd
(`resolveInitialEngines` matching; deleted engines = no drift). Compare:

- `args` (legacy string normalizes to a ONE-element list, mirroring
  fastchess.py -- never split it), `env`,
- `options`, ignoring tournament-managed keys (casefold match) -- JS
  mirror of
  `_managed_option_keys`: Hash/Threads/SyzygyPath/Ponder/OwnBook are
  managed only when the tournament defines them,
- `cmd`/`name` (registry rename or binary move counts as drift).

Missing `options` snapshot vs a registry entry with options is genuine
drift (pre-snapshot tournaments prompt once, then converge).

## Update & start flow

1. GET `/engines?probe=false` (skips the lazy schema probe, so Start
   never spawns engines), rebuild refs from the matched registry
   entries. Unmatched (deleted) refs round-trip verbatim, `options`
   included -- `EngineRef.options` accepts input for exactly this;
   dropping them would silently shrink the roster or lose the snapshot.
2. Re-fold `max_threads`/`max_hash_mb` (`resolveResourceParams`) --
   the frozen fold may be stale.
3. POST `/api/tournaments/rescheck`; on blocker, surface and abort;
   warnings toast as in Edit.
4. PATCH the tournament (same name/template apart from the re-fold;
   resets to idle, wipes games server-side; `store.update` re-pins a
   template seed when absent, mirroring create).
5. POST `/start` (no `confirm_wipe` needed post-PATCH).

Entry points sharing the gate: `startOne` (Arena + Studio verbs) and
the workspace `restartFromBanner`.

## Accepted edges

- PATCH-then-start can lose a busy race: tournament is left updated
  and idle. Benign; list resync shows it.
- PATCH also re-freezes `engine_defaults` from current global Settings
  (server behavior): Update & start refreshes that snapshot too, Start
  with saved keeps it. Global-defaults drift alone is not detected.
- Managed-keys rule now exists in JS and Python; keep in sync.

## Tests

- Unit: `GET /engines?probe=false` spawns no probe; `store.update`
  re-pins / preserves the template seed.
- e2e (`test_e2e_tournament_drift_ui.py`): drifted engine -> dialog;
  Update & start re-freezes (assert saved options + seed + re-folded
  caps via the API); Esc cancels; Start with saved keeps the snapshot;
  no-drift start shows no dialog.
