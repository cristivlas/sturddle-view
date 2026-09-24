"""PGN tag-pair names the app reads or writes."""
from __future__ import annotations

TAG_EVENT = "Event"
TAG_SITE = "Site"
TAG_DATE = "Date"
TAG_ROUND = "Round"
TAG_WHITE = "White"
TAG_BLACK = "Black"
TAG_RESULT = "Result"
TAG_TERMINATION = "Termination"
TAG_TIME_CONTROL = "TimeControl"
TAG_ECO = "ECO"
TAG_OPENING = "Opening"
TAG_FEN = "FEN"
TAG_ANNOTATOR = "Annotator"

# PGN's placeholder for an unknown tag value (e.g. a player's name).
UNKNOWN_TAG_VALUE = "?"
