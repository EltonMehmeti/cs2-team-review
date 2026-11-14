"""Phase 3: round summaries and the match digest (the text an LLM reads).

Everything here is deterministic: the same database gives the same JSON,
byte for byte. No AI is involved. The LLM in Phase 4 only reads this.

A round summary is written from the point of view of "us" (matches.us_team,
or team A when your Steam IDs are not configured). Our players appear by
name, the other team as "enemy". Example (shortened):

    {"round": 14, "side": "T", "score": "6-7",
     "buy": {"us": "full", "them": "full"},
     "setup_20s": {"P1": "TopofMid", "P2": "dead"},
     "utility": [{"t": 24, "by": "P3", "type": "smoke", "from": "TRamp", "to": "CTSpawn"}],
     "kills": [{"t": 29, "killer": "enemy", "victim": "P3", "weapon": "awp",
                "at": "TRamp", "opening": true, "traded": false}],
     "bomb": null,
     "result": {"won": false, "how": "t_eliminated", "length_s": 52, "alive": "0v3"},
     "flags": ["lost_opening", "opening_death_untraded"]}

Words: `t` is whole seconds since the round went live (end of freeze time).
`setup_20s` is where each of our players stood 20 seconds in. `alive` is
"our living players v theirs". `at` is the area where the victim died.
In a kill, `traded` says whether this death was avenged within 5 seconds
(always given for our deaths), and `trade` marks a kill of ours that avenged
a teammate. `killer: "world"` is the bomb, a fall or the map.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, delete, text

from .db import MatchDigest, RoundSummary

TOKEN_BUDGET = 10_000  # aim for a digest under this many tokens
SETUP_TIME_S = 20  # setup_20s: positions this many seconds into the round
NAMED_ENEMY_MIN_OPENINGS = 3  # an enemy with this many opening kills keeps his name
UTILITY_TYPES = {"smoke", "flash", "he", "molotov"}  # decoys are left out


def estimate_tokens(text_: str) -> int:
    """Rough token count: about 4 characters per token for JSON like ours."""
    return (len(text_) + 3) // 4


def to_json(data, pretty: bool = False) -> str:
    """The one way we turn summaries into text, so two runs give the same bytes."""
    if pretty:
        return json.dumps(data, ensure_ascii=False, indent=2)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------
# pydantic models: the exact shape of the digest (Phase 4 validates against it)
# --------------------------------------------------------------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Buy(_Strict):
    us: str | None = None
    them: str | None = None


class UtilityItem(_Strict):
    t: int
    by: str
    type: str
    to: str | None = None
    enemies_blinded: int | None = None
    mates_blinded: int | None = None
    # "from" is a Python keyword, so the field is called from_ and written as "from"
    from_: str | None = Field(default=None, alias="from")


class KillItem(_Strict):
    t: int
    killer: str
    victim: str
    weapon: str | None = None
    at: str | None = None
    opening: bool | None = None
    traded: bool | None = None
    trade: bool | None = None
    teamkill: bool | None = None
    headshot: bool | None = None


class Bomb(_Strict):
    site: str | None = None
    t: int
    by: str
    alive: str
    outcome: str | None = None


class Result(_Strict):
    won: bool
    how: str
    length_s: int
    alive: str
    clutch: str | None = None


class RoundSummaryModel(_Strict):
    round: int
    side: str
    score: str
    buy: Buy
    setup_20s: dict[str, str] | None = None
    utility: list[UtilityItem] | None = None
    enemy_utility: dict[str, int] | None = None
    kills: list[KillItem] | None = None
    bomb: Bomb | None = None
    result: Result
    flags: list[str] | None = None


class Rate(_Strict):
    won: int
    n: int
    pct: int | None = None  # empty when n is 0


class MatchInfo(_Strict):
    match_id: str
    map: str
    us: str
    them: str
    final_score: str
    won: bool | None = None
    halves: list[dict[str, str]]
    rounds_played: int
    date: str | None = None


class PlayerLine(_Strict):
    name: str
    k: int
    d: int
    a: int
    adr: float
    kast_pct: int
    opening_kills: int
    opening_deaths: int
    trade_kills: int
    untraded_deaths: int
    flashes_thrown: int
    teammates_blinded: int


class TeamStats(_Strict):
    flags: dict[str, int]
    win_by_buy: dict[str, Rate]
    win_by_side: dict[str, Rate]
    postplant: Rate
    retake: Rate
    opening_duels: Rate
    trade_rate: Rate  # won = traded deaths, n = deaths to an enemy


class Digest(_Strict):
    match: MatchInfo
    players: list[PlayerLine]
    team: TeamStats
    rounds: list[RoundSummaryModel]
    dropped: list[str] | None = None  # fields removed to stay under the token budget


# --------------------------------------------------------------------------
# the data of one match, loaded once
# --------------------------------------------------------------------------

def _bools(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    frame = frame.copy()
    for column in columns:
        if column in frame.columns:
            frame[column] = frame[column].fillna(0).astype(bool)
    return frame


@dataclass
class MatchContext:
    """All tables of one match plus who is "us". Built by from_tables()."""

    match: dict
    players: pd.DataFrame
    rounds: pd.DataFrame
    team_rounds: pd.DataFrame
    player_round_stats: pd.DataFrame
    kills: pd.DataFrame
    grenades: pd.DataFrame
    positions: pd.DataFrame
    bomb_events: pd.DataFrame
    us: str = "A"
    them: str = "B"
    names: dict[str, str] = field(default_factory=dict)  # steamid -> label ("P3" or "enemy")
    team_of: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_tables(cls, tables: dict[str, pd.DataFrame]) -> "MatchContext":
        match = tables["matches"].iloc[0].to_dict()
        us = match.get("us_team") if match.get("us_team") in ("A", "B") else "A"
        them = "B" if us == "A" else "A"
        players = tables["players"]
        kills = _bools(tables["kills"], ["after_round_end", "is_suicide", "is_teamkill", "is_opening", "is_trade",
                                         "was_traded", "headshot"])
        team_of = dict(zip(players["steamid"], players["team"]))

        # Our players by name. Enemies are "enemy", except one who kept
        # winning opening duels (an AWPer, usually): his name matters.
        names: dict[str, str] = {}
        openings = kills[kills["is_opening"]].groupby("attacker_steamid").size() if not kills.empty else pd.Series(dtype=int)
        for steamid, name, team in zip(players["steamid"], players["name"], players["team"]):
            if team == us:
                names[steamid] = str(name)
            elif int(openings.get(steamid, 0)) >= NAMED_ENEMY_MIN_OPENINGS:
                names[steamid] = f"enemy {name}"
            else:
                names[steamid] = "enemy"

        def get(name):
            return tables.get(name, pd.DataFrame())

        return cls(
            match=match, players=players, rounds=_bools(tables["rounds"], ["played", "bomb_planted"]),
            team_rounds=_bools(get("team_rounds"), ["won", "planted", "is_us"]),
            player_round_stats=_bools(get("player_round_stats"), ["kast", "survived", "was_traded", "died"]),
            kills=kills, grenades=get("grenades"), positions=get("positions"), bomb_events=get("bomb_events"),
            us=us, them=them, names=names, team_of=team_of,
        )

    def label(self, steamid) -> str:
        if steamid is None or (isinstance(steamid, float) and pd.isna(steamid)):
            return "world"  # the bomb, falling, the map
        return self.names.get(steamid, "enemy")

    def is_ours(self, steamid) -> bool:
        return self.team_of.get(steamid) == self.us


def _t(seconds) -> int:
    """Whole seconds since the round went live."""
    return int(float(seconds)) if pd.notna(seconds) else 0


def _place(value) -> str | None:
    return None if value is None or (isinstance(value, float) and pd.isna(value)) or value == "" else str(value)


# --------------------------------------------------------------------------
# one round
# --------------------------------------------------------------------------

def round_summary(ctx: MatchContext, rnd: int) -> dict:
    """The summary of one played round, as a plain dict (see the module docstring)."""
    r = ctx.rounds[ctx.rounds["round"] == rnd].iloc[0]
    side = "T" if r["t_team"] == ctx.us else "CT"
    tr = ctx.team_rounds[ctx.team_rounds["round"] == rnd]
    us_row = tr[tr["team"] == ctx.us].iloc[0]
    them_row = tr[tr["team"] == ctx.them].iloc[0]
    score_us = int(r["score_a_before"] if ctx.us == "A" else r["score_b_before"])
    score_them = int(r["score_b_before"] if ctx.us == "A" else r["score_a_before"])
    end_tick = int(r["end_tick"])

    out: dict = {
        "round": int(rnd),
        "side": side,
        "score": f"{score_us}-{score_them}",
        "buy": {"us": us_row["buy_type"], "them": them_row["buy_type"]},
    }

    # ---- where our players stood 20 seconds in ------------------------------
    kills = ctx.kills[(ctx.kills["round"] == rnd) & ~ctx.kills["after_round_end"]].sort_values(["tick", "id"])
    if pd.notna(r["duration_s"]) and float(r["duration_s"]) >= SETUP_TIME_S:
        pos = ctx.positions[(ctx.positions["round"] == rnd) & (ctx.positions["time_s"] == SETUP_TIME_S)]
        place_of = dict(zip(pos["steamid"], pos["place"]))
        died_before = set(kills.loc[kills["time_s"] < SETUP_TIME_S, "victim_steamid"])
        ours = ctx.player_round_stats[(ctx.player_round_stats["round"] == rnd) & (ctx.player_round_stats["team"] == ctx.us)]
        setup = {}
        for steamid in sorted(ours["steamid"], key=lambda s: ctx.label(s)):
            if steamid in died_before:
                setup[ctx.label(steamid)] = "dead"
            elif _place(place_of.get(steamid)):
                setup[ctx.label(steamid)] = str(place_of[steamid])
        if setup:
            out["setup_20s"] = setup

    # ---- grenades: ours one by one, theirs as counts --------------------------
    g = ctx.grenades[(ctx.grenades["round"] == rnd) & ctx.grenades["type"].isin(UTILITY_TYPES)].copy()
    if not g.empty:
        g["when_tick"] = pd.to_numeric(g["throw_tick"], errors="coerce").fillna(pd.to_numeric(g["det_tick"], errors="coerce"))
        g["when_s"] = pd.to_numeric(g["throw_time_s"], errors="coerce").fillna(pd.to_numeric(g["det_time_s"], errors="coerce"))
        g = g[g["when_tick"].notna() & (g["when_tick"] <= end_tick)].sort_values(["when_tick", "id"])
        our_side = g["thrower_side"] == side
        utility = []
        for row in g[our_side].itertuples():
            item = {"t": _t(row.when_s), "by": ctx.label(row.thrower_steamid), "type": row.type}
            if row.type == "flash":
                item["enemies_blinded"] = int(row.enemies_blinded) if pd.notna(row.enemies_blinded) else 0
                if pd.notna(row.teammates_blinded) and int(row.teammates_blinded) > 0:
                    item["mates_blinded"] = int(row.teammates_blinded)
            else:
                if row.type == "smoke" and _place(row.throw_place):
                    item["from"] = str(row.throw_place)
                if _place(row.land_place):
                    item["to"] = str(row.land_place)
            utility.append(item)
        if utility:
            out["utility"] = utility
        enemy_counts = g[~our_side]["type"].value_counts()
        if not enemy_counts.empty:
            out["enemy_utility"] = {kind: int(enemy_counts[kind]) for kind in ("smoke", "flash", "he", "molotov") if kind in enemy_counts}

    # ---- kills before the round ended ----------------------------------------
    kill_items = []
    for k in kills.itertuples():
        item = {"t": _t(k.time_s), "killer": ctx.label(k.attacker_steamid), "victim": ctx.label(k.victim_steamid)}
        if k.is_suicide:
            item["killer"] = "world" if k.attacker_steamid is None or pd.isna(k.attacker_steamid) else "self"
        if _place(k.weapon):
            item["weapon"] = str(k.weapon)
        if _place(k.victim_place):
            item["at"] = str(k.victim_place)
        if k.is_opening:
            item["opening"] = True
        if k.is_teamkill:
            item["teamkill"] = True
        elif not k.is_suicide:
            if ctx.is_ours(k.victim_steamid):
                item["traded"] = bool(k.was_traded)  # did we avenge our teammate?
            elif k.was_traded:
                item["traded"] = True
            if k.is_trade and ctx.is_ours(k.attacker_steamid):
                item["trade"] = True
        kill_items.append(item)
    if kill_items:
        out["kills"] = kill_items

    # ---- the bomb ---------------------------------------------------------------
    if bool(us_row["planted"]):
        plants = ctx.bomb_events[(ctx.bomb_events["round"] == rnd) & (ctx.bomb_events["event"] == "plant")].sort_values("tick")
        plant = plants.iloc[0]
        bomb = {"site": _place(us_row["plant_site"]), "t": _t(us_row["plant_time_s"]), "by": ctx.label(plant["steamid"]),
                "alive": f"{int(us_row['alive_at_plant'])}v{int(us_row['enemy_alive_at_plant'])}"}
        if r["reason"] in ("bomb_exploded", "bomb_defused"):
            bomb["outcome"] = "exploded" if r["reason"] == "bomb_exploded" else "defused"
        out["bomb"] = {k: v for k, v in bomb.items() if v is not None}
    else:
        out["bomb"] = None

    # ---- how it ended -----------------------------------------------------------
    result = {
        "won": bool(us_row["won"]),
        "how": str(r["reason"]),
        "length_s": int(float(r["duration_s"])) if pd.notna(r["duration_s"]) else 0,
        "alive": f"{int(us_row['survivors'])}v{int(them_row['survivors'])}",
    }
    for row, team_word in ((us_row, "us"), (them_row, "them")):
        if int(row["clutch_n"]) > 0:
            clutcher = ctx.player_round_stats[
                (ctx.player_round_stats["round"] == rnd) & (ctx.player_round_stats["team"] == row["team"])
                & (ctx.player_round_stats["clutch_n"] > 0)
            ]
            who = ctx.label(clutcher["steamid"].iloc[0]) if not clutcher.empty else team_word
            if team_word == "us":  # our clutch matters most; theirs only if we had none
                result["clutch"] = f"{who} 1v{int(row['clutch_n'])}"
            elif "clutch" not in result:
                result["clutch"] = f"enemy 1v{int(row['clutch_n'])}"
    out["result"] = result

    flags = json.loads(us_row["flags"]) if isinstance(us_row["flags"], str) else []
    if flags:
        out["flags"] = flags
    return out


# --------------------------------------------------------------------------
# the whole match
# --------------------------------------------------------------------------

def _rate(won: int, n: int) -> dict:
    return {"won": int(won), "n": int(n), "pct": round(100 * won / n) if n else None}


def _drop_none(d):
    """Remove empty fields (None) from a nested dict/list structure."""
    if isinstance(d, dict):
        return {k: _drop_none(v) for k, v in d.items() if v is not None or k == "bomb"}
    if isinstance(d, list):
        return [_drop_none(v) for v in d]
    return d


def match_digest(ctx: MatchContext) -> dict:
    """The full digest: match facts, our players, team rates, every round."""
    played = ctx.rounds[ctx.rounds["played"]].sort_values("round")
    ours = ctx.team_rounds[ctx.team_rounds["team"] == ctx.us].sort_values("round")
    us_score = int(ctx.match["score_a"] if ctx.us == "A" else ctx.match["score_b"])
    them_score = int(ctx.match["score_b"] if ctx.us == "A" else ctx.match["score_a"])

    # halves: runs of rounds where we kept the same side
    halves: list[dict[str, str]] = []
    for row in ours.itertuples():
        if halves and halves[-1]["side"] == row.side:
            halves[-1]["rounds"] = halves[-1]["rounds"].split("-")[0] + f"-{row.round}"
        else:
            halves.append({"rounds": f"{row.round}-{row.round}", "side": row.side})

    name_us = ctx.match.get(f"team_{ctx.us.lower()}_name")
    name_them = ctx.match.get(f"team_{ctx.them.lower()}_name")
    info = {
        "match_id": str(ctx.match["match_id"]),
        "map": str(ctx.match["map"]),
        "us": name_us if isinstance(name_us, str) and name_us else f"team {ctx.us}",
        "them": name_them if isinstance(name_them, str) and name_them else f"team {ctx.them}",
        "final_score": f"{us_score}-{them_score}",
        "won": None if us_score == them_score else us_score > them_score,
        "halves": halves,
        "rounds_played": int(len(played)),
        "date": None,  # the demo has no match date (PLAN.md section 4)
    }

    # ---- players --------------------------------------------------------------
    stats = ctx.player_round_stats
    n_rounds = max(int(len(played)), 1)
    live_real = ctx.kills[~ctx.kills["after_round_end"] & ~ctx.kills["is_suicide"] & ~ctx.kills["is_teamkill"]]
    untraded = live_real[~live_real["was_traded"]].groupby("victim_steamid").size()
    g = ctx.grenades
    if not g.empty:
        ends = dict(zip(ctx.rounds["round"], ctx.rounds["end_tick"]))
        when = pd.to_numeric(g["throw_tick"], errors="coerce").fillna(pd.to_numeric(g["det_tick"], errors="coerce"))
        g = g[when <= g["round"].map(ends)]
    flashes = g[g["type"] == "flash"] if not g.empty else g
    players = []
    for steamid, s in stats[stats["team"] == ctx.us].groupby("steamid"):
        players.append({
            "name": ctx.label(steamid),
            "k": int(s["kills"].sum()), "d": int(s["died"].sum()), "a": int(s["assists"].sum()),
            "adr": round(float(s["damage"].sum()) / n_rounds, 1),
            "kast_pct": round(100 * int(s["kast"].sum()) / max(len(s), 1)),
            "opening_kills": int(s["opening_kill"].sum()), "opening_deaths": int(s["opening_death"].sum()),
            "trade_kills": int(s["trade_kills"].sum()), "untraded_deaths": int(untraded.get(steamid, 0)),
            "flashes_thrown": int((flashes["thrower_steamid"] == steamid).sum()) if not flashes.empty else 0,
            "teammates_blinded": int(s["teammates_blinded"].sum()),
        })
    players.sort(key=lambda p: (-p["k"], p["name"]))

    # ---- team rates -------------------------------------------------------------
    flag_counts: dict[str, int] = {}
    for flags in ours["flags"]:
        for f in json.loads(flags):
            flag_counts[f] = flag_counts.get(f, 0) + 1
    win_by_buy = {
        buy: _rate(int(grp["won"].sum()), len(grp))
        for buy, grp in ours.groupby(ours["buy_type"].fillna("unknown"), sort=True)
    }
    win_by_side = {side: _rate(int(grp["won"].sum()), len(grp)) for side, grp in ours.groupby("side", sort=True)}
    t_plants = ours[(ours["side"] == "T") & ours["planted"]]
    ct_plants = ours[(ours["side"] == "CT") & ours["planted"]]
    with_opening = ours[ours["opening"].notna()]
    team = {
        "flags": dict(sorted(flag_counts.items())),
        "win_by_buy": win_by_buy,
        "win_by_side": win_by_side,
        "postplant": _rate(int(t_plants["won"].sum()), len(t_plants)),
        "retake": _rate(int(ct_plants["won"].sum()), len(ct_plants)),
        "opening_duels": _rate(int((with_opening["opening"] == "won").sum()), len(with_opening)),
        "trade_rate": _rate(int(ours["traded_deaths"].sum()), int(ours["traded_deaths"].sum() + ours["untraded_deaths"].sum())),
    }

    rounds = [round_summary(ctx, int(rnd)) for rnd in played["round"]]
    return _drop_none({"match": info, "players": players, "team": team, "rounds": rounds})


# --------------------------------------------------------------------------
# token budget
# --------------------------------------------------------------------------

def _drop_non_opening_kill_areas(d: dict) -> None:
    for rnd in d["rounds"]:
        for k in rnd.get("kills", []):
            if not k.get("opening"):
                k.pop("at", None)


def _drop_utility_from(d: dict) -> None:
    for rnd in d["rounds"]:
        for u in rnd.get("utility", []):
            u.pop("from", None)


def _drop_enemy_details(d: dict) -> None:
    for rnd in d["rounds"]:
        rnd.pop("enemy_utility", None)
        for k in rnd.get("kills", []):
            for key in ("killer", "victim"):
                if str(k.get(key, "")).startswith("enemy "):
                    k[key] = "enemy"
        clutch = rnd.get("result", {}).get("clutch", "")
        if clutch.startswith("enemy "):
            rnd["result"]["clutch"] = "enemy " + clutch.split(" ")[-1]


# in this order, as PLAN.md says
DROP_STEPS = [
    ("kill areas of non-opening kills", _drop_non_opening_kill_areas),
    ("utility from", _drop_utility_from),
    ("enemy details", _drop_enemy_details),
]


def fit_budget(digest: dict, budget: int = TOKEN_BUDGET) -> tuple[dict, int]:
    """Drop less important fields until the digest fits. Returns (digest, tokens)."""
    digest = json.loads(to_json(digest))  # a deep copy
    tokens = estimate_tokens(to_json(digest))
    for label, step in DROP_STEPS:
        if tokens <= budget:
            break
        step(digest)
        digest.setdefault("dropped", []).append(label)
        tokens = estimate_tokens(to_json(digest))
    return digest, tokens


# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------

DIGEST_TABLES = ["matches", "players", "rounds", "team_rounds", "player_round_stats", "kills", "grenades", "bomb_events"]


def load_context(engine: Engine, match_id: str) -> MatchContext:
    with engine.connect() as conn:
        tables = {name: pd.read_sql(text(f"SELECT * FROM {name} WHERE match_id = :m"), conn, params={"m": match_id})
                  for name in DIGEST_TABLES}
        tables["positions"] = pd.read_sql(
            text("SELECT round, steamid, time_s, place FROM positions WHERE match_id = :m AND time_s = :t"),
            conn, params={"m": match_id, "t": SETUP_TIME_S},
        )
    if tables["matches"].empty:
        raise ValueError(f"Unknown match id: {match_id}")
    return MatchContext.from_tables(tables)


def build_digest(engine: Engine, match_id: str, budget: int = TOKEN_BUDGET) -> tuple[dict, int]:
    """Build, validate and store the digest of one match. Returns (digest, tokens)."""
    ctx = load_context(engine, match_id)
    digest, tokens = fit_budget(match_digest(ctx), budget)
    Digest.model_validate(digest)  # raises if the shape is wrong
    with engine.begin() as conn:
        conn.execute(delete(RoundSummary).where(RoundSummary.match_id == match_id))
        conn.execute(delete(MatchDigest).where(MatchDigest.match_id == match_id))
        if digest["rounds"]:
            conn.execute(
                RoundSummary.__table__.insert(),
                [{"match_id": match_id, "round": rs["round"], "json": to_json(rs)} for rs in digest["rounds"]],
            )
        conn.execute(MatchDigest.__table__.insert().values(match_id=match_id, json=to_json(digest), token_estimate=tokens))
    return digest, tokens
