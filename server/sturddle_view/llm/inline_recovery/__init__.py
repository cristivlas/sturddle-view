"""Flavor-registry rewrite of inline tool-call recovery.

Built in parallel with the legacy implementation in
`..inline_tool_calls`. Selected at runtime via SV_AI_INLINE_RECOVERY:
  "legacy" (default) -- flat state machine in inline_tool_calls.py
  "v2"               -- flavor-registry impl built here

Once parity with the legacy impl is reached for the full test corpus,
the dispatcher's default flips and the legacy code is deleted.

`recover_inline_tool_calls_v2` is currently a delegating placeholder;
real flavors will replace it incrementally.
"""
from __future__ import annotations

import logging
import os
from typing import AsyncIterator, Iterable

from ..base import ProviderChunk
from ..inline_tool_calls import _recover_inline_tool_calls_legacy

log = logging.getLogger(__name__)

_IMPL_LEGACY = "legacy"
_IMPL_V2 = "v2"
_IMPL_ENV_VAR = "SV_AI_INLINE_RECOVERY"


def recover_inline_tool_calls_v2(
    upstream: AsyncIterator[ProviderChunk],
    *,
    tool_names: Iterable[str] | None = None,
) -> AsyncIterator[ProviderChunk]:
    return _recover_inline_tool_calls_legacy(upstream, tool_names=tool_names)


def recover_inline_tool_calls(
    upstream: AsyncIterator[ProviderChunk],
    *,
    tool_names: Iterable[str] | None = None,
) -> AsyncIterator[ProviderChunk]:
    impl = os.environ.get(_IMPL_ENV_VAR, _IMPL_LEGACY).strip().lower()
    if impl == _IMPL_V2:
        return recover_inline_tool_calls_v2(upstream, tool_names=tool_names)
    if impl != _IMPL_LEGACY:
        log.warning("unknown %s=%r; falling back to %r", _IMPL_ENV_VAR, impl, _IMPL_LEGACY)
    return _recover_inline_tool_calls_legacy(upstream, tool_names=tool_names)


__all__ = ["recover_inline_tool_calls", "recover_inline_tool_calls_v2"]
