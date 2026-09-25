"""Tournament template vocabulary: keys of the frozen ``template`` dict
and their enumerated values. A leaf module, so every tournament layer
can import it."""
from __future__ import annotations

# Pins fastchess's -srand for the tournament's lifetime.
SEED_KEY = "seed"
TOURNAMENT_TYPE_KEY = "tournament_type"
TYPE_ROUNDROBIN = "roundrobin"
TYPE_GAUNTLET = "gauntlet"
SPRT_KEY = "sprt"
# Opening book: per-tournament, falling back to the engine defaults.
BOOK_PATH_KEY = "book_path"
BOOK_PLIES_KEY = "book_plies"
BOOK_ORDER_KEY = "book_order"
BOOK_KEYS = (BOOK_PATH_KEY, BOOK_PLIES_KEY, BOOK_ORDER_KEY)

# Resource-check inputs the client folds into the template at create time.
GAMES_IN_PARALLEL_KEY = "games_in_parallel"
MAX_THREADS_KEY = "max_threads"
MAX_HASH_MB_KEY = "max_hash_mb"
PONDER_KEY = "ponder"
PIN_AFFINITY_KEY = "pin_affinity"
# Set via SV_ALLOW_OVERSUBSCRIBE; lets CPU/RAM oversubscription through.
ALLOW_OVERSUBSCRIBE_KEY = "allow_oversubscribe"
