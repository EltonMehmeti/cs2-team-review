"""Unit tests for Phase 3 summaries (cs2review/summaries.py). No demo needed.

Run with:  python tests/test_summaries.py     (or: python -m pytest)
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from cs2review.metrics import compute_metrics
from cs2review.summaries import (
    Digest, MatchContext, RoundSummaryModel, estimate_tokens, fit_budget, match_digest, round_summary, to_json,
)

TICK_RATE = 64
FREEZE_END = 1000
NAMES = {"a1": "Ann", "a2": "Bob", "a3": "Cid", "a4": "Dan", "a5": "Eve",
         "b1": "Xavi", "b2": "Yan", "b3": "Zed", "b4": "Wu", "b5": "Vik"}


def tick(second):
    return FREEZE_END + int(round(second * TICK_RATE))


def kill(second, attacker, victim, weapon, place, rnd=1):
    return {
        "round": rnd, "tick": tick(second), "time_s": second, "after_round_end": False,
        "attacker_steamid": attacker, "attacker_side": None if attacker is None else ("T" if attacker[0] == "a" else "CT"),
        "victim_steamid": victim, "victim_side": "T" if victim[0] == "a" else "CT", "victim_place": place,
        "assister_steamid": None, "flash_assist": False, "headshot": False, "weapon": weapon,
        "is_suicide": attacker is None, "is_teamkill": False,
    }


def grenade(second, who, kind, frm=None, to=None, enemies=None, mates=None, rnd=1):
    return {"round": rnd, "type": kind, "thrower_steamid": who, "thrower_side": "T" if who[0] == "a" else "CT",
            "throw_tick": tick(second), "throw_time_s": second, "det_tick": tick(second + 2), "det_time_s": second + 2,
            "throw_place": frm, "land_place": to, "enemies_blinded": enemies, "teammates_blinded": mates}


def hand_made_match(us_team="A", extra_kills=()):
    """One round on Mirage: we (team A) attack, lose the opening, trade it, plant A and win."""
    kills = [
        kill(10.0, None, "a5", "world", "TSpawn"),        # Eve falls to her death
        kill(25.4, "b1", "a1", "awp", "TRamp"),           # opening: Ann dies to the AWP ...
        kill(27.0, "a2", "b1", "ak47", "BombsiteA"),      # ... and Bob trades her
        kill(40.2, "a3", "b2", "ak47", "Jungle"),
        kill(50.0, "b3", "a3", "m4a1", "BombsiteA"),      # Cid dies after the plant, not traded
        *extra_kills,
    ]
    kills = pd.DataFrame(kills)
    kills.insert(0, "id", range(1, len(kills) + 1))
    players = pd.DataFrame([{"steamid": s, "name": n, "team": "A" if s[0] == "a" else "B"} for s, n in NAMES.items()])
    rounds = pd.DataFrame([{
        "round": 1, "t_team": "A", "ct_team": "B", "winner_team": "A", "winner_side": "T", "reason": "bomb_exploded",
        "played": True, "start_tick": 0, "freeze_end_tick": FREEZE_END, "end_tick": tick(70), "duration_s": 70.0,
        "score_a_before": 0, "score_b_before": 0,
    }])
    player_rounds = pd.DataFrame([{"round": 1, "steamid": s, "team": "A" if s[0] == "a" else "B",
                                   "side": "T" if s[0] == "a" else "CT", "money_start": 4000, "equip_value": 4500}
                                  for s in NAMES])
    grenades = pd.DataFrame([
        grenade(18.2, "a4", "smoke", "TSpawn", "CTSpawn"),
        grenade(24.0, "a4", "flash", "TSpawn", "TRamp", enemies=0, mates=2),
        grenade(30.0, "b4", "molotov", "CTSpawn", "TRamp"),
        grenade(75.0, "a2", "he", "BombsiteA", "CTSpawn"),  # after the round ended: left out
    ])
    grenades.insert(0, "id", range(1, len(grenades) + 1))
    positions = pd.DataFrame([{"round": 1, "steamid": s, "time_s": 20, "place": p} for s, p in
                              [("a1", "TRamp"), ("a2", "TRamp"), ("a3", "PalaceInterior"), ("a4", "TopofMid"),
                               ("b1", "BombsiteA")]])
    bombs = pd.DataFrame([{"round": 1, "tick": tick(45.0), "time_s": 45.0, "event": "plant", "steamid": "a3", "site": "A"}])
    tables = {"rounds": rounds, "player_rounds": player_rounds, "kills": kills, "grenades": grenades, "bomb_events": bombs,
              "damages": pd.DataFrame()}
    metrics = compute_metrics(tables, match_id="m1", us_team=us_team)
    match = pd.DataFrame([{"match_id": "m1", "map": "de_mirage", "us_team": us_team, "score_a": 1, "score_b": 0,
                           "team_a_name": "Our Five", "team_b_name": None}])
    return {
        "matches": match, "players": players, "rounds": rounds, "team_rounds": metrics["team_rounds"],
        "player_round_stats": metrics["player_round_stats"], "kills": metrics["kills"], "grenades": grenades,
        "positions": positions, "bomb_events": bombs,
    }


EXPECTED_ROUND = {
    "round": 1, "side": "T", "score": "0-0",
    "buy": {"us": "full", "them": "full"},
    "setup_20s": {"Ann": "TRamp", "Bob": "TRamp", "Cid": "PalaceInterior", "Dan": "TopofMid", "Eve": "dead"},
    "utility": [
        {"t": 18, "by": "Dan", "type": "smoke", "from": "TSpawn", "to": "CTSpawn"},
        {"t": 24, "by": "Dan", "type": "flash", "enemies_blinded": 0, "mates_blinded": 2},
    ],
    "enemy_utility": {"molotov": 1},
    "kills": [
        {"t": 10, "killer": "world", "victim": "Eve", "weapon": "world", "at": "TSpawn"},
        {"t": 25, "killer": "enemy", "victim": "Ann", "weapon": "awp", "at": "TRamp", "opening": True, "traded": True},
        {"t": 27, "killer": "Bob", "victim": "enemy", "weapon": "ak47", "at": "BombsiteA", "trade": True},
        {"t": 40, "killer": "Cid", "victim": "enemy", "weapon": "ak47", "at": "Jungle"},
        {"t": 50, "killer": "enemy", "victim": "Cid", "weapon": "m4a1", "at": "BombsiteA", "traded": False},
    ],
    "bomb": {"site": "A", "t": 45, "by": "Cid", "alive": "3v3", "outcome": "exploded"},
    "result": {"won": True, "how": "bomb_exploded", "length_s": 70, "alive": "2v3"},
    "flags": ["lost_opening", "comeback", "team_flash", "postplant_won"],
}


def test_round_summary_matches_expected_json():
    ctx = MatchContext.from_tables(hand_made_match())
    got = round_summary(ctx, 1)
    assert got == EXPECTED_ROUND, json.dumps(got, indent=1)
    RoundSummaryModel.model_validate(got)


def test_round_summary_from_the_other_team():
    # the same round seen by team B: they defend, and our names are now theirs
    ctx = MatchContext.from_tables(hand_made_match(us_team="B"))
    got = round_summary(ctx, 1)
    assert got["side"] == "CT" and got["result"]["won"] is False and got["result"]["alive"] == "3v2"
    assert got["kills"][1] == {"t": 25, "killer": "Xavi", "victim": "enemy", "weapon": "awp", "at": "TRamp",
                               "opening": True, "traded": True}
    assert got["bomb"]["by"] == "enemy" and got["bomb"]["alive"] == "3v3"
    assert got["utility"] == [{"t": 30, "by": "Wu", "type": "molotov", "to": "TRamp"}]
    assert got["setup_20s"] == {"Xavi": "BombsiteA"}  # the others have no position in the hand-made data
    assert "retake_lost" in got["flags"]


def test_enemy_with_many_openings_keeps_his_name():
    # Xavi gets three opening kills in three rounds: he is named, the others are "enemy"
    tables = hand_made_match()
    more = []
    for rnd in (2, 3):
        more.append({**tables["rounds"].iloc[0].to_dict(), "round": rnd})
    tables["rounds"] = pd.concat([tables["rounds"], pd.DataFrame(more)], ignore_index=True)
    extra = tables["kills"].iloc[[1]].copy()
    extra = pd.concat([extra.assign(round=2, id=100), extra.assign(round=3, id=101)])
    tables["kills"] = pd.concat([tables["kills"], extra], ignore_index=True)
    ctx = MatchContext.from_tables(tables)
    assert ctx.label("b1") == "enemy Xavi" and ctx.label("b2") == "enemy" and ctx.label("a1") == "Ann"
    assert round_summary(ctx, 1)["kills"][1]["killer"] == "enemy Xavi"


def test_digest_is_valid_and_identical_on_two_runs():
    first = to_json(match_digest(MatchContext.from_tables(hand_made_match())))
    second = to_json(match_digest(MatchContext.from_tables(hand_made_match())))
    assert first == second
    digest = json.loads(first)
    Digest.model_validate(digest)
    assert digest["match"] == {"match_id": "m1", "map": "de_mirage", "us": "Our Five", "them": "team B",
                               "final_score": "1-0", "won": True, "halves": [{"rounds": "1-1", "side": "T"}],
                               "rounds_played": 1}
    eve = next(p for p in digest["players"] if p["name"] == "Eve")
    assert (eve["k"], eve["d"], eve["untraded_deaths"], eve["kast_pct"]) == (0, 1, 0, 0)
    ann = next(p for p in digest["players"] if p["name"] == "Ann")
    assert (ann["opening_deaths"], ann["untraded_deaths"], ann["kast_pct"]) == (1, 0, 100)
    dan = next(p for p in digest["players"] if p["name"] == "Dan")
    assert (dan["flashes_thrown"], dan["teammates_blinded"]) == (1, 2)
    team = digest["team"]
    assert team["opening_duels"] == {"won": 0, "n": 1, "pct": 0}
    assert team["trade_rate"] == {"won": 1, "n": 2, "pct": 50}  # Ann traded, Cid not; Eve's fall cannot be traded
    assert team["postplant"] == {"won": 1, "n": 1, "pct": 100} and team["retake"] == {"won": 0, "n": 0}
    assert team["flags"] == {"comeback": 1, "lost_opening": 1, "postplant_won": 1, "team_flash": 1}


def test_budget_drops_fields_in_the_planned_order():
    digest = match_digest(MatchContext.from_tables(hand_made_match()))
    full_tokens = estimate_tokens(to_json(digest))
    same, tokens = fit_budget(digest, budget=full_tokens)
    assert "dropped" not in same and tokens == full_tokens

    small, tokens = fit_budget(digest, budget=10)  # impossible: every step is taken
    assert small["dropped"] == ["kill areas of non-opening kills", "utility from", "enemy details"]
    kills = small["rounds"][0]["kills"]
    assert [k.get("at") for k in kills] == [None, "TRamp", None, None, None]  # only the opening keeps its area
    assert all("from" not in u for u in small["rounds"][0]["utility"])
    assert "enemy_utility" not in small["rounds"][0]
    assert tokens < full_tokens
    Digest.model_validate(small)

    one_step, _ = fit_budget(digest, budget=full_tokens - 1)
    assert one_step["dropped"] == ["kill areas of non-opening kills"]
    assert "from" in one_step["rounds"][0]["utility"][0]


def test_estimate_tokens():
    assert estimate_tokens("") == 0 and estimate_tokens("abcd") == 1 and estimate_tokens("abcde") == 2


if __name__ == "__main__":
    tests = [f for name, f in sorted(globals().items()) if name.startswith("test_") and callable(f)]
    for test in tests:
        test()
        print("ok  ", test.__name__)
    print(f"\n{len(tests)} tests passed")
