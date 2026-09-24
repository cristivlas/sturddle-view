"""Wire shape of structured errors sent to the UI."""
from __future__ import annotations

# ``{error: <code>}``: 409 details and system-event payloads.
ERROR_KEY = "error"
# ``{code, message}``: the UI shows ``message``.
CODE_KEY = "code"
MESSAGE_KEY = "message"
# ``{reason, message, ...details}``: tournament rejections the UI branches on.
REASON_KEY = "reason"


def error_detail(code: str, message: str) -> dict[str, str]:
    return {CODE_KEY: code, MESSAGE_KEY: message}
