from __future__ import annotations

import asyncio
import json
import time

import pytest

from sturddle_view.recent_imports import (
    RecentImports,
    RemoveStatus,
    _hash_text,
)


@pytest.fixture
def store(tmp_path):
    return RecentImports.load(root=tmp_path / "imports", cap=5)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_empty_when_no_file(store):
    assert store.list() == []


def test_save_writes_blob_and_index(store, tmp_path):
    h = _run(store.save(fmt="pgn", text="[Event \"X\"]\n1. e4 e5 *", summary="Two-move PGN"))
    [row] = store.list()
    assert row["hash"] == h
    assert row["format"] == "pgn"
    assert row["summary"] == "Two-move PGN"
    # Blob present, content matches.
    blob = (tmp_path / "imports" / row["file"]).read_text(encoding="utf-8")
    assert blob == "[Event \"X\"]\n1. e4 e5 *"
    # Index file mirrors memory.
    idx = json.loads((tmp_path / "imports" / "index.json").read_text(encoding="utf-8"))
    assert h in idx
    assert idx[h]["format"] == "pgn"


def test_save_dedupes_by_hash(store):
    h1 = _run(store.save(fmt="pgn", text="1. e4 e5 *", summary="s1"))
    h2 = _run(store.save(fmt="pgn", text="1. e4 e5 *", summary="s2"))
    assert h1 == h2
    # Same hash -> single entry. Summary updates to the latest.
    [row] = store.list()
    assert row["summary"] == "s2"


def test_save_trims_whitespace_for_hash(store):
    h1 = _run(store.save(fmt="pgn", text="  1. e4 e5 *  \n", summary="trimmed"))
    h2 = _run(store.save(fmt="pgn", text="1. e4 e5 *", summary="plain"))
    assert h1 == h2


# Distinct legal first moves so canonical hashes differ (canonicalization
# strips comments/junk, so an integer suffix would collapse to one entry).
_DISTINCT_PGNS = [
    "1. e4 *", "1. d4 *", "1. c4 *", "1. Nf3 *",
    "1. g3 *", "1. b3 *", "1. f4 *",
]


def test_save_evicts_oldest_past_cap(store):
    # cap=5 (from fixture). Save 6 -> first one drops.
    hashes = []
    for pgn in _DISTINCT_PGNS[:6]:
        # Pause so ts differs measurably between writes (eviction sorts on ts).
        time.sleep(0.002)
        hashes.append(_run(store.save(fmt="pgn", text=pgn, summary="s")))
    rows = store.list()
    assert len(rows) == 5
    kept_hashes = {r["hash"] for r in rows}
    assert hashes[0] not in kept_hashes  # oldest evicted
    assert hashes[5] in kept_hashes  # newest retained


def test_evicted_blob_is_deleted(store, tmp_path):
    for pgn in _DISTINCT_PGNS[:6]:
        time.sleep(0.002)
        _run(store.save(fmt="pgn", text=pgn, summary="s"))
    # After the 6th save, the oldest blob should be gone.
    remaining = list((tmp_path / "imports" / "by-hash").iterdir())
    assert len(remaining) == 5


def test_get_returns_text_and_row(store):
    h = _run(store.save(fmt="pgn", text="1. d4 d5 *", summary="d-pawn"))
    got = store.get(h)
    assert got is not None
    row, text = got
    assert text == "1. d4 d5 *"
    assert row["format"] == "pgn"
    assert row["summary"] == "d-pawn"


def test_get_returns_none_for_unknown_hash(store):
    assert store.get("nonexistent") is None


def test_touch_bumps_ts(store):
    h_old = _run(store.save(fmt="pgn", text="1. c4 *", summary="english"))
    time.sleep(0.005)
    h_new = _run(store.save(fmt="pgn", text="1. e4 *", summary="ke"))
    # h_old is older; touch it and it should rise to top.
    rows = store.list()
    assert rows[0]["hash"] == h_new  # newest by default
    _run(store.touch(h_old))
    rows = store.list()
    assert rows[0]["hash"] == h_old


def test_touch_unknown_is_noop(store):
    _run(store.touch("nonexistent"))  # must not raise


def test_remove_drops_index_and_blob(store, tmp_path):
    h = _run(store.save(fmt="pgn", text="1. b3 *", summary="larsen"))
    rel = store.list()[0]["file"]
    assert (tmp_path / "imports" / rel).exists()
    result = _run(store.remove(h))
    assert result.status is RemoveStatus.DELETED
    assert store.list() == []
    assert not (tmp_path / "imports" / rel).exists()


def test_remove_unknown_returns_not_found(store):
    result = _run(store.remove("nonexistent"))
    assert result.status is RemoveStatus.NOT_FOUND


def test_load_recovers_from_index_on_disk(tmp_path):
    s1 = RecentImports.load(root=tmp_path / "imports", cap=5)
    h = _run(s1.save(fmt="pgn", text="1. Nf3 *", summary="reti"))
    s2 = RecentImports.load(root=tmp_path / "imports", cap=5)
    rows = s2.list()
    assert len(rows) == 1
    assert rows[0]["hash"] == h
    got = s2.get(h)
    assert got is not None
    _, text = got
    assert text == "1. Nf3 *"


def test_load_drops_index_rows_whose_blob_is_missing(tmp_path):
    s1 = RecentImports.load(root=tmp_path / "imports", cap=5)
    h = _run(s1.save(fmt="pgn", text="1. e4 *", summary="x"))
    # Delete the blob out of band.
    rel = s1.list()[0]["file"]
    (tmp_path / "imports" / rel).unlink()
    s2 = RecentImports.load(root=tmp_path / "imports", cap=5)
    assert s2.list() == []


def test_fen_format_uses_fen_extension(store, tmp_path):
    h = _run(store.save(fmt="fen", text="r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3", summary="italian"))
    rel = store.list()[0]["file"]
    assert rel.endswith(".fen")
    assert (tmp_path / "imports" / rel).exists()


def test_hash_is_stable(store):
    h1 = _hash_text("hello")
    h2 = _hash_text("hello")
    assert h1 == h2
    assert h1 != _hash_text("HELLO")


# ---- Phase 1: game_id, refs, active-session pinning ----

def test_save_round_trips_game_id(store, tmp_path):
    h = _run(store.save(fmt="pgn", text="1. e4 *", summary="s", game_id="gid-1"))
    [row] = store.list()
    assert row["game_id"] == "gid-1"
    # Round-trip via index.json (load fresh).
    s2 = RecentImports.load(root=tmp_path / "imports", cap=5)
    [row2] = s2.list()
    assert row2["game_id"] == "gid-1"
    assert row2["refs"] == []


def test_save_refs_default_empty_and_round_trip(store, tmp_path):
    _run(store.save(fmt="pgn", text="1. e4 *", summary="s", game_id="gid-1"))
    [row] = store.list()
    assert row["refs"] == []
    s2 = RecentImports.load(root=tmp_path / "imports", cap=5)
    [row2] = s2.list()
    assert row2["refs"] == []


def test_resave_keeps_original_game_id(store):
    h = _run(store.save(fmt="pgn", text="1. e4 *", summary="s", game_id="gid-A"))
    _run(store.save(fmt="pgn", text="1. e4 *", summary="s2", game_id="gid-B"))
    [row] = store.list()
    assert row["hash"] == h
    assert row["game_id"] == "gid-A"
    # Orphaned id has no reverse-index entry.
    assert store.hash_for_id("gid-B") is None


def test_get_by_id_returns_same_as_get(store):
    h = _run(store.save(fmt="pgn", text="1. e4 *", summary="s", game_id="gid-1"))
    by_h = store.get(h)
    by_id = store.get_by_id("gid-1")
    assert by_h is not None and by_id is not None
    assert by_h[0]["game_id"] == by_id[0]["game_id"]
    assert by_h[1] == by_id[1]


def test_get_by_id_none_for_unknown(store):
    assert store.get_by_id("nonexistent") is None


def test_eviction_removes_reverse_index_entry(store):
    for i, pgn in enumerate(_DISTINCT_PGNS[:6]):
        time.sleep(0.002)
        _run(store.save(fmt="pgn", text=pgn, summary="s", game_id=f"gid-{i}"))
    # Cap=5; gid-0 was the oldest -> evicted -> reverse-index entry gone.
    assert store.hash_for_id("gid-0") is None
    # Survivors still resolve.
    assert store.hash_for_id("gid-5") is not None


def test_eviction_skips_active_session_pinned_row(tmp_path):
    """Active-id row is the sole eviction candidate; pinning makes the
    store grow past cap (soft floor) instead of dropping it."""
    active_id = "active-gid"
    store = RecentImports.load(
        root=tmp_path / "imports", cap=5, active_game_id=lambda: active_id,
    )
    # active_id is the OLDEST, so it's the only eviction candidate
    # after the 6th save.
    _run(store.save(fmt="pgn", text=_DISTINCT_PGNS[0], summary="s", game_id=active_id))
    for i, pgn in enumerate(_DISTINCT_PGNS[1:6], start=1):
        time.sleep(0.002)
        _run(store.save(fmt="pgn", text=pgn, summary="s", game_id=f"gid-{i}"))
    rows = store.list()
    ids = {r["game_id"] for r in rows}
    assert active_id in ids  # pinned, never evicted
    # Store grew past cap because the sole eviction candidate was pinned.
    assert len(rows) == 6


def test_eviction_skips_pinned_and_drops_next_candidate(tmp_path):
    """When a pinned row and an unpinned row are both eligible for
    eviction, the unpinned one drops instead."""
    active_id = "active-gid"
    store = RecentImports.load(
        root=tmp_path / "imports", cap=5, active_game_id=lambda: active_id,
    )
    # Two oldest entries: active (pinned), then gid-other.
    _run(store.save(fmt="pgn", text=_DISTINCT_PGNS[0], summary="s", game_id=active_id))
    time.sleep(0.002)
    _run(store.save(fmt="pgn", text=_DISTINCT_PGNS[1], summary="s", game_id="gid-other"))
    for i, pgn in enumerate(_DISTINCT_PGNS[2:7], start=2):
        time.sleep(0.002)
        _run(store.save(fmt="pgn", text=pgn, summary="s", game_id=f"gid-{i}"))
    rows = store.list()
    ids = {r["game_id"] for r in rows}
    assert active_id in ids  # pinned, kept
    assert "gid-other" not in ids  # next eviction candidate, dropped
    assert len(rows) == 6  # cap=5 + pinned overhang


def test_eviction_skips_row_with_nonempty_refs(tmp_path):
    """Refs-pinned row is the sole eviction candidate; store grows past cap."""
    store = RecentImports.load(root=tmp_path / "imports", cap=5)
    _run(store.save(fmt="pgn", text=_DISTINCT_PGNS[0], summary="s", game_id="gid-0"))
    pinned_hash = store.list()[0]["hash"]
    store._index[pinned_hash]["refs"] = ["referrer-gid"]
    for i, pgn in enumerate(_DISTINCT_PGNS[1:6], start=1):
        time.sleep(0.002)
        _run(store.save(fmt="pgn", text=pgn, summary="s", game_id=f"gid-{i}"))
    rows = store.list()
    ids = {r["game_id"] for r in rows}
    assert "gid-0" in ids  # refs-pinned, kept
    # Sole candidate was pinned; store grew past cap.
    assert len(rows) == 6


def test_eviction_soft_floor_when_all_pinned(tmp_path, caplog):
    # All rows are refs-pinned BEFORE the over-cap save -> store grows
    # past cap and WARNs.
    store = RecentImports.load(root=tmp_path / "imports", cap=3)
    for i, pgn in enumerate(_DISTINCT_PGNS[:3]):
        time.sleep(0.002)
        _run(store.save(fmt="pgn", text=pgn, summary="s", game_id=f"gid-{i}"))
    # Pin everything in-place, then push over cap.
    for h in list(store._index):
        store._index[h]["refs"] = ["pinned"]
    with caplog.at_level("WARNING"):
        _run(store.save(fmt="pgn", text=_DISTINCT_PGNS[3], summary="s", game_id="gid-extra"))
    # Soft floor: all 3 existing rows were refs-pinned, so the 4th
    # save can't evict and the store grows past cap.
    assert len(store.list()) == 4
    assert any("cap (3) exceeded" in r.message for r in caplog.records)


def test_save_collision_assertion_crashes(store):
    _run(store.save(fmt="pgn", text="1. e4 *", summary="s", game_id="gid-X"))
    # Reusing gid-X for a *different* hash is a programming bug.
    with pytest.raises(AssertionError):
        _run(store.save(fmt="pgn", text="1. d4 *", summary="s", game_id="gid-X"))


def test_save_hash_collision_warns_and_keeps_original(store, caplog):
    _run(store.save(fmt="pgn", text="1. e4 *", summary="s", game_id="gid-A"))
    with caplog.at_level("WARNING"):
        _run(store.save(fmt="pgn", text="1. e4 *", summary="s", game_id="gid-B"))
    assert any("hash collision" in r.message for r in caplog.records)
    [row] = store.list()
    assert row["game_id"] == "gid-A"


def test_remove_clears_reverse_index(store):
    h = _run(store.save(fmt="pgn", text="1. e4 *", summary="s", game_id="gid-1"))
    _run(store.remove(h))
    assert store.hash_for_id("gid-1") is None
    assert store.get_by_id("gid-1") is None


def test_get_by_id_returns_none_after_eviction(tmp_path):
    """Evicted ids are dead forever; get_by_id returns None."""
    store = RecentImports.load(
        root=tmp_path / "imports", cap=2, active_game_id=lambda: None,
    )
    _run(store.save(fmt="pgn", text=_DISTINCT_PGNS[0], summary="s", game_id="gid-0"))
    time.sleep(0.002)
    _run(store.save(fmt="pgn", text=_DISTINCT_PGNS[1], summary="s", game_id="gid-1"))
    time.sleep(0.002)
    _run(store.save(fmt="pgn", text=_DISTINCT_PGNS[2], summary="s", game_id="gid-2"))
    # gid-0 was oldest -> evicted.
    assert store.get_by_id("gid-0") is None
    assert store.hash_for_id("gid-0") is None
    # Survivors still resolve.
    assert store.get_by_id("gid-2") is not None


def test_load_rebuilds_reverse_index(tmp_path):
    s1 = RecentImports.load(root=tmp_path / "imports", cap=5)
    _run(s1.save(fmt="pgn", text="1. e4 *", summary="s", game_id="gid-1"))
    s2 = RecentImports.load(root=tmp_path / "imports", cap=5)
    got = s2.get_by_id("gid-1")
    assert got is not None
    assert got[1] == "1. e4 *"


# ---------------------------------------------------------------------------
# replace_at: atomic swap of content for a preserved game_id.
# ---------------------------------------------------------------------------


def test_replace_at_evicts_old_inserts_new_rebinds_game_id(store, tmp_path):
    """Pre-edit row at H1 -> replace_at -> H2 row owns game_id, H1 gone."""
    h1 = _run(store.save(fmt="pgn", text="1. e4 *", summary="s1", game_id="gid-1"))
    h2 = _run(store.replace_at(
        old_hash=h1, fmt="pgn", text="1. e4 e5 *", summary="s2", game_id="gid-1",
    ))
    assert h1 != h2
    # H1 row + blob gone.
    assert store.get(h1) is None
    assert not (tmp_path / "imports" / "by-hash" / f"{h1}.pgn").exists()
    # H2 row carries the same game_id, fresh summary.
    row, text = store.get(h2)
    assert row["game_id"] == "gid-1"
    assert row["summary"] == "s2"
    assert text == "1. e4 e5 *"
    # Reverse index now points at H2.
    assert store.hash_for_id("gid-1") == h2


def test_replace_at_with_old_hash_none_promotes_to_recents(store):
    """No pre-edit row -> insert at new_hash, bind game_id. This is the
    play -> view -> edit -> annotate path."""
    h = _run(store.replace_at(
        old_hash=None, fmt="pgn", text="1. d4 *", summary="annotated", game_id="gid-promote",
    ))
    row, _ = store.get(h)
    assert row["game_id"] == "gid-promote"
    assert row["summary"] == "annotated"
    assert store.hash_for_id("gid-promote") == h


def test_replace_at_missing_old_hash_treated_as_insert(store):
    """old_hash provided but absent from index (already evicted?) -> just
    insert at new_hash. No error."""
    h = _run(store.replace_at(
        old_hash="deadbeef" * 8, fmt="pgn", text="1. c4 *", summary="s", game_id="gid",
    ))
    assert store.get(h) is not None


def test_replace_at_same_hash_collapses_to_summary_refresh(store):
    """Caller computed new_hash == old_hash (content didn't actually
    change) -> single row, summary/ts refreshed, game_id intact."""
    h1 = _run(store.save(fmt="pgn", text="1. e4 *", summary="orig", game_id="gid"))
    h2 = _run(store.replace_at(
        old_hash=h1, fmt="pgn", text="1. e4 *", summary="refreshed", game_id="gid",
    ))
    assert h1 == h2
    [row] = store.list()
    assert row["hash"] == h1
    assert row["summary"] == "refreshed"
    assert row["game_id"] == "gid"


def test_replace_at_refuses_old_hash_bound_to_different_game_id(store):
    """Programming bug: caller asks to evict a row whose game_id doesn't
    match the one they're preserving. Surface loudly."""
    h1 = _run(store.save(fmt="pgn", text="1. e4 *", summary="s", game_id="gid-other"))
    with pytest.raises(AssertionError, match="bound to a different game_id"):
        _run(store.replace_at(
            old_hash=h1, fmt="pgn", text="1. d4 *", summary="s", game_id="gid-mine",
        ))


def test_replace_at_persists_to_disk(store, tmp_path):
    """After replace_at, reloading the store sees the new state, not the old."""
    h1 = _run(store.save(fmt="pgn", text="1. e4 *", summary="s1", game_id="gid"))
    h2 = _run(store.replace_at(
        old_hash=h1, fmt="pgn", text="1. e4 e5 *", summary="s2", game_id="gid",
    ))
    s2 = RecentImports.load(root=tmp_path / "imports", cap=5)
    assert s2.get(h1) is None
    got = s2.get_by_id("gid")
    assert got is not None
    row, text = got
    assert row["summary"] == "s2"
    assert text == "1. e4 e5 *"


def test_replace_at_new_hash_collides_with_unrelated_game(store):
    """new_hash already present and bound to a different game_id (a 2^-256
    event). Old row evicted, but we cannot rebind game_id; the stored id
    wins, our binding is dropped."""
    # Pre-existing row at H_target bound to gid-other.
    h_target = _run(store.save(fmt="pgn", text="1. d4 *", summary="other", game_id="gid-other"))
    # Our game at H_ours.
    h_ours = _run(store.save(fmt="pgn", text="1. e4 *", summary="ours", game_id="gid-ours"))
    # Replace ours -> request lands on H_target (simulated via precomputed_hash).
    result = _run(store.replace_at(
        old_hash=h_ours, fmt="pgn", text="something irrelevant", summary="merged",
        game_id="gid-ours", precomputed_hash=h_target,
    ))
    assert result == h_target
    # Our old row gone.
    assert store.get(h_ours) is None
    # H_target row keeps gid-other; gid-ours is now unmapped.
    row, _ = store.get(h_target)
    assert row["game_id"] == "gid-other"
    assert store.hash_for_id("gid-ours") is None


# ---- x-game navigation: fork-link tests ----


def _save_parent(store, gid="gid-parent"):
    return _run(store.save(
        fmt="pgn", text="1. e4 e5 *", summary={"white": "P"}, game_id=gid,
    ))


def _save_child(store, parent_gid, ply, gid="gid-child", text="1. d4 d5 *"):
    return _run(store.save(
        fmt="pgn", text=text, summary={"white": "C"}, game_id=gid,
        parent_game_id=parent_gid, fork_ply=ply,
    ))


def test_save_with_parent_links_both_sides(store):
    h_parent = _save_parent(store)
    h_child = _save_child(store, "gid-parent", ply=5)
    parent_row = store.get(h_parent)[0]
    child_row = store.get(h_child)[0]
    assert child_row["parent_game_id"] == "gid-parent"
    assert child_row["fork_ply"] == 5
    assert parent_row["refs"] == [{"game_id": "gid-child", "fork_ply": 5}]


def test_save_requires_parent_and_ply_together(store):
    with pytest.raises(AssertionError):
        _run(store.save(
            fmt="pgn", text="1. e4 *", summary="s", game_id="g",
            parent_game_id="gid-parent",
        ))
    with pytest.raises(AssertionError):
        _run(store.save(
            fmt="pgn", text="1. e4 *", summary="s", game_id="g",
            fork_ply=3,
        ))


def test_save_rejects_zero_fork_ply(store):
    _save_parent(store)
    with pytest.raises(AssertionError):
        _save_child(store, "gid-parent", ply=0)


def test_remove_parent_blocked_by_refs(store):
    h_parent = _save_parent(store)
    _save_child(store, "gid-parent", ply=5)
    result = _run(store.remove(h_parent))
    assert result.status is RemoveStatus.BLOCKED_BY_REFS
    assert len(result.children) == 1
    assert result.children[0]["game_id"] == "gid-child"
    assert result.children[0]["fork_ply"] == 5
    # Parent row + refs untouched.
    parent_row = store.get(h_parent)[0]
    assert parent_row["refs"] == [{"game_id": "gid-child", "fork_ply": 5}]


def test_remove_child_scrubs_parent_ref(store):
    h_parent = _save_parent(store)
    h_child = _save_child(store, "gid-parent", ply=5)
    result = _run(store.remove(h_child))
    assert result.status is RemoveStatus.DELETED
    parent_row = store.get(h_parent)[0]
    assert parent_row["refs"] == []


def test_children_of_returns_sorted_by_fork_ply(store):
    _save_parent(store)
    _save_child(store, "gid-parent", ply=10, gid="gid-c1", text="1. d4 d5 *")
    _save_child(store, "gid-parent", ply=3,  gid="gid-c2", text="1. c4 c5 *")
    _save_child(store, "gid-parent", ply=7,  gid="gid-c3", text="1. Nf3 Nf6 *")
    children = store.children_of("gid-parent")
    assert [c["fork_ply"] for c in children] == [3, 7, 10]
    assert [c["game_id"] for c in children] == ["gid-c2", "gid-c3", "gid-c1"]


def test_children_of_unknown_parent_returns_empty(store):
    assert store.children_of("nope") == []


def test_evict_skips_rows_with_nonempty_refs(tmp_path):
    """Eviction must honor the dict-shaped refs (not just legacy lists)."""
    store = RecentImports.load(
        root=tmp_path / "imports", cap=2, active_game_id=lambda: None,
    )
    h_parent = _save_parent(store)
    time.sleep(0.002)
    _save_child(store, "gid-parent", ply=5)
    time.sleep(0.002)
    # Two more inserts push past cap=2; parent should be pinned via refs.
    _run(store.save(fmt="pgn", text="1. Nf3 *", summary="s", game_id="g3"))
    time.sleep(0.002)
    _run(store.save(fmt="pgn", text="1. c4 *", summary="s", game_id="g4"))
    # Parent still present.
    assert store.get(h_parent) is not None


def test_dangling_parent_scrubbed(tmp_path, caplog):
    """Child whose parent_game_id is no longer resolvable gets the link
    scrubbed by scrub_dangling_parent and a warning is logged."""
    store = RecentImports.load(root=tmp_path / "imports", cap=5)
    _save_parent(store)
    _save_child(store, "gid-parent", ply=5)
    # Forcibly drop the parent from the reverse map to simulate
    # corruption (the public API would refuse via refs).
    store._by_id.pop("gid-parent", None)
    with caplog.at_level("WARNING"):
        scrubbed = _run(store.scrub_dangling_parent("gid-child"))
    assert scrubbed is True
    assert any("dangling_parent" in r.message for r in caplog.records)
    child_row = store.get_by_id("gid-child")[0]
    assert "parent_game_id" not in child_row
    assert "fork_ply" not in child_row


def test_replace_at_preserves_fork_link(store):
    """Annotation-edit on a child must not drop parent_game_id/fork_ply."""
    _save_parent(store)
    h_child = _save_child(store, "gid-parent", ply=5)
    # Edit the child's content via replace_at (simulating annotation edit).
    new_hash = _run(store.replace_at(
        old_hash=h_child, fmt="pgn",
        text="1. d4 d5 2. Nf3 *", summary={"white": "C"},
        game_id="gid-child",
    ))
    row, _ = store.get(new_hash)
    assert row["parent_game_id"] == "gid-parent"
    assert row["fork_ply"] == 5


def test_replace_at_preserves_refs_on_parent_edit(store):
    """Annotation-edit on a parent must not drop its children's refs."""
    h_parent = _save_parent(store)
    _save_child(store, "gid-parent", ply=5)
    # Edit the parent's content.
    new_hash = _run(store.replace_at(
        old_hash=h_parent, fmt="pgn",
        text="1. e4 e5 2. Nf3 Nc6 *", summary={"white": "P"},
        game_id="gid-parent",
    ))
    row, _ = store.get(new_hash)
    assert row["refs"] == [{"game_id": "gid-child", "fork_ply": 5}]
