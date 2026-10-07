// Plain-text helpers shared across UI modules.

const WHITESPACE_RUN_RE = /\s+/g;

// Every whitespace run (newlines included) becomes one space; trimmed.
export function collapseWhitespace(text) {
  return text.replace(WHITESPACE_RUN_RE, " ").trim();
}
