# PGN pair identity: bucket-by-engine-set, drop only orphans

Status: design. Ready to implement once gauntlet Round convention is
empirically confirmed (see "Open verification" below).

## Why this doc exists

A 2-engine round-robin tour produced disagreement between four counts of
"how many games were played": our UI/standings, our PGN-stats compute,
ordo run on the PGN, and fastchess's own stdout summary. Investigation
traced one of the disagreements to a soundness bug in
`pgn_stats._iter_games_uncached`: the dedup pass was silently deleting
legitimate distinct games on every Pause/Resume rewrite. This doc
captures the root cause and the replacement scheme.

## TL;DR

Two things were going on:

1. Fastchess's notion of resume/recover doesn't fully match ours,
   especially the pre-config-patch era when our orchestrator did not
   keep fastchess's config.json in sync after a Stop.

2. As a result, the `[Round "N"]` tag value in PGN headers can be
   **reassigned/reused** across a Pause/Resume boundary. The same
   value appears on multiple distinct color-flipped pairs.

The pgn_stats code treated `Round` as a globally-unique pair identifier
(dedup key, partial-pair key, SPRT pair-group key). That assumption is
false in any PGN that survived a Pause/Resume.

The fix:

> Stop using `Round` as a pair identity key. Identify pairs structurally
> -- by color-flip completeness on the same engine set within a Round
> bucket -- and drop only true orphans, never "duplicates".

A "true orphan" is a game whose `(Round, engine-set)` bucket has no
color-flip partner: typically the odd game out from a Stop/kill that
landed between game 1 and game 2 of a pair.

## What the broken assumption looked like in the data

In the affected tour's PGN (2-engine round-robin):

- 781 Rounds had 2 games each (clean pair) -- the common case.
- 11 Rounds had 4 games each -- two complete distinct pairs sharing a
  Round value.
- 1 Round had 3 games -- a pair plus one real or apparent extra.

Total: 23 PGN entries shared a `(Round, White, Black)` key with another
entry. Of those, 7 dup-groups had *differing* results between the
"duplicates" -- proof that they could not all be the same game replayed.

`rewrite_drop_partial_pairs` applied last-wins dedup on every Resume,
silently destroying real games on disk (backed up to .bak.gz, but
otherwise permanently dropped from the live PGN). The most recent
.bak.gz contained 15+ more game entries than the live PGN that had
been rewritten from it.

## What `Round` actually means

`[Round]` is a **local bucketing label** that fastchess writes per
game, not a globally-unique pair ID:

- Within a single uninterrupted fastchess session, Round increments
  through the schedule and each value identifies one pair.
- Across a Pause/Resume, fastchess re-derives "where do I pick up"
  from its config.json. Pre-config-patch, config.json was stale on
  resume, so the resumed session's Round numbering could collide
  with values already written to the PGN.
- The historical collisions are baked into any PGN that was paused
  before the config-patch fix landed. Even with the patch, the
  invariant we want ("Round is unique in the PGN file") is not
  something fastchess guarantees -- it's something we hoped was true.

## The new scheme: pair identity from structure, not from tags

A **pair** is a structural property of the games, not something we
read off a tag:

1. Group games by `(Round, frozenset({White, Black}))`. This is the
   bucket key. Two games with the same Round value but different
   engine sets (gauntlet) land in different buckets. Two games with
   the same Round value and the same engine set (2-engine collision)
   land in the same bucket.

2. Within each bucket, **match color-flips greedily**: for each
   `(A-as-white, B-as-black)` game, find one unmatched
   `(B-as-white, A-as-black)` game in the bucket and pair them.

3. After matching:
   - Paired games are kept in standings, included in SPRT pair-list.
   - Unmatched games (**orphans**) are kept in standings (W/L/D
     still counted, no data loss), excluded from SPRT pair-list,
     and become drop candidates for `rewrite_drop_partial_pairs`.

4. **Drop only orphans, never "duplicates".** A bucket of 4 games
   that all pair cleanly produces 2 pairs and 0 orphans -- nothing
   gets deleted. A bucket of 3 games where 2 color-flip and 1 has
   no partner produces 1 pair and 1 orphan -- the orphan is the
   drop candidate.

Pair mode is per-tour. Single-game mode (`-games 1`, no color-flip
intent) disables pair formation entirely:

- `paired=False`: every game stands alone, no orphan detection,
  no rewrite drops, SPRT uses logistic (trinomial) model only,
  `count_partial_pairs` returns 0, `_needs_rewrite` returns False.

The mode is known to the orchestrator at tour-creation time -- we
do not parse fastchess CLI args to re-derive it at runtime.

## Robustness summary

**Guaranteed sound:**

- `(round, engine-set)` bucketing handles Round-number reuse across
  pause/resume -- collisions across distinct engine-set buckets are
  auto-disambiguated.
- Color-flip matching is structural, not based on a trusted ID. As
  long as fastchess writes correct `[White]`/`[Black]`/`[Result]`
  tags, we identify real pairs correctly.
- Two complete pairs of the same engine set sharing one Round value
  (the 2-engine collision case) are matched as 2 pairs, not collapsed.
- Orphan detection is conservative: a game must have no color-flip
  partner in its bucket to be dropped. Pre-pair-completion Stops
  produce orphans; everything else is kept.
- Works the same way for 2-engine, gauntlet, and (with `paired=False`)
  single-game tours. No special cases beyond the paired/single switch.

**Relies on:**

- Fastchess writes the right engine names in `[White]`/`[Black]` and
  a valid `[Result]`. (Universal assumption -- our whole pipeline
  rests on this.)
- "Same engine set in the same Round" means "same intended pair".
  This holds if fastchess never assigns a Round number such that two
  different pair attempts of the same engine set land there -- and
  Resume-reuse produces complete distinct pairs that pair correctly
  anyway.

**Weaker than ideal:**

- True resume-dup (rare post-config-patch): produces an orphan that
  gets dropped. We cannot distinguish "extra copy of a played game"
  from "legitimate game whose partner was lost". The drop is the safe
  choice -- at worst we lose one real game per kill event, in exchange
  for no false drops in the common case.
- Two color-identical games of the same engine set in one Round bucket
  (e.g., two `A-as-white` games, no `B-as-white`): cannot be paired
  with each other. Both become orphans. This is the right call --
  they are not a pair.

**Does not assume:**

- That `Round` is globally unique.
- That `Round` is unique per pair.
- That games are written in Round order (concurrency reshuffles).
- That `-games` is exactly 2.
- That fastchess's resume picks up cleanly from where it stopped.

**Failure mode if wrong about gauntlet Round convention:**

Worst case: extra games per Round bucket that do not color-flip
cleanly become orphans and get dropped on next Resume rewrite. We
would see this as "unexpected orphans" in a gauntlet test. Detectable,
recoverable from .bak.gz, fixable with a refined bucketing rule.
Not silent.

## Gauntlet `[Round]` convention (verified)

Empirical check on a live 3-engine gauntlet (Sturddle 2.5.1 leader vs
2.5.0 and 2.4.0): fastchess increments `[Round]` **per (opening,
challenger) match-up**, not per opening. Each Round contains exactly
2 games of one engine set, color-flipped. Round 1 = leader vs C1,
Round 2 = leader vs C2, Round 3 = leader vs C1, and so on.

This is "Convention B" of the two we considered: it degenerates to
the 2-engine case per Round bucket. The `(round, engine-set)`
bucketing handles it trivially -- the engine-set component is the
same for both games in any given Round, so the bucket key is
effectively just Round.

If a Pause/Resume in a gauntlet ever produces a Round-value collision:

- Across different match-ups (resume's Round 50 lands on a different
  match-up than pre-pause's Round 50): engine-set disambiguates.
  Two separate buckets, each gets paired independently.
- On the same match-up: 4 games in one `(round, engine-set)` bucket,
  identical to the 2-engine collision case the design already
  handles -- all 4 pair, 0 orphans.

The design is sound for gauntlet under the observed Round convention
**and** under hypothetical resume-induced Round collisions.

## Implementation plan

| Function | Change |
|---|---|
| `_iter_games_uncached` | Remove the last-wins dedup pass. Yield every decisive-result entry in file order. |
| `_iter_games_keyed` / `_iter_games` | No behavioral change; flow through the un-deduped stream. |
| New: `_form_pairs(keyed, paired) -> (pairs, orphans)` | Group games by `(round, engine-set)`, greedy color-flip match within each bucket. Returns paired games and orphans. `paired=False` short-circuits with empty pair list, no orphans. |
| `compute_standings` | No change. Tallies every game including orphans; gauntlet path unaffected. |
| `_iter_pairs` (SPRT) | Replace `len(games) != 2` skip with `_form_pairs` output. SPRT consumes only complete pairs from the helper. |
| `count_partial_pairs` | Redefine as orphan count from `_form_pairs`. |
| `_needs_rewrite` | True iff `_form_pairs` reports any orphan. |
| `rewrite_drop_partial_pairs` | Use `_form_pairs` to identify orphans; drop only those. No dedup. `patch_config_json` deltas come from the orphan W/L/D. |
| Tests | Update fixtures: existing partial-pair tests stay; add Round-collision-with-two-complete-pairs test; add gauntlet bucketing test; add `paired=False` no-op test. |

`paired` is plumbed in from the tour's tournament-type/SPRT-model
configuration at the orchestrator level. The PGN-stats functions take
it as an explicit argument; they do not read tour config themselves.

## Recovery for the in-flight tour

The affected tour has 22+ `games.pgn.*.bak.gz` snapshots. Each is the
pre-rewrite PGN at one Pause boundary. Recovery procedure (one-off,
not part of the code fix):

1. Stop the tour cleanly so no further rewrites happen.
2. Walk the .bak.gz files in chronological order. Each backup is a
   superset of the next one's pre-rewrite state (assuming rewrites
   only drop, which has been true).
3. For each `(Round, White, Black, Result)` entry that exists in any
   backup but not in the current PGN, decide: was it dropped as a
   legitimate game (recover) or as a true resume-dup (skip)? The
   safest heuristic: if the entry's Round bucket in the current PGN
   already contains a color-flip partner for the missing game, the
   missing game is likely a real legitimate game and should be
   merged back in. If not, leave it out.
4. Rewrite the live PGN with the merged content; back up the
   superseded one. Recompute config.json stats from scratch (the
   running tally is no longer trustworthy after a manual merge).
5. Resume the tour.

This recovery is workflow-only and does not need to be automated.
The fix above prevents future deletions; the recovery is to repair
historical loss.
