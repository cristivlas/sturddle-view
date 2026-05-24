"""LLM provider abstractions for AI analysis & commentary.

Lifted/adapted from cluesmith's `server/llm.py`. Two real providers
(Anthropic, Ollama-via-OpenAI-compatible) and a canned-response provider
used by the walking-skeleton spike and by tests as the mock boundary.
"""
from __future__ import annotations

from .base import LLMProvider, Message, ProviderChunk, ToolWireSpec
from .canned import CannedProvider
from .scripted import ScriptedProvider
from .tools import ToolRegistry, ToolSpec, UnknownToolError

__all__ = [
    "LLMProvider",
    "Message",
    "ProviderChunk",
    "ToolWireSpec",
    "CannedProvider",
    "ScriptedProvider",
    "ToolRegistry",
    "ToolSpec",
    "UnknownToolError",
]
