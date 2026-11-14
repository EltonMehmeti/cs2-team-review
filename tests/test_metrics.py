"""Unit tests for Phase 2 metrics (cs2review/metrics.py). No demo needed.

Every test builds a tiny match by hand: team A (players a1..a5) plays T,
team B (players b1..b5) plays CT, and kills are written as
kill(second, attacker, victim). A player's side follows from his letter.

Run with:  python tests/test_metrics.py     (or: python -m pytest)
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from cs2review.config import BuyThresholds
from cs2review.db import SCHEMA_VERSION, SchemaMismatchError, get_engine
from cs2review.metrics import buy_type, check_invariants, compute_metrics, is_split_buy, round_flags

TICK_RATE = 64
FREEZE_END = 1000  # tick where each test round goes live
A = [f"a{i}" for i in range(1, 6)]
B = [f"b{i}" for i in range(1, 6)]


def side_of(player: str, t_team: str = "A") -> str:
    return "T" if player[0].upper() == t_team else "CT"


def kill(second, attacker, victim, rnd=1, after_end=False, assister=None, flash=False, headshot=False,
         weapon="ak47", t_team="A"):
    """One player_death row. attacker=None means a suicide (fall damage, the bomb)."""
    return {
        "round": rnd,
        "tick": FREEZE_END + int(round(second * TICK_RATE)),
        "time_s": float(second),
        "after_round_end": after_end,
        "attacker_steamid": attacker,
        "attacker_side": side_of(attacker, t_team) if attacker else None,
        "victim_steamid": victim,
        "victim_side": side_of(victim, t_team),
        "assister_steamid": assister,
        "flash_assist": flash,
        "headshot": headshot,
        "weapon": weapon,
        "is_suicide": attacker is None or attacker == victim,
        "is_teamkill": attacker is not None and attacker != victim and attacker[0] == victim[0],
    }


def make_match(kills, winner="A", reason="ct_eliminated", length_s=100, money=4000, equip_a=None, equip_b=None,
               grenades=None, damages=None, bombs=None, extra_rounds=None):
    """A one-round match (round 1) with A on T. Returns the tables dict."""
    equip_a = equip_a or [5000] * 5
    equip_b = equip_b or [5000] * 5
    end_tick = FREEZE_END + length_s * TICK_RATE
    rounds = [{
        "round": 1, "t_team": "A", "ct_team": "B", "winner_team": winner, "reason": reason, "played": True,
        "end_tick": end_tick, "duration_s": float(length_s),
    }] + (extra_rounds or [])
    player_rounds = (
        [{"round": 1, "steamid": p, "team": "A", "side": "T", "money_start": money, "equip_value": e}
         for p, e in zip(A, equip_a)]
        + [{"round": 1, "steamid": p, "team": "B", "side": "CT", "money_start": money, "equip_value": e}
           for p, e in zip(B, equip_b)]
    )
    kills = pd.DataFrame(kills, columns=list(kill(1, "a1", "b1").keys()))
    kills.insert(0, "id", range(1, len(kills) + 1))
    return {
        "rounds": pd.DataFrame(rounds),
        "player_rounds": pd.DataFrame(player_rounds),
        "kills": kills,
        "grenades": pd.DataFrame(grenades or []),
        "damages": pd.DataFrame(damages or []),
        "bomb_events": pd.DataFrame(bombs or []),
    }


def run(tables, **kw):
    out = compute_metrics(tables, match_id="m", us_team="A", **kw)
    team = out["team_rounds"].set_index("team")
    players = out["player_round_stats"].set_index("steamid")
    return out, team, players


def flags_of(team_row) -> list[str]:
    return json.loads(team_row["flags"])


# --------------------------------------------------------------------------
# trades
# --------------------------------------------------------------------------

def test_trade_inside_window():
    out, team, players = run(make_match([kill(10, "b1", "a1"), kill(13, "a2", "b1")]))
    k = out["kills"]
    assert list(k["was_traded"]) == [True, False]
    assert list(k["is_trade"]) == [False, True]
    assert team.loc["A", "traded_deaths"] == 1 and team.loc["A", "untraded_deaths"] == 0
    assert players.loc["a1", "was_traded"] and players.loc["a2", "trade_kills"] == 1


def test_trade_at_exactly_the_window_counts():
    out, _, _ = run(make_match([kill(10, "b1", "a1"), kill(15, "a2", "b1")]))
    assert bool(out["kills"]["was_traded"].iloc[0])


def test_trade_outside_window():
    out, team, _ = run(make_match([kill(10, "b1", "a1"), kill(15.5, "a2", "b1")]))
    assert not out["kills"]["was_traded"].any() and not out["kills"]["is_trade"].any()
    assert team.loc["A", "untraded_deaths"] == 1


def test_trade_with_a_shorter_window():
    out, _, _ = run(make_match([kill(10, "b1", "a1"), kill(13, "a2", "b1")]), trade_window_s=2.0)
    assert not out["kills"]["was_traded"].any()


def test_trade_by_the_wrong_team():
    # b1 kills a1, then b1 is killed by his own teammate b2: not a trade for team A
    out, _, _ = run(make_match([kill(10, "b1", "a1"), kill(12, "b2", "b1")]))
    assert not out["kills"]["was_traded"].any() and not out["kills"]["is_trade"].any()


def test_killing_someone_else_is_not_a_trade():
    # b1 kills a1, a2 then kills b2 (not the killer)
    out, _, _ = run(make_match([kill(10, "b1", "a1"), kill(12, "a2", "b2")]))
    assert not out["kills"]["was_traded"].any()


def test_trade_must_be_in_the_same_live_round():
    # the answer comes after the round ended: it does not count
    out, _, _ = run(make_match([kill(10, "b1", "a1"), kill(12, "a2", "b1", after_end=True)], length_s=11))
    assert not out["kills"]["was_traded"].any()


# --------------------------------------------------------------------------
# openings, alive counts, advantage
# --------------------------------------------------------------------------

def test_opening_skips_suicides_and_teamkills():
    out, team, players = run(make_match([kill(5, None, "a3"), kill(6, "a4", "a5"), kill(8, "b2", "a1")]))
    k = out["kills"]
    assert list(k["is_opening"]) == [False, False, True]
    assert team.loc["B", "opening"] == "won" and team.loc["A", "opening"] == "lost"
    assert team.loc["A", "opening_time_s"] == 8.0
    assert players.loc["b2", "opening_kill"] and players.loc["a1", "opening_death"]
    assert flags_of(team.loc["A"])[:2] == ["lost_opening", "opening_death_untraded"]


def test_no_kills_no_opening():
    _, team, _ = run(make_match([], winner="B", reason="time_ran_out"))
    assert team.loc["A", "opening"] is None and team.loc["A", "best_advantage"] == 0
    assert team.loc["A", "survivors"] == 5 and team.loc["B", "survivors"] == 5


def test_alive_counts_and_advantage():
    kills = [
        kill(10, "a1", "b1"),  # 5v4: A +1
        kill(12, "a2", "b2"),  # 5v3: A +2 (best)
        kill(20, "b3", "a1"),  # 4v3
        kill(21, "b3", "a2"),  # 3v3
        kill(22, "b3", "a3"),  # 2v3
        kill(23, "b3", "a4"),  # 1v3: A -2 (worst)
        kill(30, "a5", "b3"),  # 1v2
    ]
    out, team, _ = run(make_match(kills, winner="B", reason="t_eliminated"))
    k = out["kills"]
    assert list(k["t_alive_after"]) == [5, 5, 4, 3, 2, 1, 1]
    assert list(k["ct_alive_after"]) == [4, 3, 3, 3, 3, 3, 2]
    assert team.loc["A", "best_advantage"] == 2 and team.loc["A", "worst_disadvantage"] == -2
    assert team.loc["B", "best_advantage"] == 2 and team.loc["B", "worst_disadvantage"] == -2
    assert "threw_advantage" in flags_of(team.loc["A"])
    assert "comeback" in flags_of(team.loc["B"])
    assert team.loc["A", "survivors"] == 1 and team.loc["B", "survivors"] == 2


def test_two_deaths_on_the_same_tick_share_the_alive_count():
    out, _, _ = run(make_match([kill(10, "a1", "b1", weapon="hegrenade"), kill(10, "a1", "b2", weapon="hegrenade")]))
    assert list(out["kills"]["ct_alive_after"]) == [3, 3]


def test_suicide_is_a_death_but_not_a_kill():
    out, team, players = run(make_match([kill(10, None, "a1"), kill(20, "b1", "a2")]))
    assert list(out["kills"]["t_alive_after"]) == [4, 3]
    assert team.loc["A", "deaths"] == 2 and team.loc["B", "kills"] == 1
    assert team.loc["A", "untraded_deaths"] == 1  # only deaths to an enemy can be traded
    assert players.loc["a1", "died"] and not players.loc["a1", "survived"]
    assert check_invariants(out["kills"], out["team_rounds"], make_match([])["rounds"]) == []


# --------------------------------------------------------------------------
# clutches
# --------------------------------------------------------------------------

def test_clutch_detection():
    kills = [
        kill(10, "b1", "a1"), kill(11, "b1", "a2"), kill(12, "b1", "a3"), kill(13, "b2", "a4"),  # a5 alone vs 5
        kill(20, "a5", "b1"),  # 1v4 now, but only the first moment counts
        kill(30, "a5", "b2"), kill(31, "a5", "b3"), kill(32, "a5", "b4"), kill(33, "a5", "b5"),
    ]
    _, team, players = run(make_match(kills, winner="A"))
    assert team.loc["A", "clutch_n"] == 5 and bool(team.loc["A", "clutch_won"])
    assert "clutch_won" in flags_of(team.loc["A"])
    assert players.loc["a5", "clutch_n"] == 5 and bool(players.loc["a5", "clutch_won"])
    assert players.loc["a1", "clutch_n"] == 0 and players.loc["a1", "clutch_won"] is None
    # B dropped to 1 (b5) only with A at 1 too: that is a 1v1 for B, lost
    assert team.loc["B", "clutch_n"] == 1 and not team.loc["B", "clutch_won"]
    assert "clutch_lost" in flags_of(team.loc["B"])
    assert players.loc["a5", "multi_kill"] == 5


def test_no_clutch_when_no_enemy_is_left():
    # A drops to 1 on the same tick as the last CT dies: nobody to clutch against
    kills = [kill(10, "a1", "b1"), kill(11, "a1", "b2"), kill(12, "a1", "b3"), kill(13, "a1", "b4"),
             kill(20, "b5", "a2"), kill(21, "b5", "a3"), kill(22, "b5", "a4"),
             kill(30, "a1", "b5"), kill(30, "b5", "a5")]
    _, team, _ = run(make_match(kills, winner="A"))
    assert team.loc["A", "clutch_n"] == 0 and team.loc["A", "clutch_won"] is None
    assert team.loc["B", "clutch_n"] == 5  # b5 was alone against all five at 13 s


# --------------------------------------------------------------------------
# KAST and box-score numbers
# --------------------------------------------------------------------------

def test_kast():
    kills = [
        kill(10, "a1", "b1"),                       # a1: kill
        kill(11, "b2", "a2", assister="b3"),        # b3: assist; a2: died ...
        kill(13, "a3", "b2"),                       # ... and traded by a3 (a3: kill)
        kill(20, "b4", "a4"),                       # a4: died, not traded, nothing else
        kill(60, "b5", "a5", after_end=True),       # a5: killed after the round ended, still "survived"
    ]
    _, _, p = run(make_match(kills, winner="B", reason="t_eliminated", length_s=50))
    assert p.loc["a1", "kast"] and p.loc["b3", "kast"] and p.loc["a2", "kast"] and p.loc["a3", "kast"]
    assert not p.loc["a4", "kast"]
    assert p.loc["a5", "kast"] and p.loc["a5", "survived"] and p.loc["a5", "died"]
    assert p.loc["b5", "kills"] == 1  # the late kill still counts on the scoreboard
    assert p.loc["b5", "kast"]  # survived anyway
    assert p.loc["b3", "assists"] == 1 and p.loc["b3", "kills"] == 0


def test_late_kill_is_not_an_opening_and_has_no_alive_count():
    out, team, _ = run(make_match([kill(60, "b5", "a5", after_end=True)], length_s=50, winner="B"))
    k = out["kills"].iloc[0]
    assert not k["is_opening"] and pd.isna(k["t_alive_after"])
    assert team.loc["A", "opening"] is None and team.loc["A", "deaths"] == 0 and team.loc["A", "survivors"] == 5


def test_damage_headshots_flash_assists_and_utility():
    damages = [
        {"round": 1, "attacker_steamid": "a1", "attacker_side": "T", "victim_side": "CT", "weapon": "ak47",
         "dmg_health": 100, "is_self": False, "is_teamdamage": False},
        {"round": 1, "attacker_steamid": "a2", "attacker_side": "T", "victim_side": "CT", "weapon": "hegrenade",
         "dmg_health": 40, "is_self": False, "is_teamdamage": False},
        {"round": 1, "attacker_steamid": "a2", "attacker_side": "T", "victim_side": "T", "weapon": "inferno",
         "dmg_health": 30, "is_self": False, "is_teamdamage": True},  # team damage: not counted
        {"round": 1, "attacker_steamid": "b1", "attacker_side": "CT", "victim_side": "T", "weapon": "inferno",
         "dmg_health": 12, "is_self": False, "is_teamdamage": False},
    ]
    grenades = [
        {"round": 1, "type": "flash", "thrower_steamid": "a3", "thrower_side": "T", "throw_tick": 1200, "det_tick": 1300,
         "enemies_blinded": 2, "teammates_blinded": 1},
        {"round": 1, "type": "smoke", "thrower_steamid": "a3", "thrower_side": "T", "throw_tick": 1100, "det_tick": 1200},
        {"round": 1, "type": "molotov", "thrower_steamid": "b1", "thrower_side": "CT", "throw_tick": 1100, "det_tick": None},
        {"round": 1, "type": "he", "thrower_steamid": "b1", "thrower_side": "CT", "throw_tick": 99999, "det_tick": 99999},  # after the end
    ]
    kills = [kill(10, "a1", "b1", headshot=True, assister="a3", flash=True)]
    _, team, p = run(make_match(kills, damages=damages, grenades=grenades))
    assert p.loc["a1", "damage"] == 100 and p.loc["a1", "headshot_kills"] == 1
    assert p.loc["a2", "damage"] == 40 and p.loc["a2", "utility_damage"] == 40
    assert p.loc["a3", "flash_assists"] == 1 and p.loc["a3", "enemies_blinded"] == 2 and p.loc["a3", "teammates_blinded"] == 1
    a, b = team.loc["A"], team.loc["B"]
    assert (a["smokes"], a["flashes"], a["hes"], a["molotovs"]) == (1, 1, 0, 0)
    assert (b["smokes"], b["flashes"], b["hes"], b["molotovs"]) == (0, 0, 0, 1)
    assert a["utility_damage"] == 40 and b["utility_damage"] == 12
    assert a["enemies_blinded"] == 2 and a["teammates_blinded"] == 1
    assert "team_flash" in flags_of(a) and "team_flash" not in flags_of(b)


# --------------------------------------------------------------------------
# the bomb
# --------------------------------------------------------------------------

def test_after_the_plant():
    bombs = [{"round": 1, "tick": FREEZE_END + 40 * TICK_RATE, "time_s": 40.0, "event": "plant", "site": "B"}]
    kills = [kill(10, "a1", "b1"), kill(30, "b2", "a1"), kill(35, "b2", "a2"), kill(50, "b3", "a3")]
    _, team, _ = run(make_match(kills, winner="B", reason="bomb_defused", length_s=75, bombs=bombs))
    a, b = team.loc["A"], team.loc["B"]
    assert a["planted"] and b["planted"] and a["plant_site"] == "B" and a["plant_time_s"] == 40.0
    assert a["postplant_s"] == 35.0
    assert (a["alive_at_plant"], a["enemy_alive_at_plant"]) == (3, 4)
    assert (b["alive_at_plant"], b["enemy_alive_at_plant"]) == (4, 3)
    assert "postplant_lost" in flags_of(a) and "retake_won" in flags_of(b)


def test_plant_after_the_round_ended_is_ignored():
    # seen in the public test demo: all CTs died, then the bomb was planted anyway
    bombs = [{"round": 1, "tick": FREEZE_END + 80 * TICK_RATE, "time_s": 80.0, "event": "plant", "site": "A"}]
    _, team, _ = run(make_match([], length_s=70, bombs=bombs))
    assert not team.loc["A", "planted"] and team.loc["A", "plant_site"] is None
    assert "postplant_won" not in flags_of(team.loc["A"])


# --------------------------------------------------------------------------
# buy types
# --------------------------------------------------------------------------

def test_buy_types():
    th = BuyThresholds()
    assert buy_type([800] * 5, [200, 300, 1000, 850, 200], th) == "pistol"
    assert buy_type([1000] * 5, [1000] * 5, th) == "pistol"
    assert buy_type([800, 800, 800, 800, 1500], [200] * 5, th) == "eco"  # one player has more: not a pistol round
    assert buy_type([3000] * 5, [900] * 5, th) == "eco"  # 4,500 in total
    assert buy_type([3000] * 5, [1000] * 5, th) == "semi"  # exactly 5,000
    assert buy_type([5000] * 5, [3999] * 5, th) == "semi"  # 19,995
    assert buy_type([5000] * 5, [4000] * 5, th) == "full"  # exactly 20,000
    assert buy_type([5000] * 4, [4000] * 4, th) == "full"  # 16,000 for four players counts as 20,000
    assert buy_type([5000] * 4, [3000] * 4, th) == "semi"
    assert buy_type([], [], th) is None
    assert buy_type([3000] * 5, [1000] * 5, BuyThresholds(eco_below=6000)) == "eco"


def test_buy_types_in_team_rounds():
    _, team, _ = run(make_match([], money=4000, equip_a=[5000, 5000, 4500, 4000, 4000], equip_b=[700] * 5))
    assert team.loc["A", "buy_type"] == "full" and team.loc["A", "enemy_buy_type"] == "eco"
    assert team.loc["B", "buy_type"] == "eco" and team.loc["A", "equip_total"] == 22500
    _, team, _ = run(make_match([], money=800, equip_a=[200] * 5, equip_b=[200] * 5))
    assert team.loc["A", "buy_type"] == "pistol"


def test_split_buy():
    assert is_split_buy("semi", [4700, 4500, 1000, 800, 200])
    assert not is_split_buy("semi", [4700, 4500, 2000, 800, 1600])  # only one poor player
    assert not is_split_buy("eco", [3000, 1000, 800, 800, 200])  # nobody bought properly
    assert not is_split_buy("pistol", [3500, 200, 200, 200, 200])
    _, team, _ = run(make_match([], equip_a=[4700, 4500, 1000, 800, 200]))
    assert "split_buy" in flags_of(team.loc["A"])


# --------------------------------------------------------------------------
# flags, one by one
# --------------------------------------------------------------------------

def _row(**changes):
    """A dull team round: won, no opening, nothing special."""
    row = {
        "won": True, "side": "T", "planted": False, "reason": "ct_eliminated", "opening": None,
        "opening_traded": None, "untraded_deaths": 0, "best_advantage": 0, "worst_disadvantage": 0,
        "teammates_blinded": 0, "buy_type": "semi", "enemy_buy_type": "semi", "clutch_n": 0, "survivors": 0,
    }
    row.update(changes)
    return row


def test_dull_round_has_no_flags():
    assert round_flags(_row()) == []
    assert round_flags(_row(won=False, side="CT")) == []


def test_each_flag():
    cases = [
        ("won_opening", _row(opening="won")),
        ("lost_opening", _row(opening="lost", opening_traded=True)),
        ("opening_death_untraded", _row(opening="lost", opening_traded=False)),
        ("many_untraded_deaths", _row(untraded_deaths=3)),
        ("threw_advantage", _row(won=False, best_advantage=2)),
        ("lost_after_opening_kill", _row(won=False, opening="won")),
        ("comeback", _row(won=True, worst_disadvantage=-2)),
        ("team_flash", _row(teammates_blinded=1)),
        ("lost_to_weaker_buy", _row(won=False, buy_type="full", enemy_buy_type="eco")),
        ("lost_to_weaker_buy", _row(won=False, buy_type="full", enemy_buy_type="semi")),
        ("won_vs_stronger_buy", _row(won=True, buy_type="eco", enemy_buy_type="full")),
        ("full_buy_no_plant", _row(won=False, side="T", buy_type="full", enemy_buy_type="full")),
        ("time_ran_out", _row(won=False, side="T", reason="time_ran_out")),
        ("postplant_won", _row(won=True, side="T", planted=True)),
        ("postplant_lost", _row(won=False, side="T", planted=True)),
        ("retake_won", _row(won=True, side="CT", planted=True)),
        ("retake_lost", _row(won=False, side="CT", planted=True)),
        ("clutch_won", _row(won=True, clutch_n=2)),
        ("clutch_lost", _row(won=False, clutch_n=1)),
        ("saved", _row(won=False, survivors=2)),
    ]
    for flag, row in cases:
        assert flag in round_flags(row), (flag, round_flags(row))
    assert "split_buy" in round_flags(_row(), split_buy=True)


def test_flags_that_must_not_fire():
    assert "opening_death_untraded" not in round_flags(_row(opening="lost", opening_traded=True))
    assert "many_untraded_deaths" not in round_flags(_row(untraded_deaths=2))
    assert "threw_advantage" not in round_flags(_row(won=True, best_advantage=3))
    assert "threw_advantage" not in round_flags(_row(won=False, best_advantage=1))
    assert "comeback" not in round_flags(_row(won=False, worst_disadvantage=-3))
    assert "lost_to_weaker_buy" not in round_flags(_row(won=False, buy_type="full", enemy_buy_type="full"))
    assert "won_vs_stronger_buy" not in round_flags(_row(won=True, buy_type="pistol", enemy_buy_type="full"))
    assert "full_buy_no_plant" not in round_flags(_row(won=False, side="CT", buy_type="full"))
    assert "full_buy_no_plant" not in round_flags(_row(won=False, side="T", buy_type="full", planted=True))
    assert "time_ran_out" not in round_flags(_row(won=True, side="CT", reason="time_ran_out"))
    assert "saved" not in round_flags(_row(won=False, survivors=1))
    assert "saved" not in round_flags(_row(won=True, survivors=4))


def test_time_ran_out_in_a_full_match_row():
    _, team, _ = run(make_match([], winner="B", reason="time_ran_out"))
    assert "time_ran_out" in flags_of(team.loc["A"]) and "saved" in flags_of(team.loc["A"])
    assert "time_ran_out" not in flags_of(team.loc["B"])


# --------------------------------------------------------------------------
# invariants and rounds that do not count
# --------------------------------------------------------------------------

def test_unplayed_rounds_are_skipped():
    surrender = {"round": 2, "t_team": "A", "ct_team": "B", "winner_team": "A", "reason": "ct_surrender",
                 "played": False, "end_tick": 99999, "duration_s": None}
    tables = make_match([kill(10, "a1", "b1")], extra_rounds=[surrender])
    out, _, _ = run(tables)
    assert list(out["team_rounds"]["round"].unique()) == [1]
    assert len(out["player_round_stats"]) == 10
    assert check_invariants(out["kills"], out["team_rounds"], tables["rounds"]) == []


def test_invariants_catch_broken_numbers():
    tables = make_match([kill(10, "a1", "b1"), kill(20, "b2", "a1")])
    out, _, _ = run(tables)
    assert check_invariants(out["kills"], out["team_rounds"], tables["rounds"]) == []

    broken = out["team_rounds"].copy()
    broken.loc[broken["team"] == "A", "kills"] = 3
    assert any("kills" in p for p in check_invariants(out["kills"], broken, tables["rounds"]))

    both_won = out["team_rounds"].copy()
    both_won["won"] = True
    assert any("2 teams won" in p for p in check_invariants(out["kills"], both_won, tables["rounds"]))

    negative = out["kills"].copy()
    negative.loc[0, "t_alive_after"] = -1
    assert any("below 0" in p for p in check_invariants(negative, out["team_rounds"], tables["rounds"]))

    missing = out["team_rounds"].iloc[:1]
    assert any("1 team_rounds rows" in p for p in check_invariants(out["kills"], missing, tables["rounds"]))


# --------------------------------------------------------------------------
# the schema version
# --------------------------------------------------------------------------

def test_schema_version_is_written_and_checked():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "new.sqlite"
        engine = get_engine(path)  # a new database gets the current version
        engine.dispose()
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
        get_engine(path).dispose()  # opening it again is fine

        old = Path(tmp) / "old.sqlite"  # a Phase 1 database: tables, but no meta table
        with sqlite3.connect(old) as conn:
            conn.execute("CREATE TABLE matches (match_id TEXT PRIMARY KEY)")
        try:
            get_engine(old)
        except SchemaMismatchError as exc:
            assert "older version" in str(exc) and "python -m cs2review.ingest" in str(exc)
        else:
            raise AssertionError("an old database must be refused")


if __name__ == "__main__":
    tests = [f for name, f in sorted(globals().items()) if name.startswith("test_") and callable(f)]
    for test in tests:
        test()
        print("ok  ", test.__name__)
    print(f"\n{len(tests)} tests passed")
