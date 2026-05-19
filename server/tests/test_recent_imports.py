from __future__ import annotations

import asyncio
import json
import time

import pytest

from sturddle_view.recent_imports import RecentImports, _hash_text


@pytest.fixture
def store(tmp_path):
    return RecentImports.load(root=tmp_path / "imports", cap=5)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


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
    removed = _run(store.remove(h))
    assert removed is True
    assert store.list() == []
    assert not (tmp_path / "imports" / rel).exists()


def test_remove_unknown_returns_false(store):
    assert _run(store.remove("nonexistent")) is False


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
