"""End-to-end test on a real demo. Skipped when the demo file is not there.

Get the public test demo once (60 MB, from the demoparser2 repository):

    git clone --depth 1 https://github.com/LaihoE/demoparser tmp_demoparser
    copy tmp_demoparser/src/parser/test_demo.dem  ->  tests/data/test_demo.dem

Run with:  python tests/test_integration.py     (or: python -m pytest)

The numbers below were produced by Phase 1 and agree exactly with the demo's
own end-of-match scoreboard. If a change makes this test fail, the change
broke the parsing.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEMO = Path(os.environ.get("CS2REVIEW_TEST_DEMO", Path(__file__).parent / "data" / "test_demo.dem"))

# steamid -> (kills, deaths, assists, damage) as shown by the game itself
SCOREBOARD = {
    "76561197964020430": (11, 5, 4, 1219),
    "76561198265366770": (11, 4, 1, 1163),
    "76561198244754626": (10, 5, 1, 954),
    "76561198118803912": (5, 7, 2, 572),
    "76561198202353993": (4, 2, 0, 315),
    "76561198258044111": (8, 8, 1, 783),
    "76561198194694750": (6, 7, 2, 754),
    "76561198280975787": (4, 9, 0, 344),
    "76561198073049527": (3, 8, 3, 529),
    "76561198324843075": (2, 9, 3, 427),
}
TEAM_A = {"76561197964020430", "76561198265366770", "76561198244754626", "76561198118803912", "76561198202353993"}


def test_full_pipeline_on_public_demo():
    if not DEMO.exists():
        print(f"skipped: {DEMO} not found (see the top of this file)")
        return

    from cs2review.check import check_match, scoreboard
    from cs2review.db import get_engine, load_place_cells, save_match
    from cs2review.parse import parse_demo

    tables = parse_demo(DEMO, team_steam_ids=sorted(TEAM_A), match_id="public_test")

    sizes = {name: len(table) for name, table in tables.items()}
    assert sizes["rounds"] == 10 and sizes["player_rounds"] == 90
    assert sizes["kills"] == 64 and sizes["damages"] == 264
    assert sizes["grenades"] == 150 and sizes["blinds"] == 72
    assert sizes["bomb_events"] == 10 and sizes["positions"] == 4533

    match = tables["matches"].iloc[0]
    assert match["map"] == "de_mirage" and match["tick_rate"] == 64
    assert (match["score_a"], match["score_b"]) == (8, 2)
    assert (match["demo_score_a"], match["demo_score_b"]) == (8, 2)
    assert match["rounds_played"] == 9 and match["us_team"] == "A"

    rounds = tables["rounds"]
    assert list(rounds["winner_team"]) == ["A", "B", "A", "A", "B", "A", "A", "A", "A", "A"]
    assert list(rounds["reason"]) == [
        "ct_eliminated", "bomb_defused", "ct_eliminated", "ct_eliminated", "t_eliminated",
        "bomb_exploded", "ct_eliminated", "bomb_exploded", "ct_eliminated", "ct_surrender",
    ]
    assert list(rounds["plant_site"].fillna("-")) == ["B", "A", "A", "B", "-", "A", "A", "B", "-", "-"]
    assert not bool(rounds["played"].iloc[-1])  # the surrender round never went live

    grenades = tables["grenades"]
    assert grenades["type"].value_counts().to_dict() == {"flash": 48, "smoke": 40, "he": 34, "molotov": 28}
    assert int(grenades["det_tick"].isna().sum()) == 3  # three molotovs never ignited

    players = tables["players"].set_index("steamid")
    assert set(players.index[players["team"] == "A"]) == TEAM_A
    assert int(players["is_us"].sum()) == 5

    with tempfile.TemporaryDirectory() as tmp:
        engine = get_engine(Path(tmp) / "test.sqlite")
        save_match(engine, tables)
        ok, findings = check_match(engine, "public_test")
        assert ok, findings
        board = scoreboard(engine, "public_test").set_index("steamid")
        for steamid, (kills, deaths, assists, damage) in SCOREBOARD.items():
            row = board.loc[steamid]
            assert (row["K"], row["D"], row["A"], row["damage"]) == (kills, deaths, assists, damage), steamid
        assert len(load_place_cells(engine, "de_mirage")) > 1000
        engine.dispose()


if __name__ == "__main__":
    test_full_pipeline_on_public_demo()
    print("ok   test_full_pipeline_on_public_demo")
