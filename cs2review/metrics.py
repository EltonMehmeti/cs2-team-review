"""Phase 2: team metrics and round flags. Plain counting, no AI.

    python -m cs2review.metrics --rebuild            # recompute every match
    python -m cs2review.metrics --rebuild MATCH_ID   # one match
    python -m cs2review.metrics --buy-histogram      # see how buy types fall

Ingest runs this automatically after each demo. It reads the Phase 1 tables
and writes:

  1. five columns on `kills`: is_opening, is_trade, was_traded,
     t_alive_after, ct_alive_after
  2. `player_round_stats`: one row per player per played round
  3. `team_rounds`: one row per team per played round (the main table later)

CS2 words used here (see also the glossary in PLAN.md):
  opening kill  the first kill of a round. The team that gets it plays 5v4.
  trade         a teammate kills your killer within a few seconds of your
                death. A traded death is "fine"; an untraded one is a loss.
  advantage     own living players minus enemy living players (5v3 = +2).
  clutch (1vN)  one player left alive against N enemies.
  KAST          a round where the player got a Kill, an Assist, Survived, or
                was Traded. Good players have about 70%.
  ADR           average damage per round (health removed from enemies).
  buy types     pistol (first round of a half), eco (saving money),
                semi (partial buy), full (rifles, armour, grenades).

Two kinds of kills, on purpose:
  * How the round went (openings, trades, alive counts, advantages,
    clutches, team kills and deaths) uses only kills before the round ended
    (`after_round_end = 0`). A kill after the round is decided changes
    nothing about the round.
  * A player's own numbers in `player_round_stats` (kills, died, assists,
    damage, headshot_kills) count like the in-game scoreboard, which also
    counts kills after the round ended. So they add up to the scoreboard.
    A player killed after the round ended therefore has died = 1 and
    survived = 1 ("alive when the round ended").
"""

from __future__ import annotations

import argparse
import json
import sys

import pandas as pd
from sqlalchemy import Engine, bindparam, delete, text

from .config import BuyThresholds, load_config
from .db import Kill, PlayerRoundStat, SchemaMismatchError, TeamRound, get_engine, list_matches

TRADE_WINDOW_S = 5.0  # seconds a teammate has to kill your killer for a trade

UTILITY_DAMAGE_WEAPONS = {"hegrenade", "inferno"}  # HE grenade and molotov/incendiary fire
GRENADE_COUNT_COLUMN = {"smoke": "smokes", "flash": "flashes", "he": "hes", "molotov": "molotovs"}

SPLIT_BUY_RICH = 3500  # a player with this much gear or more bought properly ...
SPLIT_BUY_POOR = 1500  # ... while two or more players have less than this
SAVED_MIN_SURVIVORS = 2  # lost the round with this many alive: they saved their guns

SIDES = ("T", "CT")
OTHER_SIDE = {"T": "CT", "CT": "T"}

TEAM_ROUND_COLUMNS = [
    "match_id", "round", "team", "side", "is_us", "won", "buy_type", "enemy_buy_type",
    "equip_total", "players", "opening", "opening_time_s", "opening_traded",
    "kills", "deaths", "traded_deaths", "untraded_deaths", "best_advantage", "worst_disadvantage",
    "clutch_n", "clutch_won", "smokes", "flashes", "hes", "molotovs",
    "enemies_blinded", "teammates_blinded", "utility_damage",
    "planted", "plant_site", "plant_time_s", "postplant_s", "alive_at_plant", "enemy_alive_at_plant",
    "survivors", "flags",
]
PLAYER_ROUND_COLUMNS = [
    "match_id", "round", "steamid", "team", "side", "kills", "died", "assists", "damage",
    "headshot_kills", "opening_kill", "opening_death", "trade_kills", "was_traded", "survived",
    "kast", "multi_kill", "clutch_n", "clutch_won", "flash_assists", "enemies_blinded",
    "teammates_blinded", "utility_damage",
]


# --------------------------------------------------------------------------
# buy types
# --------------------------------------------------------------------------

def buy_type(money_starts: list, equip_values: list, thresholds: BuyThresholds = BuyThresholds()) -> str | None:
    """Name a team's buy from each player's money and gear at freeze end.

    pistol  every player started with pistol_max_money or less (first round of a half)
    eco     team gear value, scaled to five players, below eco_below
    semi    between eco_below and full_from
    full    full_from or more
    """
    equip = [int(v) for v in equip_values if pd.notna(v)]
    if not equip:
        return None
    money = list(money_starts)
    if money and all(pd.notna(m) and m <= thresholds.pistol_max_money for m in money):
        return "pistol"
    scaled = sum(equip) * 5 / len(equip)  # a team of 4 is judged as if it had 5 players
    if scaled < thresholds.eco_below:
        return "eco"
    if scaled < thresholds.full_from:
        return "semi"
    return "full"


def is_split_buy(buy: str | None, equip_values: list) -> bool:
    """Some players bought and others saved: at least one player with
    SPLIT_BUY_RICH gear or more and at least two with less than SPLIT_BUY_POOR."""
    if buy == "pistol" or buy is None:
        return False
    equip = [int(v) for v in equip_values if pd.notna(v)]
    rich = sum(v >= SPLIT_BUY_RICH for v in equip)
    poor = sum(v < SPLIT_BUY_POOR for v in equip)
    return rich >= 1 and poor >= 2


# --------------------------------------------------------------------------
# flags
# --------------------------------------------------------------------------

def round_flags(row: dict, split_buy: bool = False) -> list[str]:
    """Short labels for what happened to one team in one round.

    `row` is a team_rounds row (a dict). The labels are explained in PLAN.md,
    Phase 2, "Flags".
    """
    won = bool(row["won"])
    side = row["side"]
    planted = bool(row["planted"])
    flags: list[str] = []

    if row["opening"] == "won":
        flags.append("won_opening")
    if row["opening"] == "lost":
        flags.append("lost_opening")
        if row["opening_traded"] is False or row["opening_traded"] == 0:
            flags.append("opening_death_untraded")
    if row["untraded_deaths"] >= 3:
        flags.append("many_untraded_deaths")
    if not won and row["best_advantage"] >= 2:
        flags.append("threw_advantage")
    if not won and row["opening"] == "won":
        flags.append("lost_after_opening_kill")
    if won and row["worst_disadvantage"] <= -2:
        flags.append("comeback")
    if row["teammates_blinded"] > 0:  # blinded for 1 second or more by an own flash
        flags.append("team_flash")
    if split_buy:
        flags.append("split_buy")
    if not won and row["buy_type"] == "full" and row["enemy_buy_type"] in ("eco", "semi"):
        flags.append("lost_to_weaker_buy")
    if won and row["buy_type"] in ("eco", "semi") and row["enemy_buy_type"] == "full":
        flags.append("won_vs_stronger_buy")
    if side == "T" and row["buy_type"] == "full" and not planted and not won:
        flags.append("full_buy_no_plant")
    if side == "T" and not won and row.get("reason") == "time_ran_out":
        flags.append("time_ran_out")
    if side == "T" and planted:
        flags.append("postplant_won" if won else "postplant_lost")
    if side == "CT" and planted:
        flags.append("retake_won" if won else "retake_lost")
    if row["clutch_n"] >= 1:
        flags.append("clutch_won" if won else "clutch_lost")
    if not won and row["survivors"] >= SAVED_MIN_SURVIVORS:
        flags.append("saved")
    return flags


# --------------------------------------------------------------------------
# the main computation (works on DataFrames, so tests need no database)
# --------------------------------------------------------------------------

def _bools(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """SQLite stores True/False as 1/0 (or NULL); turn them back into bools."""
    frame = frame.copy()
    for column in columns:
        if column not in frame.columns:
            frame[column] = False
        frame[column] = frame[column].fillna(0).astype(bool)
    return frame


def _ensure(frame: pd.DataFrame | None, columns: list[str]) -> pd.DataFrame:
    """Missing table -> empty table with these columns; missing columns -> None."""
    frame = pd.DataFrame(columns=columns) if frame is None else frame.copy()
    for column in columns:
        if column not in frame.columns:
            frame[column] = None
    return frame


def _prepare(tables: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    t = {}
    t["rounds"] = _bools(
        _ensure(tables.get("rounds"), ["round", "t_team", "ct_team", "winner_team", "reason", "played",
                                       "end_tick", "duration_s"]),
        ["played"],
    )
    t["player_rounds"] = _ensure(tables.get("player_rounds"), ["round", "steamid", "team", "side", "money_start", "equip_value"])
    kills = _ensure(
        tables.get("kills"),
        ["id", "round", "tick", "time_s", "after_round_end", "attacker_steamid", "attacker_side",
         "victim_steamid", "victim_side", "assister_steamid", "flash_assist", "headshot", "is_suicide", "is_teamkill"],
    )
    kills = _bools(kills, ["after_round_end", "flash_assist", "headshot", "is_suicide", "is_teamkill"])
    if kills["id"].isna().all():
        kills["id"] = range(1, len(kills) + 1)
    t["kills"] = kills.sort_values(["round", "tick", "id"]).reset_index(drop=True)
    t["damages"] = _bools(
        _ensure(tables.get("damages"), ["round", "attacker_steamid", "attacker_side", "victim_side", "weapon",
                                        "dmg_health", "is_self", "is_teamdamage"]),
        ["is_self", "is_teamdamage"],
    )
    t["grenades"] = _ensure(
        tables.get("grenades"),
        ["round", "type", "thrower_steamid", "thrower_side", "throw_tick", "det_tick", "enemies_blinded", "teammates_blinded"],
    )
    t["bomb_events"] = _ensure(tables.get("bomb_events"), ["round", "tick", "time_s", "event", "site"])
    return t


def annotate_kills(kills: pd.DataFrame, rounds: pd.DataFrame, player_rounds: pd.DataFrame,
                   trade_window_s: float = TRADE_WINDOW_S) -> pd.DataFrame:
    """Add is_opening, is_trade, was_traded, t_alive_after, ct_alive_after to kills.

    Only kills in played rounds and before the round ended get values; the
    others keep False / empty alive counts.
    """
    kills = kills.copy()
    kills["is_opening"] = False
    kills["is_trade"] = False
    kills["was_traded"] = False
    kills["t_alive_after"] = pd.array([pd.NA] * len(kills), dtype="Int64")
    kills["ct_alive_after"] = pd.array([pd.NA] * len(kills), dtype="Int64")

    played = set(rounds.loc[rounds["played"], "round"].astype(int))
    live = kills[kills["round"].astype(int).isin(played) & ~kills["after_round_end"]]

    for rnd, group in live.groupby("round", sort=True):
        roster = player_rounds[player_rounds["round"] == rnd]
        alive = {side: int((roster["side"] == side).sum()) for side in SIDES}

        # Alive counts. Every death of any kind (also a suicide or the bomb)
        # removes one living player. When several players die on the same
        # tick (one HE grenade, say), all those kill rows show the count
        # after all of them.
        for _, same_tick in group.groupby("tick", sort=True):
            for victim_side in same_tick["victim_side"]:
                if victim_side in alive:
                    alive[victim_side] -= 1
            kills.loc[same_tick.index, "t_alive_after"] = alive["T"]
            kills.loc[same_tick.index, "ct_alive_after"] = alive["CT"]

        # "Real" kills: an enemy killed you (not a suicide, not a teamkill).
        real = group[~group["is_suicide"] & ~group["is_teamkill"]]
        if real.empty:
            continue

        # Opening kill: the first real kill of the round.
        kills.loc[real.index[0], "is_opening"] = True

        # Trades: your killer is killed by one of your teammates within
        # trade_window_s seconds after your death.
        for i, death in real.iterrows():
            answers = real[
                (real["victim_steamid"] == death["attacker_steamid"])  # the killer dies ...
                & (real["attacker_side"] == death["victim_side"])  # ... to the victim's side ...
                & (real["attacker_steamid"] != death["victim_steamid"])  # ... but not to the victim himself
                & (real["tick"] >= death["tick"])
                & (real["time_s"] - death["time_s"] <= trade_window_s)
            ]
            if not answers.empty:
                kills.loc[i, "was_traded"] = True
                kills.loc[answers.index[0], "is_trade"] = True
    return kills


def compute_metrics(
    tables: dict[str, pd.DataFrame],
    match_id: str | None = None,
    us_team: str | None = None,
    thresholds: BuyThresholds = BuyThresholds(),
    trade_window_s: float = TRADE_WINDOW_S,
) -> dict[str, pd.DataFrame]:
    """Compute all Phase 2 metrics for one match.

    tables: the Phase 1 tables of one match, by table name ("rounds",
            "player_rounds", "kills", "damages", "grenades", "bomb_events").
            Unit tests pass small hand-made tables.
    returns {"kills": kills with the five new columns,
             "team_rounds": ..., "player_round_stats": ...}
    """
    t = _prepare(tables)
    rounds, player_rounds, damages, grenades, bombs = (
        t["rounds"], t["player_rounds"], t["damages"], t["grenades"], t["bomb_events"]
    )
    kills = annotate_kills(t["kills"], rounds, player_rounds, trade_window_s)

    team_rows: list[dict] = []
    player_rows: list[dict] = []

    for r in rounds[rounds["played"]].sort_values("round").itertuples():
        rnd = int(r.round)
        team_of_side = {"T": r.t_team, "CT": r.ct_team}
        end_tick = int(r.end_tick) if pd.notna(r.end_tick) else None
        roster = player_rounds[player_rounds["round"] == rnd]
        in_round = kills[kills["round"] == rnd]  # everything in the round window
        live = in_round[~in_round["after_round_end"]]  # only before the round ended
        live_real = live[~live["is_suicide"] & ~live["is_teamkill"]]

        # ---- alive counts over time ---------------------------------------
        start = {side: int((roster["side"] == side).sum()) for side in SIDES}
        # states: alive counts at round start and after each tick with a death
        states = [dict(start)]
        dead_by_state: list[set] = [set()]
        alive, dead = dict(start), set()
        for _, same_tick in live.groupby("tick", sort=True):
            for victim, victim_side in zip(same_tick["victim_steamid"], same_tick["victim_side"]):
                if victim_side in alive:
                    alive[victim_side] -= 1
                dead.add(victim)
            states.append(dict(alive))
            dead_by_state.append(set(dead))
        final = states[-1]

        # ---- the plant (only if it happened before the round ended) --------
        plants = bombs[(bombs["round"] == rnd) & (bombs["event"] == "plant")].sort_values("tick")
        if end_tick is not None:
            plants = plants[plants["tick"] <= end_tick]
        plant = plants.iloc[0] if not plants.empty else None
        alive_at_plant = None
        if plant is not None:
            alive_at_plant = dict(start)
            for victim_side in live.loc[live["tick"] <= plant["tick"], "victim_side"]:
                if victim_side in alive_at_plant:
                    alive_at_plant[victim_side] -= 1

        # ---- the opening kill ------------------------------------------------
        opening = live[live["is_opening"]]
        opening = opening.iloc[0] if not opening.empty else None

        # ---- grenades thrown before the round ended ----------------------------
        g = grenades[grenades["round"] == rnd]
        if end_tick is not None and not g.empty:
            when = pd.to_numeric(g["throw_tick"], errors="coerce").fillna(pd.to_numeric(g["det_tick"], errors="coerce"))
            g = g[when.isna() | (when <= end_tick)]

        # utility damage: HE and fire damage to enemies (whole round window, like damage)
        d = damages[damages["round"] == rnd]
        enemy_damage = d[d["attacker_steamid"].notna() & ~d["is_self"] & ~d["is_teamdamage"]]
        utility = enemy_damage[enemy_damage["weapon"].isin(UTILITY_DAMAGE_WEAPONS)]

        sides_rows = {}
        clutch_player = {}
        for side in SIDES:
            enemy = OTHER_SIDE[side]
            team = team_of_side[side]
            members = roster[roster["side"] == side]

            # advantage = own alive - enemy alive, at the start and after each death
            diffs = [s[side] - s[enemy] for s in states]

            # clutch: the first moment we are down to one player while enemies live
            clutch_n, clutch_won = 0, None
            for state, dead_now in zip(states[1:], dead_by_state[1:]):
                if state[side] == 1 and state[enemy] >= 1:
                    clutch_n = state[enemy]
                    clutch_won = r.winner_team == team
                    last = [s for s in members["steamid"] if s not in dead_now]
                    clutch_player[side] = last[0] if len(last) == 1 else None
                    break

            deaths = live[live["victim_side"] == side]
            enemy_deaths = live_real[live_real["victim_side"] == side]  # killed by an enemy
            team_grenades = g[g["thrower_side"] == side]
            flashes = team_grenades[team_grenades["type"] == "flash"]

            if opening is None:
                opening_result, opening_traded = None, None
            elif opening["attacker_side"] == side:
                opening_result, opening_traded = "won", None
            else:
                opening_result, opening_traded = "lost", bool(opening["was_traded"])

            row = {
                "match_id": match_id,
                "round": rnd,
                "team": team,
                "side": side,
                "is_us": us_team is not None and team == us_team,
                "won": r.winner_team == team,
                "buy_type": buy_type(list(members["money_start"]), list(members["equip_value"]), thresholds),
                "enemy_buy_type": None,  # filled below, once both sides are known
                "equip_total": int(pd.to_numeric(members["equip_value"], errors="coerce").fillna(0).sum()),
                "players": int(start[side]),
                "opening": opening_result,
                "opening_time_s": None if opening is None else float(opening["time_s"]),
                "opening_traded": opening_traded,
                "kills": int((live_real["attacker_side"] == side).sum()),
                "deaths": int(len(deaths)),
                "traded_deaths": int(enemy_deaths["was_traded"].sum()),
                "untraded_deaths": int((~enemy_deaths["was_traded"]).sum()),
                "best_advantage": int(max(diffs)),
                "worst_disadvantage": int(min(diffs)),
                "clutch_n": int(clutch_n),
                "clutch_won": clutch_won,
                "enemies_blinded": int(pd.to_numeric(flashes["enemies_blinded"], errors="coerce").fillna(0).sum()),
                "teammates_blinded": int(pd.to_numeric(flashes["teammates_blinded"], errors="coerce").fillna(0).sum()),
                "utility_damage": int(pd.to_numeric(utility.loc[utility["attacker_side"] == side, "dmg_health"]).sum()),
                "planted": plant is not None,
                "plant_site": None if plant is None else plant["site"],
                "plant_time_s": None if plant is None else float(plant["time_s"]),
                "postplant_s": None,
                "alive_at_plant": None if alive_at_plant is None else int(alive_at_plant[side]),
                "enemy_alive_at_plant": None if alive_at_plant is None else int(alive_at_plant[enemy]),
                "survivors": int(final[side]),
                "reason": r.reason,  # used by the flags, not stored
            }
            for kind, column in GRENADE_COUNT_COLUMN.items():
                row[column] = int((team_grenades["type"] == kind).sum())
            if plant is not None and pd.notna(r.duration_s) and pd.notna(plant["time_s"]):
                row["postplant_s"] = round(float(r.duration_s) - float(plant["time_s"]), 1)
            row["_split"] = list(members["equip_value"])
            sides_rows[side] = row

        for side in SIDES:
            row = sides_rows[side]
            row["enemy_buy_type"] = sides_rows[OTHER_SIDE[side]]["buy_type"]
            split = is_split_buy(row["buy_type"], row.pop("_split"))
            row["flags"] = json.dumps(round_flags(row, split_buy=split))
            row.pop("reason")
            team_rows.append(row)

        # ---- one row per player ---------------------------------------------------
        real_all = in_round[~in_round["is_suicide"] & ~in_round["is_teamkill"]]
        player_damage = enemy_damage.groupby("attacker_steamid")["dmg_health"].sum()
        player_utility = utility.groupby("attacker_steamid")["dmg_health"].sum()
        live_dead = set(live["victim_steamid"])
        for p in roster.itertuples():
            sid = p.steamid
            side = p.side
            my_kills = real_all[real_all["attacker_steamid"] == sid]
            my_live_kills = live_real[live_real["attacker_steamid"] == sid]
            my_live_death = live_real[live_real["victim_steamid"] == sid]
            my_assists = in_round[in_round["assister_steamid"] == sid]
            my_flashes = g[(g["thrower_steamid"] == sid) & (g["type"] == "flash")]
            n_kills = int(len(my_kills))
            n_assists = int(len(my_assists))
            survived = sid not in live_dead
            was_traded = bool(my_live_death["was_traded"].any())
            is_clutcher = side in clutch_player and clutch_player[side] == sid
            team_row = sides_rows.get(side)
            player_rows.append(
                {
                    "match_id": match_id,
                    "round": rnd,
                    "steamid": sid,
                    "team": p.team,
                    "side": side,
                    "kills": n_kills,
                    "died": bool((in_round["victim_steamid"] == sid).any()),
                    "assists": n_assists,
                    "damage": int(player_damage.get(sid, 0)),
                    "headshot_kills": int(my_kills["headshot"].sum()),
                    "opening_kill": bool(my_live_kills["is_opening"].any()),
                    "opening_death": bool(my_live_death["is_opening"].any()),
                    "trade_kills": int(my_live_kills["is_trade"].sum()),
                    "was_traded": was_traded,
                    "survived": survived,
                    "kast": n_kills > 0 or n_assists > 0 or survived or was_traded,
                    "multi_kill": n_kills if n_kills >= 2 else 0,
                    "clutch_n": int(team_row["clutch_n"]) if is_clutcher else 0,
                    "clutch_won": team_row["clutch_won"] if is_clutcher else None,
                    "flash_assists": int(my_assists["flash_assist"].sum()),
                    "enemies_blinded": int(pd.to_numeric(my_flashes["enemies_blinded"], errors="coerce").fillna(0).sum()),
                    "teammates_blinded": int(pd.to_numeric(my_flashes["teammates_blinded"], errors="coerce").fillna(0).sum()),
                    "utility_damage": int(player_utility.get(sid, 0)),
                }
            )

    team_rounds = pd.DataFrame(team_rows, columns=TEAM_ROUND_COLUMNS)
    player_round_stats = pd.DataFrame(player_rows, columns=PLAYER_ROUND_COLUMNS)
    return {"kills": kills, "team_rounds": team_rounds, "player_round_stats": player_round_stats}


# --------------------------------------------------------------------------
# invariants: facts that must hold for every match
# --------------------------------------------------------------------------

def check_invariants(kills: pd.DataFrame, team_rounds: pd.DataFrame, rounds: pd.DataFrame) -> list[str]:
    """Return a list of problems (empty = all good).

    * every played round has one row per team, and exactly one of them won
    * per round: kills of both teams = deaths of both teams - suicides - teamkills
    * alive counts never go below 0
    """
    problems: list[str] = []
    rounds = _bools(rounds, ["played"])
    kills = _bools(kills, ["after_round_end", "is_suicide", "is_teamkill"])
    for rnd in rounds.loc[rounds["played"], "round"].astype(int):
        rows = team_rounds[team_rounds["round"] == rnd]
        if len(rows) != 2:
            problems.append(f"round {rnd}: {len(rows)} team_rounds rows instead of 2")
            continue
        if int(rows["won"].astype(bool).sum()) != 1:
            problems.append(f"round {rnd}: {int(rows['won'].astype(bool).sum())} teams won")
        live = kills[(kills["round"] == rnd) & ~kills["after_round_end"]]
        not_by_enemy = int((live["is_suicide"] | live["is_teamkill"]).sum())
        if int(rows["kills"].sum()) != int(rows["deaths"].sum()) - not_by_enemy:
            problems.append(
                f"round {rnd}: kills {int(rows['kills'].sum())} != deaths {int(rows['deaths'].sum())} "
                f"- suicides and teamkills {not_by_enemy}"
            )
        if (rows["survivors"] < 0).any():
            problems.append(f"round {rnd}: fewer than 0 survivors")
    for column in ("t_alive_after", "ct_alive_after"):
        if column in kills.columns:
            values = pd.to_numeric(kills[column], errors="coerce")
            if (values < 0).any():
                bad = sorted(set(kills.loc[values < 0, "round"].astype(int)))
                problems.append(f"{column} below 0 in rounds {bad}")
    return problems


# --------------------------------------------------------------------------
# database: load, compute, save
# --------------------------------------------------------------------------

PHASE1_TABLES = ["rounds", "player_rounds", "kills", "damages", "grenades", "bomb_events"]


def _read(engine: Engine, sql: str, **params) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


def load_tables(engine: Engine, match_id: str) -> dict[str, pd.DataFrame]:
    return {name: _read(engine, f"SELECT * FROM {name} WHERE match_id = :m", m=match_id) for name in PHASE1_TABLES}


def save_metrics(engine: Engine, match_id: str, result: dict[str, pd.DataFrame]) -> None:
    """Write the metrics of one match, replacing any old ones."""
    kills = result["kills"]
    updates = [
        {
            "kill_id": int(row.id),
            "v_is_opening": bool(row.is_opening),
            "v_is_trade": bool(row.is_trade),
            "v_was_traded": bool(row.was_traded),
            "v_t_alive": None if pd.isna(row.t_alive_after) else int(row.t_alive_after),
            "v_ct_alive": None if pd.isna(row.ct_alive_after) else int(row.ct_alive_after),
        }
        for row in kills.itertuples()
    ]
    table = Kill.__table__
    statement = (
        table.update()
        .where(table.c.id == bindparam("kill_id"))
        .values(
            is_opening=bindparam("v_is_opening"),
            is_trade=bindparam("v_is_trade"),
            was_traded=bindparam("v_was_traded"),
            t_alive_after=bindparam("v_t_alive"),
            ct_alive_after=bindparam("v_ct_alive"),
        )
    )
    with engine.begin() as conn:
        if updates:
            conn.execute(statement, updates)
        for model in (TeamRound, PlayerRoundStat):
            conn.execute(delete(model).where(model.match_id == match_id))
            frame = result[model.__tablename__]
            if not frame.empty:
                frame.to_sql(model.__tablename__, conn, if_exists="append", index=False)


def build_match_metrics(engine: Engine, match_id: str, thresholds: BuyThresholds = BuyThresholds()) -> dict[str, pd.DataFrame]:
    """Load one match from the database, compute its metrics, save them."""
    match = _read(engine, "SELECT us_team FROM matches WHERE match_id = :m", m=match_id)
    if match.empty:
        raise ValueError(f"Unknown match id: {match_id}")
    us_team = match["us_team"].iloc[0]
    us_team = us_team if isinstance(us_team, str) else None
    result = compute_metrics(load_tables(engine, match_id), match_id=match_id, us_team=us_team, thresholds=thresholds)
    save_metrics(engine, match_id, result)
    return result


# --------------------------------------------------------------------------
# buy histogram: helps the owner tune the buy-type thresholds
# --------------------------------------------------------------------------

def buy_histogram(engine: Engine, thresholds: BuyThresholds, bucket: int = 2500) -> str:
    rows = _read(
        engine,
        "SELECT match_id, round, team, buy_type, equip_total, players FROM team_rounds",
    )
    if rows.empty:
        return "No team_rounds yet. Run:  python -m cs2review.ingest"
    lines = [
        f"Team gear value at freeze end, scaled to 5 players ({len(rows)} team-rounds).",
        f"Current thresholds: eco below {thresholds.eco_below}, full from {thresholds.full_from}, "
        f"pistol if everyone started with {thresholds.pistol_max_money} or less.",
        "",
    ]
    pistol = rows[rows["buy_type"] == "pistol"]
    rest = rows[rows["buy_type"] != "pistol"].copy()
    rest["scaled"] = rest["equip_total"] * 5 / rest["players"].where(rest["players"] > 0)
    if not rest.empty:
        top = int(rest["scaled"].max() // bucket + 1) * bucket
        counts = (rest["scaled"] // bucket * bucket).value_counts()
        widest = max(int(counts.max()), 1)
        marks = {thresholds.eco_below: "eco | semi", thresholds.full_from: "semi | full"}
        for low in range(0, top, bucket):
            for mark, label in marks.items():
                if low <= mark < low + bucket:  # draw the threshold line above its bucket
                    lines.append(f"{'':>13}  ---- {label} at {mark} ----")
            n = int(counts.get(low, 0))
            bar = "#" * max(round(40 * n / widest), 1 if n else 0)
            lines.append(f"{low:>6}-{low + bucket - 1:<6} {n:>4}  {bar}")
    lines += ["", "Buy types: " + ", ".join(f"{k} {v}" for k, v in rows["buy_type"].fillna("none").value_counts().items())]
    lines.append(f"(pistol rounds are not in the histogram: {len(pistol)})")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compute Phase 2 team metrics for ingested matches.")
    ap.add_argument("match_ids", nargs="*", help="matches to rebuild (default: all)")
    ap.add_argument("--rebuild", action="store_true", help="recompute metrics and save them")
    ap.add_argument("--buy-histogram", action="store_true", help="show how team buys are distributed")
    ap.add_argument("--config", default="config.toml")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not args.rebuild and not args.buy_histogram:
        ap.print_help()
        return 1

    cfg = load_config(args.config)
    try:
        engine = get_engine(cfg.db_path)
    except SchemaMismatchError as exc:
        print(exc)
        return 1
    matches = list_matches(engine)
    if matches.empty:
        print("No matches in the database yet. Run:  python -m cs2review.ingest")
        return 1

    failed = 0
    if args.rebuild:
        wanted = args.match_ids or list(matches["match_id"])
        unknown = set(wanted) - set(matches["match_id"])
        if unknown:
            print(f"Unknown match id(s): {', '.join(sorted(unknown))}. Use python -m cs2review.check --list.")
            return 1
        for match_id in wanted:
            result = build_match_metrics(engine, match_id, cfg.buy_types)
            problems = check_invariants(result["kills"], result["team_rounds"], load_tables(engine, match_id)["rounds"])
            status = "ok    " if not problems else "PROBLEM"
            failed += bool(problems)
            print(f"{status} {match_id}: {len(result['team_rounds'])} team-rounds, "
                  f"{len(result['player_round_stats'])} player-rounds")
            for problem in problems:
                print(f"         - {problem}")
    if args.buy_histogram:
        if args.rebuild:
            print()
        print(buy_histogram(engine, cfg.buy_types))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
