"""LLM provider abstractions for AI analysis & commentary.

Two real providers (Anthropic, Ollama-via-OpenAI-compatible) and two
test doubles (CannedProvider, ScriptedProvider) that serve as the
provider-level mock boundary so the coordinator can be tested without
HTTP.
"""
from __future__ import annotations

from .base import LLMProvider, Message, ProviderChunk, ToolWireSpec
from .canned import CannedProvider
from .markdown_strip import strip_markdown_stream
from .openai_compat import TOOL_SIGNATURE_KEY
from .prompts import PromptMode, assemble_system_prompt, build_initial_user_message
from .scripted import ScriptedProvider
from .tools import ToolRegistry, ToolSpec, UnknownToolError
from .transcript import (
    NullTranscript,
    Transcript,
    open_transcript,
    transcript_enabled,
)

__all__ = [
    "LLMProvider",
    "Message",
    "ProviderChunk",
    "PromptMode",
    "TOOL_SIGNATURE_KEY",
    "ToolWireSpec",
    "CannedProvider",
    "ScriptedProvider",
    "ToolRegistry",
    "ToolSpec",
    "UnknownToolError",
    "assemble_system_prompt",
    "build_initial_user_message",
    "strip_markdown_stream",
    "Transcript",
    "NullTranscript",
    "open_transcript",
    "transcript_enabled",
]
