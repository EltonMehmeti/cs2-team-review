"""A small synthetic database for tests that need several matches (no demo).

build_db(path) writes three made-up matches through the real Phase 2 and 3
code (compute_metrics, build_digest), so the views and tables look exactly
like the real thing:

  m1  de_mirage   10 rounds, file time 2026-11-01
  m2  de_mirage   10 rounds, file time 2026-11-08
  m3  de_inferno   4 rounds, file time 2026-11-09

We are team A (players a1..a5, names Ann, Bob, Cid, Dan, Eve) and always
play T. In odd rounds we win the opening duel (a1 kills b1), in even rounds
we lose it (b1 kills a1, traded by a2 in rounds divisible by 4). We win
every round except those divisible by 3. Round 1 is a pistol round; we plant
B in rounds 2, 5 and 8.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from cs2review.db import get_engine
from cs2review.metrics import compute_metrics
from cs2review.summaries import build_digest

TICK_RATE = 64
NAMES = {"a1": "Ann", "a2": "Bob", "a3": "Cid", "a4": "Dan", "a5": "Eve",
         "b1": "Xavi", "b2": "Yan", "b3": "Zed", "b4": "Wu", "b5": "Vik"}


def _kill(rnd, start, second, attacker, victim, place="Middle"):
    return {"round": rnd, "tick": start + int(second * TICK_RATE), "time_s": float(second), "after_round_end": False,
            "attacker_steamid": attacker, "attacker_side": "T" if attacker[0] == "a" else "CT",
            "victim_steamid": victim, "victim_side": "T" if victim[0] == "a" else "CT", "victim_place": place,
            "attacker_place": "TRamp", "assister_steamid": None, "flash_assist": False, "headshot": False,
            "weapon": "ak47", "is_suicide": False, "is_teamkill": False}


def synthetic_match(match_id: str, map_name: str, n_rounds: int, file_time: str) -> dict:
    rounds, player_rounds, kills, bombs = [], [], [], []
    score = {"A": 0, "B": 0}
    for rnd in range(1, n_rounds + 1):
        freeze = rnd * 10_000
        winner = "B" if rnd % 3 == 0 else "A"
        reason = "t_eliminated" if winner == "B" else "ct_eliminated"
        rounds.append({"round": rnd, "start_tick": freeze - 1000, "freeze_end_tick": freeze, "end_tick": freeze + 60 * TICK_RATE,
                       "window_end_tick": freeze + 70 * TICK_RATE, "t_team": "A", "ct_team": "B",
                       "winner_side": "T" if winner == "A" else "CT", "winner_team": winner, "reason": reason,
                       "played": True, "duration_s": 60.0, "score_a_before": score["A"], "score_b_before": score["B"],
                       "bomb_planted": rnd in (2, 5, 8), "plant_time_s": 40.0 if rnd in (2, 5, 8) else None,
                       "plant_site": "B" if rnd in (2, 5, 8) else None})
        score[winner] += 1
        money, equip = (800, 200) if rnd == 1 else (5000, 4500)
        for s in NAMES:
            player_rounds.append({"round": rnd, "steamid": s, "team": "A" if s[0] == "a" else "B",
                                  "side": "T" if s[0] == "a" else "CT", "money_start": money, "equip_value": equip})
        if rnd % 2 == 1:
            kills.append(_kill(rnd, freeze, 10, "a1", "b1", "Connector"))
        else:
            kills.append(_kill(rnd, freeze, 10, "b1", "a1", "Jungle"))
            if rnd % 4 == 0:
                kills.append(_kill(rnd, freeze, 12, "a2", "b1", "Jungle"))
        if rnd in (2, 5, 8):
            bombs.append({"round": rnd, "tick": freeze + 40 * TICK_RATE, "time_s": 40.0, "event": "plant",
                          "steamid": "a3", "place": "BombsiteB", "site": "B"})
    kills = pd.DataFrame(kills)
    kills.insert(0, "id", range(1, len(kills) + 1))
    tables = {"rounds": pd.DataFrame(rounds), "player_rounds": pd.DataFrame(player_rounds), "kills": kills,
              "bomb_events": pd.DataFrame(bombs), "grenades": pd.DataFrame(), "damages": pd.DataFrame()}
    metrics = compute_metrics(tables, match_id=match_id, us_team="A")
    match = pd.DataFrame([{"match_id": match_id, "file_name": f"{match_id}.dem", "map": map_name, "source": "faceit",
                           "tick_rate": TICK_RATE, "rounds_total": n_rounds, "rounds_played": n_rounds,
                           "score_a": score["A"], "score_b": score["B"], "us_team": "A", "file_time": file_time,
                           "ingested_at": file_time}])
    players = pd.DataFrame([{"steamid": s, "name": n, "team": "A" if s[0] == "a" else "B", "is_us": s[0] == "a"}
                            for s, n in NAMES.items()])
    kills_out = metrics["kills"].drop(columns=["id"])
    out = {"matches": match, "players": players, "rounds": pd.DataFrame(rounds), "kills": kills_out,
           "bomb_events": pd.DataFrame(bombs), "team_rounds": metrics["team_rounds"],
           "player_round_stats": metrics["player_round_stats"],
           "player_rounds": pd.DataFrame(player_rounds)}
    for name, frame in out.items():
        if "match_id" not in frame.columns:
            frame.insert(0, "match_id", match_id)
    return out


def build_db(path) -> object:
    """Write the three synthetic matches into a new database. Returns the engine."""
    engine = get_engine(path)
    for match_id, map_name, n, when in (("m1", "de_mirage", 10, "2026-11-01T20:00:00+00:00"),
                                         ("m2", "de_mirage", 10, "2026-11-08T20:00:00+00:00"),
                                         ("m3", "de_inferno", 4, "2026-11-09T20:00:00+00:00")):
        tables = synthetic_match(match_id, map_name, n, when)
        with engine.begin() as conn:
            for name, frame in tables.items():
                if not frame.empty:
                    frame.to_sql(name, conn, if_exists="append", index=False)
        build_digest(engine, match_id)
    return engine
