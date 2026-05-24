"""LLM provider abstractions for AI analysis & commentary.

Lifted/adapted from cluesmith's `server/llm.py`. Two real providers
(Anthropic, Ollama-via-OpenAI-compatible) and a canned-response provider
used by the walking-skeleton spike and by tests as the mock boundary.
"""
from __future__ import annotations

from .base import LLMProvider, Message, ProviderChunk, ToolSpec
from .canned import CannedProvider
from .scripted import ScriptedProvider

__all__ = [
    "LLMProvider",
    "Message",
    "ProviderChunk",
    "ToolSpec",
    "CannedProvider",
    "ScriptedProvider",
]
