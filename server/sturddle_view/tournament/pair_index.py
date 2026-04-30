"""Per-tournament pairing index.

Maintains the mapping ``proxy_id → game_id`` by watching UCI
``position startpos moves ...`` lines from each proxy.

Pairing rule (see ``docs/tournament-spec.md`` "Live observation
pipeline" for design rationale):

  In a UCI game between engines A and B mediated by fastchess, the
  two engines are *never* at exactly the same move list at the same
  instant. The flow is::

      fastchess → A : position ... moves <prev>          (A at ply N)
      A → fastchess : bestmove m
      fastchess → B : position ... moves <prev> m         (B at ply N+1)
      B → fastchess : bestmove m'
      fastchess → A : position ... moves <prev> m m'      (A at ply N+2)
      ...

  So one engine's current move list is always the other's move list
  with one extra move appended. When proxy X reports a new position
  with key K at ply N, we pair X with any other proxy currently at
  ``parent(K)`` — i.e. the move list with the last move removed.

  Special case: at game start both engines are briefly at ``startpos``
  (ply 0) before either has moved; we pair on identical-key-at-ply-0.

The index is purely in-memory; scoped to a single tournament's
lifetime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


def parse_position_line(line: str) -> tuple[str, int] | None:
    """Return ``(key, ply)`` from a ``position`` UCI line, or ``None``
    if the line isn't a position line.

    ``key`` is a string identifying the game state up to the current
    move list. ``ply`` is the half-move count.
    """
    s = line.strip()
    if not s.startswith("position "):
        return None
    rest = s[len("position "):]
    if rest.startswith("startpos"):
        prefix = "startpos"
        tail = rest[len("startpos"):].strip()
    elif rest.startswith("fen "):
        idx = rest.find(" moves")
        if idx == -1:
            prefix = rest
            tail = ""
        else:
            prefix = rest[:idx]
            tail = rest[idx + 1:]
    else:
        return None

    if tail.startswith("moves"):
        moves_str = tail[len("moves"):].strip()
        moves = moves_str.split() if moves_str else []
    else:
        moves = []

    key = prefix + "|" + " ".join(moves)
    return key, len(moves)


def parent_key(key: str) -> str | None:
    """Return the key with one move removed, or ``None`` if there are
    no moves to remove."""
    sep = key.find("|")
    if sep == -1:
        return None
    prefix, moves_str = key[:sep], key[sep + 1:]
    if not moves_str:
        return None
    parts = moves_str.split()
    if not parts:
        return None
    parent_moves = " ".join(parts[:-1])
    return f"{prefix}|{parent_moves}"


@dataclass
class PairIndex:
    """Bookkeeping for proxy → game pairing.

    Lifecycle:
      - ``observe(proxy_id, line)`` for each line a proxy reports.
      - ``game_id_for(proxy_id)`` returns the game id once paired.
      - ``forget_proxy(proxy_id)`` when a proxy session ends.
    """
    # proxy_id → (key, ply): the latest position observed for each
    # unpaired proxy.
    _latest: dict[str, tuple[str, int]] = field(default_factory=dict)
    # Reverse index: key → proxy_id. There is at most one unpaired
    # proxy at a given key at a time.
    _by_key: dict[str, str] = field(default_factory=dict)
    # proxy_id → game_id (paired proxies).
    _paired: dict[str, str] = field(default_factory=dict)
    # game_id → (proxy_id_a, proxy_id_b)
    _games: dict[str, tuple[str, str]] = field(default_factory=dict)
    _next_game: int = 1

    def observe(self, proxy_id: str, line: str) -> str | None:
        """Process one line. If this completes a pairing, return the
        new game_id; otherwise return ``None``.

        Idempotent on already-paired proxies (returns ``None`` and
        skips bookkeeping)."""
        if proxy_id in self._paired:
            return None
        parsed = parse_position_line(line)
        if parsed is None:
            return None
        key, ply = parsed

        partner = self._find_partner(proxy_id, key, ply)

        # Update bookkeeping (whether or not we paired).
        old = self._latest.get(proxy_id)
        if old is not None and self._by_key.get(old[0]) == proxy_id:
            del self._by_key[old[0]]
        self._latest[proxy_id] = (key, ply)
        self._by_key[key] = proxy_id

        if partner is not None:
            return self._lock_pair(proxy_id, partner)
        return None

    def _find_partner(self, proxy_id: str, key: str, ply: int) -> str | None:
        """Look for a proxy that should pair with this observation."""
        # Special case: ply 0 — both engines briefly at the same key.
        if ply == 0:
            holder = self._by_key.get(key)
            if holder and holder != proxy_id and holder not in self._paired:
                return holder

        # General case: pair with the proxy at parent(key).
        if ply > 0:
            pkey = parent_key(key)
            if pkey is not None:
                holder = self._by_key.get(pkey)
                if holder and holder != proxy_id and holder not in self._paired:
                    holder_state = self._latest.get(holder)
                    if holder_state is not None and holder_state[1] == ply - 1:
                        return holder

        # Mirror: this proxy is the parent of someone else. Scan would
        # be O(n); we avoid it by relying on the next observation from
        # that other proxy to trigger the parent-lookup case above.
        # In other words, pairing always completes when the *higher-ply*
        # proxy makes its first qualifying observation.
        return None

    def _lock_pair(self, p1: str, p2: str) -> str:
        game_id = f"g{self._next_game}"
        self._next_game += 1
        self._paired[p1] = game_id
        self._paired[p2] = game_id
        self._games[game_id] = (p1, p2)
        # Remove from candidate bookkeeping.
        for proxy in (p1, p2):
            state = self._latest.pop(proxy, None)
            if state and self._by_key.get(state[0]) == proxy:
                del self._by_key[state[0]]
        return game_id

    def game_id_for(self, proxy_id: str) -> str | None:
        return self._paired.get(proxy_id)

    def proxies_for(self, game_id: str) -> tuple[str, str] | None:
        return self._games.get(game_id)

    def all_games(self) -> dict[str, tuple[str, str]]:
        return dict(self._games)

    def forget_proxy(self, proxy_id: str) -> None:
        """Drop bookkeeping for a proxy that has exited.

        - If unpaired: just remove its entries.
        - If paired: drop the whole pairing — both proxies of an ended
          game lose their binding because the game is over.
        """
        state = self._latest.pop(proxy_id, None)
        if state and self._by_key.get(state[0]) == proxy_id:
            del self._by_key[state[0]]

        game_id = self._paired.pop(proxy_id, None)
        if game_id is not None:
            partner = None
            pair = self._games.get(game_id)
            if pair:
                partner = pair[0] if pair[1] == proxy_id else pair[1]
            self._games.pop(game_id, None)
            if partner is not None:
                self._paired.pop(partner, None)

    def reset(self) -> None:
        self._latest.clear()
        self._by_key.clear()
        self._paired.clear()
        self._games.clear()
        self._next_game = 1


def observe_lines(index: PairIndex, proxy_id: str, lines: Iterable[str]) -> list[str]:
    """Convenience: feed multiple lines, return new game_ids in order."""
    new_games: list[str] = []
    for line in lines:
        gid = index.observe(proxy_id, line)
        if gid is not None:
            new_games.append(gid)
    return new_games
