// Shared event-kind constants for the live game/AI WebSocket stream.
// Server emits these verbatim; treat as a wire-protocol enum.
// Cross-ref: server/sturddle_view/events.py (EVT_*, EventKind, ENVELOPE_*).
// Tournament kinds live in tournament-events.js; window-level UI
// CustomEvent names live in app-events.js.

export const KIND = {
  ENGINE_INFO:           "engine_info",
  ENGINE_SEARCH_START:   "engine_search_start",
  BOARD_UPDATE:          "board_update",
  CLOCK_TICK:            "clock_tick",
  GAME_RESULT:           "game_result",
  AI_INFO:               "ai_info",
  AI_THINKING:           "ai_thinking",
  AI_TOOL_CALL:          "ai_tool_call",
  AI_TOOL_CALL_FAILED:   "ai_tool_call_failed",
  AI_TOOL_CALL_COMPLETE: "ai_tool_call_complete",
  AI_RECOMMENDATION:     "ai_recommendation",
  AI_POSITION_NOTE:      "ai_position_note",
};

// AI-stream kinds share this prefix; play.js buffers them during replay.
export const AI_KIND_PREFIX = "ai_";
