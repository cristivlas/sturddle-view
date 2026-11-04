"""Playbook catalog: plan text on disk (docs/ai-playbook-matchers-spec.md).

TOML files of entries keyed by tags. Shipped files live next to this
module; user files under the app config dir override shipped text by id
or add entries. A bad shipped file raises; a bad user entry is logged
and skipped.
"""
from __future__ import annotations

import functools
import logging
import string
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .. import app_config_dir
from ..env_utils import env_path
from .matchers import REGISTRY
from .playbook_tags import (
    AXIS_MARGIN, AXIS_PHASE, AXIS_STRUCTURE, BASE_MARGINS, MARGIN_EVEN, PHASES,
)

log = logging.getLogger(__name__)

SHIPPED_DIR = Path(__file__).parent / "playbook_data"
_USER_SUBDIR = "playbook"
_FILE_GLOB = "*.toml"

KIND_FRAGMENT = "fragment"
KIND_COMBO = "combo"
KIND_NOTE = "note"
_KINDS = frozenset({KIND_FRAGMENT, KIND_COMBO, KIND_NOTE})

ORIGIN_SHIPPED = "shipped"
ORIGIN_USER = "user"
ORIGIN_RESEARCH = "research"
_ORIGINS = frozenset({ORIGIN_SHIPPED, ORIGIN_USER, ORIGIN_RESEARCH})
# A user-dir file cannot claim to be shipped.
_USER_ORIGINS = frozenset({ORIGIN_USER, ORIGIN_RESEARCH})
TRUST_TRUSTED = "trusted"
TRUST_UNVERIFIED = "unverified"
_TRUSTS = frozenset({TRUST_TRUSTED, TRUST_UNVERIFIED})

# Notes are selected by id from code; each declares its template fields.
NOTE_CRUSHING = "note.crushing"
NOTE_LOST = "note.lost"
NOTE_REPEAT_BEHIND = "note.repeat_behind"
NOTE_REPEAT_AHEAD = "note.repeat_ahead"
NOTE_FIELD_MOVES = "moves"
NOTE_FIELDS: Mapping[str, frozenset[str]] = {
    NOTE_CRUSHING: frozenset(),
    NOTE_LOST: frozenset(),
    NOTE_REPEAT_BEHIND: frozenset({NOTE_FIELD_MOVES}),
    NOTE_REPEAT_AHEAD: frozenset({NOTE_FIELD_MOVES}),
}

# The only partners the renderer looks up next to the margin.
COMBO_PARTNERS = frozenset({AXIS_PHASE, AXIS_STRUCTURE})

# Margins with no fragment by design: level, or reached by normalization.
_NO_FRAGMENT = frozenset({(AXIS_MARGIN, MARGIN_EVEN)})

_META_KEY = "meta"
_ENTRY_KEY = "entry"
_META_FIELDS = frozenset({"source", "trust"})
_ENTRY_FIELDS = frozenset({"id", "kind", "when", "text"})

When = tuple[tuple[str, str], ...]


class CatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Entry:
    id: str
    kind: str
    when: When
    text: str
    source: str
    trust: str

    @property
    def key(self) -> tuple[str, When] | None:
        """Lookup key; notes have none (unique by id)."""
        return None if self.kind == KIND_NOTE else (self.kind, self.when)


def vocabulary() -> dict[str, frozenset[str]]:
    """Axis -> tags a `when` may name: base tags plus every matcher's."""
    vocab = {AXIS_MARGIN: BASE_MARGINS, AXIS_PHASE: PHASES}
    for matcher in REGISTRY:
        vocab.update(matcher.tags)
    return vocab


def _when(pairs: Mapping[str, str]) -> When:
    return tuple(sorted(pairs.items()))


@dataclass(frozen=True)
class Catalog:
    entries: Mapping[str, Entry]

    @functools.cached_property
    def _by_key(self) -> Mapping[tuple[str, When], Entry]:
        return {e.key: e for e in self.entries.values() if e.key is not None}

    def text(self, entry_id: str) -> str:
        return self.entries[entry_id].text

    def fragment(self, axis: str, tag: str) -> str | None:
        entry = self._by_key.get((KIND_FRAGMENT, _when({axis: tag})))
        return None if entry is None else entry.text

    def combo(self, margin: str, axis: str, tag: str) -> str | None:
        entry = self._by_key.get((KIND_COMBO, _when({AXIS_MARGIN: margin, axis: tag})))
        return None if entry is None else entry.text

    def note(self, entry_id: str, **fields: str) -> str:
        return string.Template(self.entries[entry_id].text).substitute(fields)

    def missing(self) -> list[str]:
        """Declared tags with no fragment, and code-selected notes absent."""
        gaps = [
            f"{axis}={tag}"
            for axis, tags in vocabulary().items()
            for tag in sorted(tags)
            if (axis, tag) not in _NO_FRAGMENT and self.fragment(axis, tag) is None
        ]
        return gaps + [note for note in NOTE_FIELDS if note not in self.entries]


def _check_when(kind: str, when: When) -> None:
    vocab = vocabulary()
    for axis, tag in when:
        if axis not in vocab:
            raise CatalogError(f"unknown axis {axis!r}")
        if tag not in vocab[axis]:
            raise CatalogError(f"unknown tag {axis}={tag!r}")
    axes = {axis for axis, _ in when}
    if kind == KIND_FRAGMENT and len(when) != 1:
        raise CatalogError("a fragment's `when` has exactly one key")
    if kind == KIND_COMBO and (
        len(when) != 2 or AXIS_MARGIN not in axes or not axes - {AXIS_MARGIN} <= COMBO_PARTNERS
    ):
        raise CatalogError("a combo's `when` is margin plus phase or structure")
    if kind == KIND_NOTE and when:
        raise CatalogError("a note has no `when`")


def _check_text(entry_id: str, kind: str, text: object) -> None:
    if not isinstance(text, str) or not text.strip():
        raise CatalogError("`text` must be a non-empty string")
    if kind != KIND_NOTE:
        return
    if entry_id not in NOTE_FIELDS:
        raise CatalogError(f"unknown note id {entry_id!r}")
    template = string.Template(text)
    fields = set(template.get_identifiers())
    if not template.is_valid() or not fields <= NOTE_FIELDS[entry_id]:
        raise CatalogError(f"bad template fields {sorted(fields)} for {entry_id!r}")


def _raw_entries(
    path: Path, default_source: str, origins: frozenset[str],
) -> tuple[dict, list[dict]]:
    """(meta, entry tables) of one file; meta defaults by location, and
    `source` must be one of `origins`."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CatalogError(f"{path.name}: {exc}") from exc
    unknown = set(data) - {_META_KEY, _ENTRY_KEY}
    if unknown:
        raise CatalogError(f"{path.name}: unknown tables {sorted(unknown)}")
    raw_meta = data.get(_META_KEY, {})
    raws = data.get(_ENTRY_KEY, [])
    if not isinstance(raw_meta, dict) or not isinstance(raws, list):
        raise CatalogError(f"{path.name}: `meta` is a table, `entry` an array of tables")
    meta = {"source": default_source, "trust": TRUST_TRUSTED, **raw_meta}
    if set(meta) - _META_FIELDS:
        raise CatalogError(f"{path.name}: unknown meta {sorted(set(meta) - _META_FIELDS)}")
    if meta["source"] not in origins or meta["trust"] not in _TRUSTS:
        raise CatalogError(f"{path.name}: bad meta {meta}")
    return meta, raws


def _entry(raw: dict, meta: dict, base: Entry | None) -> Entry:
    """Validated entry from one table. `base` is the shipped entry an
    override replaces: kind and when, if given, must match it."""
    if not isinstance(raw, dict) or set(raw) - _ENTRY_FIELDS:
        raise CatalogError(f"bad entry fields in {raw!r}")
    entry_id = raw.get("id")
    if not isinstance(entry_id, str) or not entry_id:
        raise CatalogError("entry without an `id`")
    kind = raw.get("kind", KIND_FRAGMENT if base is None else base.kind)
    if kind not in _KINDS:
        raise CatalogError(f"{entry_id}: unknown kind {kind!r}")
    raw_when = raw.get("when", {})
    if not isinstance(raw_when, dict) or not all(isinstance(v, str) for v in raw_when.values()):
        raise CatalogError(f"{entry_id}: `when` maps axes to tag strings")
    when = _when(raw_when)
    if base is not None:
        if kind != base.kind or ("when" in raw and when != base.when):
            raise CatalogError(f"{entry_id}: an override cannot re-key a shipped entry")
        when = base.when
    _check_when(kind, when)
    text = raw.get("text")
    _check_text(entry_id, kind, text)
    return Entry(entry_id, kind, when, text, meta["source"], meta["trust"])


def _add(entries: dict[str, Entry], entry: Entry) -> None:
    keys = {e.key: e.id for e in entries.values() if e.key is not None}
    if entry.key is not None and entry.key in keys:
        raise CatalogError(f"{entry.id}: same `when` as {keys[entry.key]}")
    entries[entry.id] = entry


def _load_shipped(shipped_dir: Path) -> dict[str, Entry]:
    entries: dict[str, Entry] = {}
    for path in sorted(shipped_dir.glob(_FILE_GLOB)):
        meta, raws = _raw_entries(path, ORIGIN_SHIPPED, _ORIGINS)
        for raw in raws:
            entry = _entry(raw, meta, None)
            if entry.id in entries:
                raise CatalogError(f"{path.name}: duplicate id {entry.id!r}")
            _add(entries, entry)
    return entries


def _merge_user(entries: dict[str, Entry], user_dir: Path) -> None:
    shipped = dict(entries)
    seen: set[str] = set()
    for path in sorted(user_dir.glob(_FILE_GLOB)):
        try:
            meta, raws = _raw_entries(path, ORIGIN_USER, _USER_ORIGINS)
        except CatalogError as exc:
            log.warning("playbook: skipping %s", exc)
            continue
        for raw in raws:
            try:
                entry_id = raw.get("id") if isinstance(raw, dict) else None
                if entry_id in seen:
                    raise CatalogError(f"duplicate id {entry_id!r}")
                base = shipped.get(entry_id)
                entry = _entry(raw, meta, base)
                if base is None:
                    _add(entries, entry)
                else:
                    entries[entry.id] = entry
                seen.add(entry.id)
            except CatalogError as exc:
                log.warning("playbook: skipping entry in %s: %s", path.name, exc)


def load_catalog(shipped_dir: Path, user_dir: Path | None) -> Catalog:
    """Shipped entries, then user overrides / additions. Raises
    CatalogError on a bad shipped file; bad user entries are skipped."""
    entries = _load_shipped(shipped_dir)
    if user_dir is not None and user_dir.is_dir():
        _merge_user(entries, user_dir)
    return Catalog(entries)


def default_user_dir() -> Path:
    return env_path("SV_AI_PLAYBOOK_DIR", app_config_dir() / _USER_SUBDIR)


@functools.cache
def default_catalog() -> Catalog:
    """The process catalog, loaded on first use."""
    return load_catalog(SHIPPED_DIR, default_user_dir())
