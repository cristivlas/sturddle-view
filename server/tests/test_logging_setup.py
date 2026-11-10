import logging

from sturddle_view.logging_setup import _TokenRedactingFormatter


def _format(msg: str) -> str:
    record = logging.LogRecord("sturddle_view", logging.INFO, __file__, 1, msg, (), None)
    return _TokenRedactingFormatter("%(message)s").format(record)


def test_file_formatter_masks_token_in_entry_url():
    assert _format("open: http://127.0.0.1:8765/auth?token=s3cr3t-_x") == (
        "open: http://127.0.0.1:8765/auth?token=***"
    )


def test_file_formatter_masks_token_among_other_params():
    assert _format("GET /auth?a=1&token=s3cr3t&b=2") == "GET /auth?a=1&token=***&b=2"


def test_file_formatter_leaves_other_text_alone():
    msg = "thinking budget tokens=4096"
    assert _format(msg) == msg
