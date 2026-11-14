"""End-to-end test on a real demo. Skipped when the demo file is not there.

Get the public test demo once (60 MB, from the demoparser2 repository):

    git clone --depth 1 https://github.com/LaihoE/demoparser tmp_demoparser
    copy tmp_demoparser/src/parser/test_demo.dem  ->  tests/data/test_demo.dem

Run with:  python tests/test_integration.py     (or: python -m pytest)

The numbers below were produced by Phase 1 and agree exactly with the demo's
own end-of-match scoreboard. If a change makes this test fail, the change
broke the parsing.
"""

import json
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

# Phase 2 golden numbers. steamid -> (KAST rounds, opening kills, opening deaths,
# trade kills, traded deaths), out of 9 played rounds
DUELS = {
    "76561197964020430": (9, 2, 0, 1, 2),
    "76561198265366770": (9, 1, 0, 1, 1),
    "76561198244754626": (8, 5, 0, 1, 0),
    "76561198118803912": (7, 0, 1, 2, 1),
    "76561198202353993": (8, 0, 0, 0, 1),
    "76561198258044111": (5, 0, 0, 1, 1),
    "76561198194694750": (4, 0, 2, 2, 0),
    "76561198280975787": (3, 1, 1, 0, 0),
    "76561198073049527": (6, 0, 2, 0, 1),
    "76561198324843075": (6, 0, 3, 1, 2),
}


def test_full_pipeline_on_public_demo():
    if not DEMO.exists():
        print(f"skipped: {DEMO} not found (see the top of this file)")
        return

    from cs2review.check import check_match, format_report, scoreboard
    from cs2review.db import get_engine, load_place_cells, save_match
    from cs2review.metrics import build_match_metrics, check_invariants, load_tables
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
        result = build_match_metrics(engine, "public_test")
        ok, findings = check_match(engine, "public_test")
        assert ok, findings
        board = scoreboard(engine, "public_test").set_index("steamid")
        for steamid, (kills, deaths, assists, damage) in SCOREBOARD.items():
            row = board.loc[steamid]
            assert (row["K"], row["D"], row["A"], row["damage"]) == (kills, deaths, assists, damage), steamid
        assert len(load_place_cells(engine, "de_mirage")) > 1000

        # ---- Phase 2: metrics -------------------------------------------------
        kills = result["kills"]
        live = kills[~kills["after_round_end"]]
        assert len(live) == 62  # kills before the round ended
        assert int(kills["was_traded"].sum()) == 9 and int(kills["is_trade"].sum()) == 9
        openings = kills[kills["is_opening"]].sort_values("round")
        assert list(openings["round"]) == list(range(1, 10))
        assert list(openings["attacker_side"]) == ["T", "CT", "T", "T", "T", "T", "T", "T", "T"]
        assert int(live["t_alive_after"].min()) == 0 and int(live["ct_alive_after"].min()) == 0
        assert kills.loc[kills["after_round_end"], "t_alive_after"].isna().all()

        team_rounds, stats = result["team_rounds"], result["player_round_stats"]
        assert len(team_rounds) == 18 and len(stats) == 90
        assert check_invariants(kills, team_rounds, load_tables(engine, "public_test")["rounds"]) == []
        a = team_rounds[team_rounds["team"] == "A"].sort_values("round")
        b = team_rounds[team_rounds["team"] == "B"].sort_values("round")
        assert a["is_us"].all() and not b["is_us"].any()
        assert list(a["buy_type"]) == ["pistol", "semi", "semi", "full", "full", "full", "full", "full", "full"]
        assert list(b["buy_type"]) == ["pistol", "eco", "full", "eco", "semi", "full", "semi", "eco", "semi"]
        assert list(a["opening"]) == ["won", "lost", "won", "won", "won", "won", "won", "won", "won"]
        # round 3: the bomb went down after all CTs were dead, so it is not a plant
        assert list(a["planted"]) == [True, True, False, True, False, True, True, True, False]
        assert list(a["clutch_n"]) == [0, 2, 0, 0, 3, 0, 0, 0, 0]
        assert list(b["clutch_n"]) == [2, 0, 5, 5, 1, 3, 3, 5, 5]
        assert [bool(v) for v in b.loc[b["clutch_n"] > 0, "clutch_won"]] == [False] * 3 + [True] + [False] * 4  # round 5: a 1v1 won
        flags_a = [f for row in a["flags"] for f in json.loads(row)]
        assert flags_a.count("won_opening") == 8 and flags_a.count("postplant_won") == 5
        assert flags_a.count("team_flash") == 7 and flags_a.count("full_buy_no_plant") == 1

        # per player: the box-score numbers add up to the game's own scoreboard
        totals = stats.groupby("steamid")[["kills", "died", "assists", "damage"]].sum()
        for steamid, (k, d, a_, dmg) in SCOREBOARD.items():
            assert tuple(int(v) for v in totals.loc[steamid]) == (k, d, a_, dmg), steamid
        duels = stats.groupby("steamid")[["kast", "opening_kill", "opening_death", "trade_kills", "was_traded"]].sum()
        for steamid, expected in DUELS.items():
            assert tuple(int(v) for v in duels.loc[steamid]) == expected, steamid

        report = format_report(engine, "public_test")
        assert "KAST and opening duels" in report and "team A won 8 of 9 opening duels" in report
        engine.dispose()


if __name__ == "__main__":
    test_full_pipeline_on_public_demo()
    print("ok   test_full_pipeline_on_public_demo")
