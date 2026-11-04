"""Playbook catalog: plan text on disk (docs/ai-playbook-matchers-spec.md).

TOML files of entries keyed by tags, plus formation patterns (Stage 2).
Shipped files live next to this module; user files under the app config
dir override shipped text by id or add entries and patterns. A bad
shipped file raises; a bad user entry or pattern is logged and skipped.
"""
from __future__ import annotations

import functools
import logging
import string
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import app_config_dir
from ..env_utils import env_path
from .formations import FormationMatcher, Pattern, PatternError, example_misses, parse_pattern
from .matchers import REGISTRY, Matcher
from .playbook_tags import (
    AXIS_BISHOPS,
    AXIS_FORMATION,
    AXIS_IMBALANCE,
    AXIS_MARGIN,
    AXIS_PHASE,
    AXIS_STRUCTURE,
    BASE_MARGINS,
    MARGIN_EVEN,
    PHASES,
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
COMBO_PARTNERS = frozenset({AXIS_BISHOPS, AXIS_PHASE, AXIS_STRUCTURE})

# Margins with no fragment by design: level, or reached by normalization.
_NO_FRAGMENT = frozenset({(AXIS_MARGIN, MARGIN_EVEN)})

_META_KEY = "meta"
_ENTRY_KEY = "entry"
_PATTERN_KEY = "pattern"
_META_FIELDS = frozenset({"source", "trust"})
_ENTRY_FIELDS = frozenset({"id", "kind", "when", "text"})

When = tuple[tuple[str, str], ...]
Vocabulary = Mapping[str, frozenset[str]]


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


def compose(patterns: tuple[Pattern, ...]) -> tuple[Matcher, ...]:
    """The static registry with the formation matcher after `imbalance`."""
    composed: list[Matcher] = []
    for matcher in REGISTRY:
        composed.append(matcher)
        if AXIS_IMBALANCE in matcher.tags:
            composed.append(FormationMatcher(patterns))
    return tuple(composed)


def _vocabulary(matchers: Sequence[Matcher]) -> Vocabulary:
    """Axis -> tags a `when` may name: base tags plus every matcher's."""
    vocab = {AXIS_MARGIN: BASE_MARGINS, AXIS_PHASE: PHASES}
    for matcher in matchers:
        vocab.update(matcher.tags)
    return vocab


def _when(pairs: Mapping[str, str]) -> When:
    return tuple(sorted(pairs.items()))


def _fragment_key(axis: str, tag: str) -> tuple[str, When]:
    return (KIND_FRAGMENT, _when({axis: tag}))


@dataclass(frozen=True)
class Catalog:
    entries: Mapping[str, Entry]
    # Load order: shipped, then user; ties in the formation matcher follow it.
    patterns: tuple[Pattern, ...] = ()

    @functools.cached_property
    def _by_key(self) -> Mapping[tuple[str, When], Entry]:
        return {e.key: e for e in self.entries.values() if e.key is not None}

    @functools.cached_property
    def matchers(self) -> tuple[Matcher, ...]:
        """The composed registry; its order is render order."""
        return compose(self.patterns)

    @functools.cached_property
    def specific_axes(self) -> tuple[str, ...]:
        """Specific-tag axes in render order; structure renders by its own rule."""
        return tuple(
            axis for matcher in self.matchers for axis in matcher.tags if axis != AXIS_STRUCTURE
        )

    @functools.cached_property
    def vocabulary(self) -> Vocabulary:
        return _vocabulary(self.matchers)

    def text(self, entry_id: str) -> str:
        return self.entries[entry_id].text

    def fragment(self, axis: str, tag: str) -> str | None:
        entry = self._by_key.get(_fragment_key(axis, tag))
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
            for axis, tags in self.vocabulary.items()
            for tag in sorted(tags)
            if (axis, tag) not in _NO_FRAGMENT and self.fragment(axis, tag) is None
        ]
        return gaps + [note for note in NOTE_FIELDS if note not in self.entries]


def _check_when(kind: str, when: When, vocab: Vocabulary) -> None:
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
        raise CatalogError("a combo's `when` is margin plus bishops, phase or structure")
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


@dataclass(frozen=True, slots=True)
class _File:
    """One parsed playbook file; tables are validated later, per pass."""
    name: str
    meta: Mapping[str, str]
    entries: list
    patterns: list


def _read_file(path: Path, default_source: str, origins: frozenset[str]) -> _File:
    """Meta defaults by location, and `source` must be one of `origins`."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CatalogError(f"{path.name}: {exc}") from exc
    unknown = set(data) - {_META_KEY, _ENTRY_KEY, _PATTERN_KEY}
    if unknown:
        raise CatalogError(f"{path.name}: unknown tables {sorted(unknown)}")
    raw_meta = data.get(_META_KEY, {})
    entries = data.get(_ENTRY_KEY, [])
    patterns = data.get(_PATTERN_KEY, [])
    if not isinstance(raw_meta, dict) or not isinstance(entries, list) or not isinstance(
        patterns, list,
    ):
        raise CatalogError(
            f"{path.name}: `meta` is a table, `entry` and `pattern` arrays of tables",
        )
    meta = {"source": default_source, "trust": TRUST_TRUSTED, **raw_meta}
    if set(meta) - _META_FIELDS:
        raise CatalogError(f"{path.name}: unknown meta {sorted(set(meta) - _META_FIELDS)}")
    if meta["source"] not in origins or meta["trust"] not in _TRUSTS:
        raise CatalogError(f"{path.name}: bad meta {meta}")
    return _File(path.name, meta, entries, patterns)


def _entry(raw: dict, meta: Mapping[str, str], base: Entry | None, vocab: Vocabulary) -> Entry:
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
    _check_when(kind, when, vocab)
    text = raw.get("text")
    _check_text(entry_id, kind, text)
    return Entry(entry_id, kind, when, text, meta["source"], meta["trust"])


def _add(entries: dict[str, Entry], entry: Entry) -> None:
    keys = {e.key: e.id for e in entries.values() if e.key is not None}
    if entry.key is not None and entry.key in keys:
        raise CatalogError(f"{entry.id}: same `when` as {keys[entry.key]}")
    entries[entry.id] = entry


def _shipped_patterns(files: Sequence[_File]) -> tuple[Pattern, ...]:
    patterns: list[Pattern] = []
    for file in files:
        for raw in file.patterns:
            try:
                pattern = parse_pattern(raw)
            except PatternError as exc:
                raise CatalogError(f"{file.name}: {exc}") from exc
            if any(p.id == pattern.id for p in patterns):
                raise CatalogError(f"{file.name}: duplicate pattern {pattern.id!r}")
            patterns.append(pattern)
    misses = example_misses(FormationMatcher(tuple(patterns)))
    if misses:
        raise CatalogError(f"pattern examples mis-tagged: {misses}")
    return tuple(patterns)


def _shipped_entries(files: Sequence[_File], vocab: Vocabulary) -> dict[str, Entry]:
    entries: dict[str, Entry] = {}
    for file in files:
        for raw in file.entries:
            entry = _entry(raw, file.meta, None, vocab)
            if entry.id in entries:
                raise CatalogError(f"{file.name}: duplicate id {entry.id!r}")
            _add(entries, entry)
    return entries


def _user_files(user_dir: Path | None) -> list[_File]:
    if user_dir is None or not user_dir.is_dir():
        return []
    files = []
    for path in sorted(user_dir.glob(_FILE_GLOB)):
        try:
            files.append(_read_file(path, ORIGIN_USER, _USER_ORIGINS))
        except CatalogError as exc:
            log.warning("playbook: skipping %s", exc)
    return files


def _add_user_patterns(
    patterns: tuple[Pattern, ...], files: Sequence[_File],
) -> tuple[Pattern, ...]:
    """Each user pattern in load order, kept only when every example,
    shipped and user, still yields its own tag with it added."""
    for file in files:
        for raw in file.patterns:
            try:
                pattern = parse_pattern(raw)
                if any(p.id == pattern.id for p in patterns):
                    raise PatternError(f"{pattern.id}: a pattern id cannot be redefined")
                candidate = (*patterns, pattern)
                misses = example_misses(FormationMatcher(candidate))
                if misses:
                    raise PatternError(f"{pattern.id}: examples mis-tagged: {misses}")
                patterns = candidate
            except PatternError as exc:
                log.warning("playbook: skipping pattern in %s: %s", file.name, exc)
    return patterns


def _merge_user_entries(
    entries: dict[str, Entry], files: Sequence[_File], vocab: Vocabulary,
) -> None:
    shipped = dict(entries)
    seen: set[str] = set()
    for file in files:
        for raw in file.entries:
            try:
                entry_id = raw.get("id") if isinstance(raw, dict) else None
                if entry_id in seen:
                    raise CatalogError(f"duplicate id {entry_id!r}")
                base = shipped.get(entry_id)
                entry = _entry(raw, file.meta, base, vocab)
                if base is None:
                    _add(entries, entry)
                else:
                    entries[entry.id] = entry
                seen.add(entry.id)
            except CatalogError as exc:
                log.warning("playbook: skipping entry in %s: %s", file.name, exc)


def _drop_uncovered(
    patterns: tuple[Pattern, ...], user_from: int, entries: dict[str, Entry],
) -> tuple[Pattern, ...]:
    """User patterns (from index `user_from`) need both fragments; one
    missing either is dropped with any fragment that names it."""
    keys = {e.key for e in entries.values()}
    kept = list(patterns[:user_from])
    for pattern in patterns[user_from:]:
        tags = (pattern.tag_ours, pattern.tag_theirs)
        needed = {_fragment_key(AXIS_FORMATION, tag) for tag in tags}
        if needed <= keys:
            kept.append(pattern)
            continue
        log.warning("playbook: skipping pattern %s: needs fragments for %s", pattern.id, tags)
        for entry_id in [i for i, e in entries.items() if e.key in needed]:
            del entries[entry_id]
    return tuple(kept)


def load_catalog(shipped_dir: Path, user_dir: Path | None) -> Catalog:
    """Passes over all files, shipped then user: patterns, entries, then
    coverage. Raises CatalogError on a bad shipped file; bad user tables
    are skipped and logged."""
    shipped_files = [
        _read_file(path, ORIGIN_SHIPPED, _ORIGINS) for path in sorted(shipped_dir.glob(_FILE_GLOB))
    ]
    user_files = _user_files(user_dir)
    shipped_patterns = _shipped_patterns(shipped_files)
    entries = _shipped_entries(shipped_files, _vocabulary(compose(shipped_patterns)))
    patterns = _add_user_patterns(shipped_patterns, user_files)
    _merge_user_entries(entries, user_files, _vocabulary(compose(patterns)))
    patterns = _drop_uncovered(patterns, len(shipped_patterns), entries)
    return Catalog(entries, patterns)


def default_user_dir() -> Path:
    return env_path("SV_AI_PLAYBOOK_DIR", app_config_dir() / _USER_SUBDIR)


@functools.cache
def default_catalog() -> Catalog:
    """The process catalog, loaded on first use."""
    return load_catalog(SHIPPED_DIR, default_user_dir())
