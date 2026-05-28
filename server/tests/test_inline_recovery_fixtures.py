"""Fixture-driven tests for inline tool-call recovery.

Each JSON file under fixtures/inline_recovery/ is a wild sample:
  - `tool_names`: registry passed to the recovery wrapper.
  - `text`: raw model output (single string).
  - `splits`: optional chunk-boundary offsets into `text`. Empty list
    means deliver as one chunk.
  - `expected`: list of {kind, text?, tool_name?, tool_input?} for
    each emitted ProviderChunk (tool_use_id is ignored -- it's a UUID).

Tests run under the v2 implementation only; legacy doesn't recognize
the newer flavors these fixtures cover.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from sturddle_view.llm.base import ProviderChunk
from sturddle_view.llm.inline_recovery import recover_inline_tool_calls_v2

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "inline_recovery"


def _load_fixtures() -> List[Path]:
    return sorted(_FIXTURE_DIR.glob("*.json"))


def _split_text(text: str, splits: List[int]) -> List[str]:
    if not splits:
        return [text]
    boundaries = [0] + list(splits) + [len(text)]
    return [text[a:b] for a, b in zip(boundaries, boundaries[1:]) if a < b]


async def _from_iter(chunks):
    for c in chunks:
        yield c


async def _collect(it):
    out = []
    async for c in it:
        out.append(c)
    return out


def _chunk_to_dict(c: ProviderChunk) -> Dict[str, Any]:
    """Normalize a ProviderChunk to the shape used in fixtures.
    `tool_use_id` is intentionally dropped -- it's a UUID per call."""
    d: Dict[str, Any] = {"kind": c.kind}
    if c.text:
        d["text"] = c.text
    if c.tool_name:
        d["tool_name"] = c.tool_name
    if c.tool_input:
        d["tool_input"] = c.tool_input
    return d


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture_path", _load_fixtures(), ids=lambda p: p.stem)
async def test_fixture(fixture_path: Path):
    spec = json.loads(fixture_path.read_text(encoding="utf-8"))
    chunks = [
        ProviderChunk(kind="text", text=piece)
        for piece in _split_text(spec["text"], spec.get("splits", []))
    ]
    out = await _collect(
        recover_inline_tool_calls_v2(
            _from_iter(chunks),
            tool_names=set(spec.get("tool_names", [])),
        )
    )
    got = [_chunk_to_dict(c) for c in out]
    assert got == spec["expected"], (
        f"Fixture {fixture_path.name} mismatch.\n"
        f"Expected: {json.dumps(spec['expected'], indent=2)}\n"
        f"Got:      {json.dumps(got, indent=2)}"
    )
