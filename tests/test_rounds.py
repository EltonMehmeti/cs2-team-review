"""Tests that need no demo file.

Run with:  python tests/test_rounds.py     (or: python -m pytest)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from cs2review.parse import _build_rounds, _RoundClock
from cs2review.rounds import select_final_rounds
from cs2review.unpack import match_id_for


def test_normal_match():
    ends = [(1000, 0), (2000, 1), (3000, 2)]
    assert select_final_rounds(ends) == [(1000, 1), (2000, 2), (3000, 3)]


def test_knife_round_before_restart():
    # knife round ends with counter 0, then the match restarts and counts from 0 again
    ends = [(500, 0), (1500, 0), (2500, 1), (3500, 2)]
    assert select_final_rounds(ends) == [(1500, 1), (2500, 2), (3500, 3)]


def test_two_restarts():
    ends = [(100, 0), (200, 1), (900, 0), (1500, 0), (2500, 1)]
    assert select_final_rounds(ends) == [(1500, 1), (2500, 2)]


def test_round_replayed_after_backup_restore():
    # rounds 3 and 4 were played, then the server went back and round 3 was replayed
    ends = [(1000, 0), (2000, 1), (3000, 2), (4000, 3), (5000, 2), (6000, 3), (7000, 4)]
    assert select_final_rounds(ends) == [(1000, 1), (2000, 2), (5000, 3), (6000, 4), (7000, 5)]


def test_demo_starts_mid_match():
    ends = [(1000, 5), (2000, 6)]
    assert select_final_rounds(ends) == [(1000, 6), (2000, 7)]


def test_empty_and_duplicates():
    assert select_final_rounds([]) == []
    assert select_final_rounds([(1000, 0), (1000, 0), (2000, 1)]) == [(1000, 1), (2000, 2)]


def test_match_id():
    assert match_id_for(Path("1-abc-1-1.dem.zst")) == "1-abc-1-1"
    assert match_id_for(Path("1-abc-1-1.dem.gz")) == "1-abc-1-1"
    assert match_id_for(Path("match730_003.dem.bz2")) == "match730_003"
    assert match_id_for(Path("My Match.DEM")) == "My Match"


def _events(round_starts, freeze_ends, round_ends, match_end=None):
    """Build the event tables _build_rounds expects. round_ends: (tick, counter, winner, message)."""
    return {
        "round_start": pd.DataFrame({"tick": round_starts}),
        "round_freeze_end": pd.DataFrame({"tick": freeze_ends}),
        "round_end": pd.DataFrame(
            {
                "tick": [e[0] for e in round_ends],
                "total_rounds_played": [e[1] for e in round_ends],
                "winner": [e[2] for e in round_ends],
                "message": [e[3] for e in round_ends],
                "is_warmup_period": [False] * len(round_ends),
                "game_time": [e[0] / 64 for e in round_ends],
            }
        ),
        "cs_win_panel_match": pd.DataFrame({"tick": [match_end]} if match_end else {"tick": []}),
    }


def test_build_rounds_skips_knife_round_and_windows_are_right():
    t_win, ct_win = "#SFUI_Notice_Terrorists_Win", "#SFUI_Notice_CTs_Win"
    ev = _events(
        round_starts=[100, 3000, 9000, 15000],          # knife, round 1, round 2, round 3 (never finished)
        freeze_ends=[200, 4000, 10000],
        round_ends=[(2500, 0, 3, ct_win), (8500, 0, 2, t_win), (14000, 1, 3, ct_win)],
        match_end=14500,
    )
    rounds, tick_rate = _build_rounds(ev)
    assert tick_rate == 64
    assert list(rounds["round"]) == [1, 2]
    assert list(rounds["start_tick"]) == [3000, 9000]
    assert list(rounds["freeze_end_tick"]) == [4000, 10000]
    assert list(rounds["window_end_tick"]) == [8999, 14999]
    assert list(rounds["winner_side"]) == ["T", "CT"]
    assert list(rounds["reason"]) == ["ct_eliminated", "t_eliminated"]
    assert list(rounds["played"]) == [True, True]

    clock = _RoundClock(rounds, tick_rate)
    got = clock.round_of(pd.Series([150, 2400, 3000, 8600, 8999, 9000, 14999, 15000]))
    assert [None if pd.isna(v) else int(v) for v in got] == [None, None, 1, 1, 1, 2, 2, None]


def test_surrender_in_freeze_time_is_not_played():
    ev = _events(
        round_starts=[100, 5000],
        freeze_ends=[1000],
        round_ends=[(4000, 0, 2, "#SFUI_Notice_Terrorists_Win"), (5200, 1, 2, "#SFUI_Notice_CTs_Surrender")],
        match_end=5400,
    )
    rounds, _ = _build_rounds(ev)
    assert list(rounds["played"]) == [True, False]
    assert list(rounds["reason"]) == ["ct_eliminated", "ct_surrender"]
    assert int(rounds["window_end_tick"].iloc[-1]) == 5400


if __name__ == "__main__":
    tests = [f for name, f in sorted(globals().items()) if name.startswith("test_") and callable(f)]
    for test in tests:
        test()
        print("ok  ", test.__name__)
    print(f"\n{len(tests)} tests passed")
