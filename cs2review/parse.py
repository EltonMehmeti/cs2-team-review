"""Turn one CS2 demo file into tidy tables (pandas DataFrames).

Nothing here touches the game. It only reads a .dem file from disk.

Vocabulary used in the tables:
  tick      one snapshot of the game; CS2 records 64 per second
  time_s    seconds since the round went live (the end of freeze time)
  side      "T" or "CT"
  team      "A" or "B": A is the team that starts on the T side. Teams keep
            their letter when sides swap at halftime.
  place     the map-area name the game itself uses ("Jungle", "BombsiteA")
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path

import numpy as np
import pandas as pd
from demoparser2 import DemoParser

from . import places
from .rounds import select_final_rounds
from .unpack import match_id_for

SIDE = {2: "T", 3: "CT"}

# extra facts we ask the parser to attach to every event
PLAYER_PROPS = [
    "X", "Y", "Z", "last_place_name", "team_num",
    "pitch", "yaw", "velocity_X", "velocity_Y", "velocity_Z",
]
OTHER_PROPS = ["total_rounds_played", "is_warmup_period", "game_time"]

EVENTS = [
    "round_start", "round_freeze_end", "round_end", "cs_win_panel_match",
    "player_death", "player_hurt", "player_blind", "weapon_fire",
    "bomb_planted", "bomb_defused", "bomb_exploded",
    "smokegrenade_detonate", "flashbang_detonate", "hegrenade_detonate",
    "inferno_startburn",
]

GRENADE_WEAPONS = {
    "weapon_smokegrenade": "smoke",
    "weapon_flashbang": "flash",
    "weapon_hegrenade": "he",
    "weapon_molotov": "molotov",
    "weapon_incgrenade": "molotov",
    "weapon_decoy": "decoy",
}
DETONATIONS = {
    "smokegrenade_detonate": "smoke",
    "flashbang_detonate": "flash",
    "hegrenade_detonate": "he",
    "inferno_startburn": "molotov",
}
END_REASONS = {
    "#SFUI_Notice_Terrorists_Win": "ct_eliminated",
    "#SFUI_Notice_CTs_Win": "t_eliminated",
    "#SFUI_Notice_Target_Bombed": "bomb_exploded",
    "#SFUI_Notice_Bomb_Defused": "bomb_defused",
    "#SFUI_Notice_Target_Saved": "time_ran_out",
    "#SFUI_Notice_Terrorists_Surrender": "t_surrender",
    "#SFUI_Notice_CTs_Surrender": "ct_surrender",
}

ECON_PROPS = [
    "team_num", "start_balance", "balance", "cash_spent_this_round",
    "current_equip_value", "round_start_equip_value",
    "armor_value", "has_helmet", "has_defuser", "inventory", "team_clan_name",
]
POS_PROPS = ["X", "Y", "Z", "last_place_name", "health", "is_alive", "team_num", "yaw"]
TOTAL_PROPS = [
    "kills_total", "deaths_total", "assists_total", "damage_total",
    "headshot_kills_total", "mvps", "team_num", "team_rounds_total",
]

MAX_FLIGHT_S = 30  # a grenade that has not gone off after this never will
MIN_BLIND_S = 1.0  # shorter flashes do not count as "blinded"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _df(x) -> pd.DataFrame:
    """demoparser2 returns [] instead of a table when an event never happened."""
    return x if isinstance(x, pd.DataFrame) else pd.DataFrame()


def _sid(series: pd.Series) -> pd.Series:
    """Steam IDs as strings (they are too long for floats); missing -> None."""

    def conv(v):
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        s = str(v)
        if s.endswith(".0"):
            s = s[:-2]
        return None if s in ("", "0", "nan", "None", "<NA>") else s

    return pd.Series([conv(v) for v in series], index=series.index, dtype="object")


def _col(df: pd.DataFrame, name: str, default=None) -> pd.Series:
    if name in df.columns:
        return df[name]
    return pd.Series([default] * len(df), index=df.index, dtype="object")


def _side(series: pd.Series) -> pd.Series:
    nums = pd.to_numeric(series, errors="coerce")
    return pd.Series([SIDE.get(int(v)) if pd.notna(v) else None for v in nums],
                     index=series.index, dtype="object")


def _bool(series: pd.Series) -> pd.Series:
    """True/False column; anything missing counts as False."""

    def conv(v) -> bool:
        if v is None or v is pd.NA:
            return False
        if isinstance(v, float) and np.isnan(v):
            return False
        return bool(v)

    return pd.Series([conv(v) for v in series], index=series.index, dtype=bool)


def _ticks(df: pd.DataFrame) -> list[int]:
    if df.empty or "tick" not in df.columns:
        return []
    return sorted({int(t) for t in df["tick"]})


def _round1(series: pd.Series, digits: int = 1) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").astype("float64").round(digits)


# --------------------------------------------------------------------------
# rounds
# --------------------------------------------------------------------------

def _build_rounds(ev: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, int]:
    ends = ev["round_end"]
    if ends.empty:
        raise ValueError("The demo has no finished rounds. Is it a complete match demo?")
    ends = ends.copy()
    if "is_warmup_period" in ends.columns:
        ends = ends[~_bool(ends["is_warmup_period"])]
    ends = ends.sort_values("tick").drop_duplicates("tick", keep="last")
    if ends.empty:
        raise ValueError("The demo only contains warmup rounds.")

    # ticks per second, measured from the demo itself (64 for CS2)
    tick_rate = 64
    if "game_time" in ends.columns and len(ends) >= 2:
        seconds = float(ends["game_time"].iloc[-1]) - float(ends["game_time"].iloc[0])
        ticks = int(ends["tick"].iloc[-1]) - int(ends["tick"].iloc[0])
        if seconds > 1:
            measured = int(round(ticks / seconds))
            if 16 <= measured <= 256:
                tick_rate = measured

    final = select_final_rounds(
        [(int(t), int(p)) for t, p in zip(ends["tick"], ends["total_rounds_played"])]
    )
    starts = _ticks(ev["round_start"])
    freezes = _ticks(ev["round_freeze_end"])
    match_end_ticks = _ticks(ev["cs_win_panel_match"])
    match_end = match_end_ticks[-1] if match_end_ticks else None
    info = ends.set_index("tick")

    rows = []
    prev_end = -1
    for end_tick, number in final:
        before = [t for t in starts if prev_end < t <= end_tick]
        start_tick = before[-1] if before else prev_end + 1
        frozen = [t for t in freezes if start_tick <= t <= end_tick]
        freeze_tick = frozen[-1] if frozen else None

        # events up to the next round_start belong to this round
        later = [t for t in starts if t > end_tick]
        if later:
            window_end = later[0] - 1
        elif match_end is not None and match_end >= end_tick:
            window_end = match_end
        else:
            window_end = end_tick + 10 * tick_rate

        message = str(info.at[end_tick, "message"]) if "message" in info.columns else ""
        reason = END_REASONS.get(message, message.replace("#SFUI_Notice_", "").lower() or "unknown")
        winner_raw = info.at[end_tick, "winner"] if "winner" in info.columns else None
        try:
            winner_side = SIDE.get(int(winner_raw))
        except (TypeError, ValueError):
            winner_side = str(winner_raw).upper() if str(winner_raw).upper() in ("T", "CT") else None

        rows.append(
            {
                "round": number,
                "start_tick": start_tick,
                "freeze_end_tick": freeze_tick,
                "end_tick": end_tick,
                "window_end_tick": window_end,
                "winner_side": winner_side,
                "reason": reason,
                # a round that never went live (surrender in freeze time) is not "played"
                "played": freeze_tick is not None and "surrender" not in reason,
                "duration_s": round((end_tick - freeze_tick) / tick_rate, 1) if freeze_tick is not None else None,
            }
        )
        prev_end = end_tick

    return pd.DataFrame(rows), tick_rate


class _RoundClock:
    """Answers: which round does this tick belong to, and how far into it?"""

    def __init__(self, rounds: pd.DataFrame, tick_rate: int):
        self.tick_rate = tick_rate
        self.numbers = rounds["round"].to_numpy()
        self.starts = rounds["start_tick"].to_numpy(dtype="int64")
        self.window_ends = rounds["window_end_tick"].to_numpy(dtype="int64")
        self.ends = dict(zip(rounds["round"], rounds["end_tick"]))
        self.freeze = {r: f for r, f in zip(rounds["round"], rounds["freeze_end_tick"]) if pd.notna(f)}

    def round_of(self, ticks: pd.Series) -> pd.Series:
        t = pd.to_numeric(ticks, errors="coerce").to_numpy(dtype="float64")
        idx = np.searchsorted(self.starts, t, side="right") - 1
        out = []
        for tick, i in zip(t, idx):
            if np.isnan(tick) or i < 0 or tick > self.window_ends[i]:
                out.append(pd.NA)
            else:
                out.append(int(self.numbers[i]))
        return pd.Series(out, index=ticks.index, dtype="Int64")

    def time_s(self, rounds: pd.Series, ticks: pd.Series) -> pd.Series:
        out = []
        for r, t in zip(rounds, ticks):
            f = self.freeze.get(int(r)) if pd.notna(r) else None
            out.append(round((int(t) - int(f)) / self.tick_rate, 2) if f is not None and pd.notna(t) else None)
        return pd.Series(out, index=ticks.index, dtype="float64")

    def after_end(self, rounds: pd.Series, ticks: pd.Series) -> pd.Series:
        return pd.Series([int(t) > int(self.ends[int(r)]) for r, t in zip(rounds, ticks)],
                         index=ticks.index, dtype=bool)


def _in_rounds(df: pd.DataFrame, clock: _RoundClock) -> pd.DataFrame:
    """Keep only events inside a real round; add round, time_s, after_round_end."""
    if df.empty:
        return df.assign(round=pd.Series(dtype="Int64"), time_s=pd.Series(dtype="float64"),
                         after_round_end=pd.Series(dtype=bool))
    df = df.copy()
    df["round"] = clock.round_of(df["tick"])
    df = df[df["round"].notna()].sort_values("tick").reset_index(drop=True)
    df["time_s"] = clock.time_s(df["round"], df["tick"])
    df["after_round_end"] = clock.after_end(df["round"], df["tick"])
    return df


# --------------------------------------------------------------------------
# players, sides, teams, economy
# --------------------------------------------------------------------------

def _snapshots(parser: DemoParser, rounds: pd.DataFrame) -> pd.DataFrame:
    """Every player's side, money and gear at the moment each round goes live."""
    tick_to_round = {}
    for r in rounds.itertuples():
        tick = r.freeze_end_tick if pd.notna(r.freeze_end_tick) else r.end_tick
        tick_to_round[int(tick)] = int(r.round)
    snap = _df(parser.parse_ticks(ECON_PROPS, ticks=sorted(tick_to_round)))
    if snap.empty:
        raise ValueError("Could not read player data from the demo.")
    snap = snap.copy()
    snap["round"] = snap["tick"].map(tick_to_round).astype("Int64")
    snap["steamid"] = _sid(snap["steamid"])
    snap["side"] = _side(snap["team_num"])
    snap = snap[snap["steamid"].notna() & snap["side"].notna()]
    return snap.sort_values(["round", "steamid"]).reset_index(drop=True)


def _assign_teams(snap: pd.DataFrame, rounds: pd.DataFrame) -> tuple[dict[str, str], dict[int, str]]:
    """Team A = whoever starts on T. Returns (team per player, T-side team per round)."""
    team_of: dict[str, str] = {}
    t_team_of_round: dict[int, str] = {}
    last = "A"
    for number in rounds["round"]:
        rows = snap[snap["round"] == number]
        t_ids = list(rows.loc[rows["side"] == "T", "steamid"])
        ct_ids = list(rows.loc[rows["side"] == "CT", "steamid"])
        if not t_ids and not ct_ids:
            t_team = last
        elif not team_of:
            t_team = "A"
        else:
            a_is_t = sum(team_of.get(s) == "A" for s in t_ids) + sum(team_of.get(s) == "B" for s in ct_ids)
            b_is_t = sum(team_of.get(s) == "B" for s in t_ids) + sum(team_of.get(s) == "A" for s in ct_ids)
            t_team = "A" if a_is_t >= b_is_t else "B"
        ct_team = "B" if t_team == "A" else "A"
        for s in t_ids:
            team_of.setdefault(s, t_team)
        for s in ct_ids:
            team_of.setdefault(s, ct_team)
        t_team_of_round[int(number)] = t_team
        last = t_team
    return team_of, t_team_of_round


def _team_names(snap: pd.DataFrame, team_of: dict[str, str]) -> dict[str, str | None]:
    """The team names shown in game (FACEIT sets them), if the demo has any."""
    out: dict[str, str | None] = {"A": None, "B": None}
    if "team_clan_name" not in snap.columns:
        return out
    names = snap["team_clan_name"].fillna("").astype(str).str.strip()
    teams = snap["steamid"].map(team_of)
    for team in out:
        found = names[(teams == team) & (names != "")]
        if not found.empty:
            out[team] = str(found.mode().iloc[0])
    return out


# --------------------------------------------------------------------------
# the main function
# --------------------------------------------------------------------------

def parse_demo(
    dem_path: str | Path,
    team_steam_ids: list[str] | tuple[str, ...] = (),
    known_cells: pd.DataFrame | None = None,
    match_id: str | None = None,
) -> dict[str, pd.DataFrame]:
    """Parse one .dem file. Returns a dict of table name -> DataFrame."""
    dem_path = Path(dem_path)
    match_id = match_id or match_id_for(dem_path)
    parser = DemoParser(str(dem_path))
    header = parser.parse_header()
    map_name = header.get("map_name", "unknown")

    ev = {name: pd.DataFrame() for name in EVENTS}
    for name, table in parser.parse_events(EVENTS, player=PLAYER_PROPS, other=OTHER_PROPS):
        ev[name] = _df(table)

    rounds, tick_rate = _build_rounds(ev)
    clock = _RoundClock(rounds, tick_rate)

    # ---- sides, teams, economy -------------------------------------------
    snap = _snapshots(parser, rounds)
    team_of, t_team_of_round = _assign_teams(snap, rounds)
    team_names = _team_names(snap, team_of)
    other = {"A": "B", "B": "A"}

    rounds["t_team"] = rounds["round"].map(t_team_of_round)
    rounds["ct_team"] = rounds["t_team"].map(other)
    rounds["winner_team"] = [
        (t if side == "T" else ct) if side in ("T", "CT") else None
        for side, t, ct in zip(rounds["winner_side"], rounds["t_team"], rounds["ct_team"])
    ]
    wins_a = (rounds["winner_team"] == "A").astype(int)
    wins_b = (rounds["winner_team"] == "B").astype(int)
    rounds["score_a_before"] = wins_a.cumsum() - wins_a
    rounds["score_b_before"] = wins_b.cumsum() - wins_b

    # ---- kills ------------------------------------------------------------
    k = _in_rounds(ev["player_death"], clock)
    kills = pd.DataFrame(
        {
            "round": k["round"],
            "tick": k["tick"],
            "time_s": k["time_s"],
            "after_round_end": k["after_round_end"],
            "attacker_steamid": _sid(_col(k, "attacker_steamid")),
            "attacker_side": _side(_col(k, "attacker_team_num")),
            "attacker_place": _col(k, "attacker_last_place_name"),
            "attacker_x": _round1(_col(k, "attacker_X")),
            "attacker_y": _round1(_col(k, "attacker_Y")),
            "attacker_z": _round1(_col(k, "attacker_Z")),
            "victim_steamid": _sid(_col(k, "user_steamid")),
            "victim_side": _side(_col(k, "user_team_num")),
            "victim_place": _col(k, "user_last_place_name"),
            "victim_x": _round1(_col(k, "user_X")),
            "victim_y": _round1(_col(k, "user_Y")),
            "victim_z": _round1(_col(k, "user_Z")),
            "assister_steamid": _sid(_col(k, "assister_steamid")),
            "assister_side": _side(_col(k, "assister_team_num")),
            "flash_assist": _bool(_col(k, "assistedflash", False)),
            "weapon": _col(k, "weapon"),
            "headshot": _bool(_col(k, "headshot", False)),
            "through_smoke": _bool(_col(k, "thrusmoke", False)),
            "attacker_blind": _bool(_col(k, "attackerblind", False)),
            "noscope": _bool(_col(k, "noscope", False)),
            "wallbang": pd.to_numeric(_col(k, "penetrated", 0), errors="coerce").fillna(0) > 0,
            "distance": _round1(_col(k, "distance")),
        }
    )
    kills["is_suicide"] = kills["attacker_steamid"].isna() | (kills["attacker_steamid"] == kills["victim_steamid"])
    kills["is_teamkill"] = ~kills["is_suicide"] & (kills["attacker_side"] == kills["victim_side"])
    # the game also names a teammate who hurt the victim as "assister"; that is not an assist
    not_an_assist = kills["assister_side"] == kills["victim_side"]
    kills.loc[not_an_assist, "assister_steamid"] = None
    kills.loc[not_an_assist, "flash_assist"] = False
    kills = kills.drop(columns=["assister_side"])

    # ---- damage -----------------------------------------------------------
    h = _in_rounds(ev["player_hurt"], clock)
    damages = pd.DataFrame(
        {
            "round": h["round"],
            "tick": h["tick"],
            "time_s": h["time_s"],
            "after_round_end": h["after_round_end"],
            "attacker_steamid": _sid(_col(h, "attacker_steamid")),
            "attacker_side": _side(_col(h, "attacker_team_num")),
            "victim_steamid": _sid(_col(h, "user_steamid")),
            "victim_side": _side(_col(h, "user_team_num")),
            "weapon": _col(h, "weapon"),
            "hitgroup": _col(h, "hitgroup"),
            "dmg_health_raw": pd.to_numeric(_col(h, "dmg_health", 0), errors="coerce").fillna(0).astype(int),
            "dmg_armor": pd.to_numeric(_col(h, "dmg_armor", 0), errors="coerce").fillna(0).astype(int),
            "health_after": pd.to_numeric(_col(h, "health", 0), errors="coerce").fillna(0).astype(int),
        }
    )
    # A 110-damage headshot on a player with 30 HP only removes 30 HP.
    # dmg_health is the health really removed (health before minus health after);
    # it is what ADR is built from and what the in-game scoreboard counts.
    health_now: dict[tuple[int, str], int] = {}
    real = []
    for rnd, victim, after in zip(damages["round"], damages["victim_steamid"], damages["health_after"]):
        key = (int(rnd), victim)
        before = health_now.get(key, 100)
        real.append(max(before - int(after), 0))
        health_now[key] = int(after)
    damages["dmg_health"] = real
    damages["is_self"] = damages["attacker_steamid"].notna() & (damages["attacker_steamid"] == damages["victim_steamid"])
    damages["is_teamdamage"] = (
        damages["attacker_steamid"].notna() & ~damages["is_self"] & (damages["attacker_side"] == damages["victim_side"])
    )

    # ---- flashes: who was blinded -----------------------------------------
    b = _in_rounds(ev["player_blind"], clock)
    blinds = pd.DataFrame(
        {
            "round": b["round"],
            "tick": b["tick"],
            "time_s": b["time_s"],
            "thrower_steamid": _sid(_col(b, "attacker_steamid")),
            "thrower_side": _side(_col(b, "attacker_team_num")),
            "victim_steamid": _sid(_col(b, "user_steamid")),
            "victim_side": _side(_col(b, "user_team_num")),
            "duration_s": pd.to_numeric(_col(b, "blind_duration", 0), errors="coerce").fillna(0).round(2),
            "entity_id": pd.to_numeric(_col(b, "entityid"), errors="coerce").astype("Int64"),
        }
    )
    blinds["is_self"] = blinds["thrower_steamid"].notna() & (blinds["thrower_steamid"] == blinds["victim_steamid"])
    blinds["is_teammate"] = ~blinds["is_self"] & (blinds["thrower_side"] == blinds["victim_side"])

    # ---- bomb --------------------------------------------------------------
    bomb_frames = []
    for event_name, label in (("bomb_planted", "plant"), ("bomb_defused", "defuse"), ("bomb_exploded", "explode")):
        e = _in_rounds(ev[event_name], clock)
        if e.empty:
            continue
        place = _col(e, "user_last_place_name")
        bomb_frames.append(
            pd.DataFrame(
                {
                    "round": e["round"],
                    "tick": e["tick"],
                    "time_s": e["time_s"],
                    "event": label,
                    "steamid": _sid(_col(e, "user_steamid")),
                    "place": place,
                    "x": _round1(_col(e, "user_X")),
                    "y": _round1(_col(e, "user_Y")),
                    "z": _round1(_col(e, "user_Z")),
                }
            )
        )
    if bomb_frames:
        bomb_events = pd.concat(bomb_frames, ignore_index=True).sort_values("tick").reset_index(drop=True)
    else:
        bomb_events = pd.DataFrame(columns=["round", "tick", "time_s", "event", "steamid", "place", "x", "y", "z"])
    plants = bomb_events[bomb_events["event"] == "plant"].drop_duplicates("round", keep="first")
    site_of_round = {}
    for r, place in zip(plants["round"], plants["place"]):
        text = str(place)
        site_of_round[int(r)] = "A" if text.endswith("A") else "B" if text.endswith("B") else None
    bomb_events["site"] = [site_of_round.get(int(r)) for r in bomb_events["round"]]
    plant_tick = dict(zip(plants["round"].astype(int), plants["tick"].astype(int)))
    plant_time = dict(zip(plants["round"].astype(int), plants["time_s"]))
    rounds["bomb_planted"] = [int(r) in plant_tick for r in rounds["round"]]
    rounds["plant_time_s"] = [plant_time.get(int(r)) for r in rounds["round"]]
    rounds["plant_site"] = [site_of_round.get(int(r)) for r in rounds["round"]]

    # ---- economy per player per round --------------------------------------
    played_rounds = set(rounds.loc[rounds["played"], "round"].astype(int))
    eco = snap[snap["round"].isin(played_rounds)]
    died = {
        (int(r), v)
        for r, v, late in zip(kills["round"], kills["victim_steamid"], kills["after_round_end"])
        if not late
    }
    player_rounds = pd.DataFrame(
        {
            "round": eco["round"],
            "steamid": eco["steamid"],
            "side": eco["side"],
            "team": eco["steamid"].map(team_of),
            "money_start": pd.to_numeric(eco["start_balance"], errors="coerce").astype("Int64"),
            "money_spent": pd.to_numeric(eco["cash_spent_this_round"], errors="coerce").astype("Int64"),
            "money_left": pd.to_numeric(eco["balance"], errors="coerce").astype("Int64"),
            "equip_value": pd.to_numeric(eco["current_equip_value"], errors="coerce").astype("Int64"),
            "start_equip_value": pd.to_numeric(eco["round_start_equip_value"], errors="coerce").astype("Int64"),
            "armor": pd.to_numeric(eco["armor_value"], errors="coerce").astype("Int64"),
            "has_helmet": _bool(eco["has_helmet"]),
            "has_defuser": _bool(eco["has_defuser"]),
            "inventory": [
                json.dumps([str(i) for i in inv] if isinstance(inv, (list, tuple, np.ndarray)) else [])
                for inv in eco["inventory"]
            ],
            "survived": [(int(r), s) not in died for r, s in zip(eco["round"], eco["steamid"])],
        }
    ).reset_index(drop=True)

    # ---- positions: where everyone is, once per second ----------------------
    sample_ticks: list[int] = []
    for r in rounds.itertuples():
        if r.played:
            sample_ticks.extend(range(int(r.freeze_end_tick), int(r.end_tick) + 1, tick_rate))
    pos_raw = _df(parser.parse_ticks(POS_PROPS, ticks=sample_ticks)) if sample_ticks else pd.DataFrame()
    if pos_raw.empty:
        positions = pd.DataFrame(columns=["round", "tick", "time_s", "steamid", "side", "x", "y", "z", "place", "health", "yaw"])
    else:
        p = pos_raw[_bool(pos_raw["is_alive"])].copy()
        p["round"] = clock.round_of(p["tick"])
        p = p[p["round"].notna()]
        positions = pd.DataFrame(
            {
                "round": p["round"],
                "tick": p["tick"],
                "time_s": clock.time_s(p["round"], p["tick"]).round(0).astype("Int64"),
                "steamid": _sid(p["steamid"]),
                "side": _side(p["team_num"]),
                "x": _round1(p["X"]),
                "y": _round1(p["Y"]),
                "z": _round1(p["Z"]),
                "place": p["last_place_name"],
                "health": pd.to_numeric(p["health"], errors="coerce").astype("Int64"),
                "yaw": _round1(p["yaw"]),
            }
        )
        positions = positions[positions["steamid"].notna() & positions["side"].notna()]
        positions = positions.sort_values(["tick", "steamid"]).reset_index(drop=True)

    # ---- grenades ------------------------------------------------------------
    fire = ev["weapon_fire"]
    throws = fire[fire["weapon"].isin(list(GRENADE_WEAPONS))] if not fire.empty else fire
    throws = _in_rounds(throws, clock)
    if not throws.empty:
        throws["type"] = throws["weapon"].map(GRENADE_WEAPONS)
        throws["thrower"] = _sid(throws["user_steamid"])

    det_frames = []
    for event_name, kind in DETONATIONS.items():
        d = _in_rounds(ev[event_name], clock)
        if not d.empty:
            d["type"] = kind
            d["thrower"] = _sid(_col(d, "user_steamid"))
            det_frames.append(d)
    dets = pd.concat(det_frames, ignore_index=True).sort_values("tick").reset_index(drop=True) if det_frames else pd.DataFrame()

    # pair each detonation with the throw that caused it (same player, same type, first in first out)
    waiting: dict[tuple[str | None, str], list[int]] = {}
    for i, (who, kind) in enumerate(zip(_col(throws, "thrower"), _col(throws, "type"))):
        waiting.setdefault((who, kind), []).append(i)
    det_of_throw: dict[int, int] = {}
    orphans: list[int] = []
    for j in range(len(dets)):
        queue = waiting.get((dets.at[j, "thrower"], dets.at[j, "type"]), [])
        det_tick = int(dets.at[j, "tick"])
        while queue and det_tick - int(throws.at[queue[0], "tick"]) > MAX_FLIGHT_S * tick_rate:
            queue.pop(0)
        if queue and int(throws.at[queue[0], "tick"]) <= det_tick:
            det_of_throw[queue.pop(0)] = j
        else:
            orphans.append(j)

    def _f(frame: pd.DataFrame, i: int, column: str):
        if column not in frame.columns:
            return None
        v = frame.at[i, column]
        return None if pd.isna(v) else v

    grenade_rows = []
    for i in range(len(throws)):
        j = det_of_throw.get(i)
        vx, vy, vz = (_f(throws, i, c) or 0.0 for c in ("user_velocity_X", "user_velocity_Y", "user_velocity_Z"))
        grenade_rows.append(
            {
                "round": int(throws.at[i, "round"]),
                "type": throws.at[i, "type"],
                "thrower_steamid": throws.at[i, "thrower"],
                "thrower_side": SIDE.get(int(_f(throws, i, "user_team_num") or 0)),
                "throw_tick": int(throws.at[i, "tick"]),
                "throw_time_s": _f(throws, i, "time_s"),
                "throw_x": _f(throws, i, "user_X"),
                "throw_y": _f(throws, i, "user_Y"),
                "throw_z": _f(throws, i, "user_Z"),
                "throw_pitch": _f(throws, i, "user_pitch"),
                "throw_yaw": _f(throws, i, "user_yaw"),
                "throw_place": _f(throws, i, "user_last_place_name"),
                "throw_speed": round(float(np.hypot(vx, vy)), 1),
                "throw_in_air": abs(float(vz)) > 1.0,
                "det_tick": int(dets.at[j, "tick"]) if j is not None else None,
                "det_time_s": _f(dets, j, "time_s") if j is not None else None,
                "land_x": _f(dets, j, "x") if j is not None else None,
                "land_y": _f(dets, j, "y") if j is not None else None,
                "land_z": _f(dets, j, "z") if j is not None else None,
                "entity_id": _f(dets, j, "entityid") if j is not None else None,
            }
        )
    for j in orphans:  # went off, but we never saw the throw
        grenade_rows.append(
            {
                "round": int(dets.at[j, "round"]),
                "type": dets.at[j, "type"],
                "thrower_steamid": dets.at[j, "thrower"],
                "thrower_side": SIDE.get(int(_f(dets, j, "user_team_num") or 0)),
                "det_tick": int(dets.at[j, "tick"]),
                "det_time_s": _f(dets, j, "time_s"),
                "land_x": _f(dets, j, "x"),
                "land_y": _f(dets, j, "y"),
                "land_z": _f(dets, j, "z"),
                "entity_id": _f(dets, j, "entityid"),
            }
        )
    grenade_columns = [
        "round", "type", "thrower_steamid", "thrower_side",
        "throw_tick", "throw_time_s", "throw_x", "throw_y", "throw_z", "throw_pitch", "throw_yaw",
        "throw_place", "throw_speed", "throw_in_air",
        "det_tick", "det_time_s", "land_x", "land_y", "land_z", "entity_id",
    ]
    grenades = pd.DataFrame(grenade_rows, columns=grenade_columns)
    for c in ("throw_x", "throw_y", "throw_z", "land_x", "land_y", "land_z"):
        grenades[c] = _round1(grenades[c])
    for c in ("throw_pitch", "throw_yaw"):
        grenades[c] = _round1(grenades[c], 2)
    for c in ("throw_tick", "det_tick", "entity_id"):
        grenades[c] = pd.to_numeric(grenades[c], errors="coerce").astype("Int64")

    # how many players each flash blinded (for at least MIN_BLIND_S seconds)
    flash_stats = {}
    enemy = ~blinds["is_self"] & ~blinds["is_teammate"]
    for ent, tick, dur, is_enemy, is_mate in zip(
        blinds["entity_id"], blinds["tick"], blinds["duration_s"], enemy, blinds["is_teammate"]
    ):
        if pd.isna(ent):
            continue
        s = flash_stats.setdefault((int(ent), int(tick)), [0, 0, 0.0, 0.0])
        if is_enemy:
            s[0] += int(dur >= MIN_BLIND_S)
            s[2] += float(dur)
        elif is_mate:
            s[1] += int(dur >= MIN_BLIND_S)
            s[3] += float(dur)
    stats = [
        flash_stats.get((int(e), int(t)), [0, 0, 0.0, 0.0]) if kind == "flash" and pd.notna(e) and pd.notna(t) else [None] * 4
        for kind, e, t in zip(grenades["type"], grenades["entity_id"], grenades["det_tick"])
    ]
    grenades["enemies_blinded"] = pd.array([s[0] for s in stats], dtype="Int64")
    grenades["teammates_blinded"] = pd.array([s[1] for s in stats], dtype="Int64")
    grenades["enemy_blind_s"] = [None if s[2] is None else round(s[2], 2) for s in stats]
    grenades["team_blind_s"] = [None if s[3] is None else round(s[3], 2) for s in stats]
    blinds = blinds.drop(columns=["entity_id"])

    # ---- area names for landing spots ---------------------------------------
    named_points = pd.concat(
        [
            positions[["x", "y", "z", "place"]],
            kills[["attacker_x", "attacker_y", "attacker_z", "attacker_place"]].set_axis(["x", "y", "z", "place"], axis=1),
            kills[["victim_x", "victim_y", "victim_z", "victim_place"]].set_axis(["x", "y", "z", "place"], axis=1),
            grenades[["throw_x", "throw_y", "throw_z", "throw_place"]].set_axis(["x", "y", "z", "place"], axis=1),
        ],
        ignore_index=True,
    )
    new_cells = places.build_cells(named_points)
    all_cells = places.merge_cells(known_cells, new_cells)
    grenades["land_place"] = places.nearest_place(
        grenades[["land_x", "land_y", "land_z"]].to_numpy(dtype="float64"), all_cells
    )
    new_cells.insert(0, "map", map_name)

    # ---- players and the demo's own scoreboard -------------------------------
    last_end = int(rounds["end_tick"].iloc[-1])
    last_window = int(rounds["window_end_tick"].iloc[-1])
    totals = pd.DataFrame()
    for tick in (last_window, last_end + 3 * tick_rate, last_end):
        totals = _df(parser.parse_ticks(TOTAL_PROPS, ticks=[tick]))
        if not totals.empty:
            break
    names: dict[str, str] = {}
    info = _df(parser.parse_player_info())
    if not info.empty:
        names.update(dict(zip(_sid(info["steamid"]), info["name"].astype(str))))
    if not totals.empty:
        totals = totals.copy()
        totals["steamid"] = _sid(totals["steamid"])
        names.update(dict(zip(totals["steamid"], totals["name"].astype(str))))
        totals = totals.drop_duplicates("steamid").set_index("steamid")

    us = {str(s) for s in team_steam_ids}

    def total(steamid: str, column: str):
        if totals.empty or steamid not in totals.index or column not in totals.columns:
            return None
        v = totals.at[steamid, column]
        return None if pd.isna(v) else int(v)

    players = pd.DataFrame(
        [
            {
                "steamid": s,
                "name": names.get(s, s),
                "team": team,
                "is_us": s in us,
                "demo_kills": total(s, "kills_total"),
                "demo_deaths": total(s, "deaths_total"),
                "demo_assists": total(s, "assists_total"),
                "demo_damage": total(s, "damage_total"),
                "demo_headshot_kills": total(s, "headshot_kills_total"),
                "demo_mvps": total(s, "mvps"),
            }
            for s, team in sorted(team_of.items(), key=lambda item: (item[1], item[0]))
        ]
    )

    # the score the game itself shows at the end, to check our round counting against
    demo_score = {"A": None, "B": None}
    if not totals.empty and "team_rounds_total" in totals.columns:
        last_t_team = t_team_of_round[int(rounds["round"].iloc[-1])]
        side_team = {"T": last_t_team, "CT": other[last_t_team]}
        for side_num, side_name in SIDE.items():
            rows = totals[pd.to_numeric(totals["team_num"], errors="coerce") == side_num]
            if not rows.empty and rows["team_rounds_total"].notna().any():
                demo_score[side_team[side_name]] = int(rows["team_rounds_total"].dropna().max())

    us_in_a = sum(1 for s, t in team_of.items() if s in us and t == "A")
    us_in_b = sum(1 for s, t in team_of.items() if s in us and t == "B")
    us_team = "A" if us_in_a > us_in_b else "B" if us_in_b > us_in_a else None

    server_name = str(header.get("server_name", ""))
    lowered = server_name.lower()
    source = "faceit" if "faceit" in lowered else "valve" if lowered.startswith("valve") else "other"
    try:
        parser_version = pkg_version("demoparser2")
    except PackageNotFoundError:
        parser_version = "unknown"

    match = pd.DataFrame(
        [
            {
                "file_name": dem_path.name,
                "map": map_name,
                "server_name": server_name,
                "source": source,
                "tick_rate": tick_rate,
                "rounds_total": int(len(rounds)),
                "rounds_played": int(rounds["played"].sum()),
                "score_a": int((rounds["winner_team"] == "A").sum()),
                "score_b": int((rounds["winner_team"] == "B").sum()),
                "demo_score_a": demo_score["A"],
                "demo_score_b": demo_score["B"],
                "us_team": us_team,
                "team_a_name": team_names["A"],
                "team_b_name": team_names["B"],
                "file_time": datetime.fromtimestamp(dem_path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
                "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "parser_version": parser_version,
            }
        ]
    )

    rounds = rounds[
        [
            "round", "start_tick", "freeze_end_tick", "end_tick", "window_end_tick",
            "t_team", "ct_team", "winner_side", "winner_team", "reason", "played", "duration_s",
            "score_a_before", "score_b_before", "bomb_planted", "plant_time_s", "plant_site",
        ]
    ].copy()
    rounds["freeze_end_tick"] = pd.to_numeric(rounds["freeze_end_tick"], errors="coerce").astype("Int64")

    tables = {
        "matches": match,
        "players": players,
        "rounds": rounds,
        "player_rounds": player_rounds,
        "kills": kills,
        "damages": damages,
        "grenades": grenades,
        "blinds": blinds,
        "bomb_events": bomb_events,
        "positions": positions,
    }
    for table in tables.values():
        table.insert(0, "match_id", match_id)
    tables["map_places"] = new_cells
    return tables
