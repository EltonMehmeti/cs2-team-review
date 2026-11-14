"""Work out which round_end events are the real, final rounds of a match.

A demo can contain more round_end events than the match has rounds:
  * a knife round before the match goes live (FACEIT does this),
  * rounds played before a restart,
  * rounds that were replayed after the server restored a backup.

The game keeps a counter, total_rounds_played, that is 0 while round 1 is being
played, 1 during round 2, and so on. A restart or a restore sets it back.
So we walk backwards from the last round_end and keep, for each round number,
the latest round_end that fits. Everything else is ignored.
"""

from __future__ import annotations


def select_final_rounds(round_ends: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Pick the final version of each round.

    round_ends: (tick, total_rounds_played) for every non-warmup round_end,
                in any order.
    returns:    (tick, round_number) sorted by tick, round_number starting at 1.
    """
    events = sorted(set(round_ends))
    if not events:
        return []

    accepted: list[tuple[int, int]] = []
    need = events[-1][1]  # counter value of the last round that ended
    for tick, played in reversed(events):
        if need < 0:
            break
        if played == need:
            accepted.append((tick, played + 1))
            need -= 1
    accepted.reverse()

    # If the demo started recording mid-match we never reach round 1.
    # That is fine: we keep what we have, numbered as the game numbered it.
    return accepted
