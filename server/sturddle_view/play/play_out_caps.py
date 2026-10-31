"""play_out caps per turn: ideas, and the model's own moves per idea. Own
module so the prompts can name them without importing the tools."""
from __future__ import annotations

from ..env_utils import env_int

_DEFAULT_MAX_IDEAS = 3
MAX_IDEAS = env_int("SV_AI_MAX_IDEAS", _DEFAULT_MAX_IDEAS, min_value=1)
_DEFAULT_PLAY_OUT_MAX_MOVES = 2
PLAY_OUT_MAX_MOVES = env_int("SV_AI_PLAY_OUT_MAX_MOVES", _DEFAULT_PLAY_OUT_MAX_MOVES, min_value=1)
