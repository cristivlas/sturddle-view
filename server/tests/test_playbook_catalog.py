"""Playbook catalog (play/playbook_catalog.py) and matcher registry
(play/matchers.py): shipped-file invariants, user overrides, declared tags.
"""
from __future__ import annotations

import itertools
import logging
import random
from pathlib import Path

import chess
import pytest
from fastapi.testclient import TestClient

import sturddle_view.play.playbook_catalog as playbook_catalog
from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from sturddle_view.llm.playbook import render_playbook
from sturddle_view.llm.prompts import COACH_MODE
from sturddle_view.play.matchers import REGISTRY, MatchContext
from sturddle_view.play.playbook import Situation, classify
from sturddle_view.play.playbook_catalog import (
    KIND_COMBO,
    KIND_FRAGMENT,
    NOTE_REPEAT_BEHIND,
    ORIGIN_SHIPPED,
    ORIGIN_USER,
    SHIPPED_DIR,
    TRUST_TRUSTED,
    TRUST_UNVERIFIED,
    CatalogError,
    load_catalog,
)
from sturddle_view.play.playbook_tags import (
    AXIS_IMBALANCE,
    AXIS_MARGIN,
    AXIS_PHASE,
    IMBALANCE_IQP_OURS,
    MARGIN_EVEN,
    MARGIN_WORSE,
    PHASE_MIDDLEGAME,
    PHASES,
    SOURCE_EVAL,
)

from .conftest import REGISTRY_FILE

_IQP_ID = "imbalance.iqp_ours"
_USER_TEXT = "user plan for the isolated pawn."


def _write(dir_: Path, name: str, body: str) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / name
    path.write_text(body, encoding="utf-8")
    return path


def _user(tmp_path: Path, body: str, name: str = "mine.toml") -> Path:
    user_dir = tmp_path / "user"
    _write(user_dir, name, body)
    return user_dir


def _load_user(tmp_path: Path, body: str):
    return load_catalog(SHIPPED_DIR, _user(tmp_path, body))


def _shipped():
    return load_catalog(SHIPPED_DIR, None)


# --- shipped files ---------------------------------------------------------

def test_shipped_catalog_covers_every_declared_tag_and_note():
    assert _shipped().missing() == []


def test_shipped_entries_are_shipped_and_trusted():
    for entry in _shipped().entries.values():
        assert (entry.source, entry.trust) == (ORIGIN_SHIPPED, TRUST_TRUSTED)


def test_shipped_duplicate_id_raises(tmp_path):
    entry = f'[[entry]]\nid = "{_IQP_ID}"\nwhen = {{ imbalance = "iqp_ours" }}\ntext = "x"\n'
    _write(tmp_path, "a.toml", entry)
    _write(tmp_path, "b.toml", entry)
    with pytest.raises(CatalogError, match="duplicate id"):
        load_catalog(tmp_path, None)


def test_shipped_bad_entry_raises(tmp_path):
    _write(tmp_path, "a.toml", '[[entry]]\nid = "x"\nwhen = { wing = "king" }\ntext = "x"\n')
    with pytest.raises(CatalogError, match="unknown axis"):
        load_catalog(tmp_path, None)


# --- matchers --------------------------------------------------------------

def test_registry_has_one_matcher_per_axis():
    axes = [axis for matcher in REGISTRY for axis in matcher.tags]
    assert len(axes) == len(set(axes)) == len(REGISTRY)
    assert set(axes) <= set(Situation.__dataclass_fields__)


def _random_boards(games: int = 20, plies: int = 60):
    rng = random.Random(11)
    for _ in range(games):
        board = chess.Board()
        for _ in range(plies):
            moves = list(board.legal_moves)
            if not moves:
                break
            board.push(rng.choice(moves))
            yield board


def test_matches_stay_inside_declared_tags():
    for board in _random_boards():
        for color, phase in itertools.product(chess.COLORS, sorted(PHASES)):
            ctx = MatchContext(
                board=board, our_color=color, margin=MARGIN_EVEN,
                margin_source=SOURCE_EVAL, phase=phase,
            )
            for matcher in REGISTRY:
                for match in matcher.match(ctx):
                    assert match.tag in matcher.tags[match.axis]


def test_classify_folds_matches_into_fields():
    board = chess.Board("6k1/pp3ppp/4p3/8/3P4/8/PP3PPP/6K1 w - - 0 1")
    situation = classify(board, score_white=None, our_color=chess.WHITE)
    assert situation.matches
    for match in situation.matches:
        assert getattr(situation, match.axis) == match.tag


# --- user overrides --------------------------------------------------------

def test_user_override_replaces_text_with_its_own_provenance(tmp_path):
    catalog = _load_user(tmp_path, (
        '[meta]\ntrust = "unverified"\n\n'
        f'[[entry]]\nid = "{_IQP_ID}"\ntext = "{_USER_TEXT}"\n'
    ))
    entry = catalog.entries[_IQP_ID]
    assert entry.text == _USER_TEXT
    assert (entry.source, entry.trust) == (ORIGIN_USER, TRUST_UNVERIFIED)
    assert catalog.fragment(AXIS_IMBALANCE, IMBALANCE_IQP_OURS) == _USER_TEXT


def test_user_override_cannot_rekey_a_shipped_entry(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        catalog = _load_user(tmp_path, (
            f'[[entry]]\nid = "{_IQP_ID}"\nwhen = {{ imbalance = "iqp_theirs" }}\n'
            f'text = "{_USER_TEXT}"\n'
        ))
    assert catalog.text(_IQP_ID) == _shipped().text(_IQP_ID)
    assert "re-key" in caplog.text


def test_user_override_cannot_change_kind(tmp_path):
    catalog = _load_user(tmp_path, (
        f'[[entry]]\nid = "{_IQP_ID}"\nkind = "{KIND_COMBO}"\ntext = "{_USER_TEXT}"\n'
    ))
    assert catalog.entries[_IQP_ID].kind == KIND_FRAGMENT
    assert catalog.text(_IQP_ID) == _shipped().text(_IQP_ID)


def test_user_new_entry_adds(tmp_path):
    catalog = _load_user(tmp_path, (
        '[[entry]]\nid = "combo.worse_middlegame"\nkind = "combo"\n'
        'when = { margin = "worse", phase = "middlegame" }\ntext = "hold and regroup."\n'
    ))
    assert catalog.combo(MARGIN_WORSE, AXIS_PHASE, PHASE_MIDDLEGAME) == "hold and regroup."


def test_user_new_entry_colliding_with_a_shipped_when_is_skipped(tmp_path):
    catalog = _load_user(tmp_path, (
        f'[[entry]]\nid = "my.iqp"\nwhen = {{ imbalance = "iqp_ours" }}\ntext = "{_USER_TEXT}"\n'
    ))
    assert "my.iqp" not in catalog.entries
    assert catalog.fragment(AXIS_IMBALANCE, IMBALANCE_IQP_OURS) == _shipped().text(_IQP_ID)


@pytest.mark.parametrize("body", [
    # A combo partner the renderer never looks up.
    '[[entry]]\nid = "my.combo"\nkind = "combo"\n'
    'when = { imbalance = "iqp_ours", bishops = "opposite" }\ntext = "x"\n',
    # The extreme margins reach their text through normalization only.
    '[[entry]]\nid = "my.lost"\nwhen = { margin = "lost" }\ntext = "x"\n',
    # Unknown tag, unknown field, empty text.
    '[[entry]]\nid = "my.tag"\nwhen = { imbalance = "hanging" }\ntext = "x"\n',
    '[[entry]]\nid = "my.typo"\nwen = { imbalance = "iqp_ours" }\ntext = "x"\n',
    '[[entry]]\nid = "my.empty"\nwhen = { margin = "worse" }\ntext = " "\n',
    # Notes are selected from code: an unknown note id has no reader.
    '[[entry]]\nid = "note.mine"\nkind = "note"\ntext = "x"\n',
])
def test_bad_user_entries_are_skipped(tmp_path, body):
    catalog = _load_user(tmp_path, body)
    assert catalog.entries.keys() == _shipped().entries.keys()


@pytest.mark.parametrize("text", [
    "repeat $moves and $board",
    "repeat ${moves.__class__}",
])
def test_note_override_with_bad_template_fields_is_skipped(tmp_path, text):
    catalog = _load_user(tmp_path, f'[[entry]]\nid = "{NOTE_REPEAT_BEHIND}"\ntext = "{text}"\n')
    assert catalog.text(NOTE_REPEAT_BEHIND) == _shipped().text(NOTE_REPEAT_BEHIND)


def test_note_braces_are_literal_text(tmp_path):
    # str.format would reach attributes here; string.Template leaves it alone.
    text = "repeat {moves.__class__} with $moves"
    catalog = _load_user(tmp_path, f'[[entry]]\nid = "{NOTE_REPEAT_BEHIND}"\ntext = "{text}"\n')
    assert catalog.note(NOTE_REPEAT_BEHIND, moves="Nf3") == "repeat {moves.__class__} with Nf3"


def test_duplicate_user_id_keeps_the_first_valid_entry(tmp_path):
    # Files load in name order; a rejected entry does not claim its id.
    user_dir = _user(tmp_path, f'[[entry]]\nid = "{_IQP_ID}"\ntext = " "\n', "a.toml")
    _write(user_dir, "b.toml", f'[[entry]]\nid = "{_IQP_ID}"\ntext = "first."\n')
    _write(user_dir, "c.toml", f'[[entry]]\nid = "{_IQP_ID}"\ntext = "second."\n')
    assert load_catalog(SHIPPED_DIR, user_dir).text(_IQP_ID) == "first."


def test_user_file_cannot_claim_shipped_source(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        catalog = _load_user(tmp_path, (
            '[meta]\nsource = "shipped"\n\n'
            f'[[entry]]\nid = "{_IQP_ID}"\ntext = "{_USER_TEXT}"\n'
        ))
    assert catalog.text(_IQP_ID) == _shipped().text(_IQP_ID)
    assert "bad meta" in caplog.text


def test_non_array_entry_rejects_the_file_once(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        catalog = _load_user(tmp_path, 'entry = "oops"\n')
    assert catalog.entries == _shipped().entries
    assert len(caplog.records) == 1


def test_unreadable_user_file_is_skipped(tmp_path, caplog):
    user_dir = _user(tmp_path, "[[entry]\nnot toml", "bad.toml")
    _write(user_dir, "good.toml", f'[[entry]]\nid = "{_IQP_ID}"\ntext = "{_USER_TEXT}"\n')
    with caplog.at_level(logging.WARNING):
        catalog = load_catalog(SHIPPED_DIR, user_dir)
    assert catalog.text(_IQP_ID) == _USER_TEXT
    assert "bad.toml" in caplog.text


def test_missing_user_dir_loads_shipped(tmp_path):
    assert load_catalog(SHIPPED_DIR, tmp_path / "absent").entries == _shipped().entries


def test_render_reads_the_given_catalog(tmp_path):
    catalog = _load_user(tmp_path, f'[[entry]]\nid = "{_IQP_ID}"\ntext = "{_USER_TEXT}"\n')
    situation = Situation(
        margin=MARGIN_WORSE, margin_source=SOURCE_EVAL, phase=PHASE_MIDDLEGAME,
        imbalance=IMBALANCE_IQP_OURS,
    )
    assert _USER_TEXT in render_playbook(situation, COACH_MODE, chess.WHITE, catalog)
    assert _USER_TEXT not in render_playbook(situation, COACH_MODE, chess.WHITE)
    assert catalog.fragment(AXIS_MARGIN, MARGIN_WORSE) in render_playbook(
        situation, COACH_MODE, chess.WHITE, catalog,
    )


def test_bad_shipped_playbook_fails_app_boot(tmp_path, monkeypatch):
    bad = tmp_path / "shipped"
    _write(bad, "a.toml", '[[entry]]\nid = "x"\nwhen = { wing = "king" }\ntext = "x"\n')
    monkeypatch.setattr(playbook_catalog, "SHIPPED_DIR", bad)
    app = create_app(
        settings=Settings(token="t", auth_disabled=True),
        engine_registry=EngineRegistry(path=tmp_path / REGISTRY_FILE),
    )
    with pytest.raises(CatalogError, match="unknown axis"):
        with TestClient(app):
            pass
