"""The SQLite database: table definitions and save/load helpers.

One row in `matches` per demo. Every other table has a match_id column, so
deleting or re-ingesting a match is a simple delete by match_id.

The database is a cache: it can always be rebuilt from the demo files. When a
table changes, SCHEMA_VERSION goes up and old databases are refused with a
message that says how to rebuild (delete the file, run the ingest again).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import Boolean, Engine, Float, Index, Integer, String, Text, create_engine, delete, func, inspect, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from . import places


# Bump this whenever a table or column changes. Version 1 was Phase 1 (no meta
# table). Version 2 added the Phase 2 metrics: team_rounds, player_round_stats
# and five new columns on kills. Version 3 added round_summaries and
# match_digests (Phase 3). Version 4 added llm_calls and reports (Phase 4).
# Version 5 added the views our_rounds, our_player_rounds, our_kills and
# reports.kind (Phase 5). Version 6 added round_plays (Phase 6).
SCHEMA_VERSION = 6


class SchemaMismatchError(RuntimeError):
    """The database file was made by a different version of the code."""


class Base(DeclarativeBase):
    pass


class Meta(Base):
    """Facts about the database itself. Today only the key 'schema_version'."""

    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str | None] = mapped_column(String)


class Match(Base):
    """One demo file."""

    __tablename__ = "matches"
    match_id: Mapped[str] = mapped_column(String, primary_key=True)
    file_name: Mapped[str] = mapped_column(String)
    map: Mapped[str] = mapped_column(String, index=True)
    server_name: Mapped[str | None] = mapped_column(String)
    source: Mapped[str | None] = mapped_column(String)  # faceit / valve / other
    tick_rate: Mapped[int] = mapped_column(Integer)
    rounds_total: Mapped[int] = mapped_column(Integer)
    rounds_played: Mapped[int] = mapped_column(Integer)
    score_a: Mapped[int] = mapped_column(Integer)  # rounds won, counted by us
    score_b: Mapped[int] = mapped_column(Integer)
    demo_score_a: Mapped[int | None] = mapped_column(Integer)  # the game's own final score
    demo_score_b: Mapped[int | None] = mapped_column(Integer)
    us_team: Mapped[str | None] = mapped_column(String)  # "A" or "B" if your Steam IDs are configured
    team_a_name: Mapped[str | None] = mapped_column(String)  # in-game team names, when the demo has them
    team_b_name: Mapped[str | None] = mapped_column(String)
    file_time: Mapped[str | None] = mapped_column(String)
    ingested_at: Mapped[str | None] = mapped_column(String)
    parser_version: Mapped[str | None] = mapped_column(String)


class Player(Base):
    """One player in one match, with the game's own scoreboard numbers."""

    __tablename__ = "players"
    match_id: Mapped[str] = mapped_column(String, primary_key=True)
    steamid: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str | None] = mapped_column(String)
    team: Mapped[str | None] = mapped_column(String)  # "A" starts on T, "B" starts on CT
    is_us: Mapped[bool] = mapped_column(Boolean, default=False)
    demo_kills: Mapped[int | None] = mapped_column(Integer)
    demo_deaths: Mapped[int | None] = mapped_column(Integer)
    demo_assists: Mapped[int | None] = mapped_column(Integer)
    demo_damage: Mapped[int | None] = mapped_column(Integer)
    demo_headshot_kills: Mapped[int | None] = mapped_column(Integer)
    demo_mvps: Mapped[int | None] = mapped_column(Integer)


class Round(Base):
    """One round of one match."""

    __tablename__ = "rounds"
    match_id: Mapped[str] = mapped_column(String, primary_key=True)
    round: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_tick: Mapped[int] = mapped_column(Integer)
    freeze_end_tick: Mapped[int | None] = mapped_column(Integer)  # the round goes live here
    end_tick: Mapped[int] = mapped_column(Integer)
    window_end_tick: Mapped[int] = mapped_column(Integer)  # last tick before the next round
    t_team: Mapped[str | None] = mapped_column(String)
    ct_team: Mapped[str | None] = mapped_column(String)
    winner_side: Mapped[str | None] = mapped_column(String)
    winner_team: Mapped[str | None] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(String)
    played: Mapped[bool] = mapped_column(Boolean)  # False for a surrender in freeze time
    duration_s: Mapped[float | None] = mapped_column(Float)
    score_a_before: Mapped[int] = mapped_column(Integer)
    score_b_before: Mapped[int] = mapped_column(Integer)
    bomb_planted: Mapped[bool] = mapped_column(Boolean)
    plant_time_s: Mapped[float | None] = mapped_column(Float)
    plant_site: Mapped[str | None] = mapped_column(String)


class PlayerRound(Base):
    """Money and gear of one player at the moment a round goes live."""

    __tablename__ = "player_rounds"
    match_id: Mapped[str] = mapped_column(String, primary_key=True)
    round: Mapped[int] = mapped_column(Integer, primary_key=True)
    steamid: Mapped[str] = mapped_column(String, primary_key=True)
    side: Mapped[str | None] = mapped_column(String)
    team: Mapped[str | None] = mapped_column(String)
    money_start: Mapped[int | None] = mapped_column(Integer)
    money_spent: Mapped[int | None] = mapped_column(Integer)
    money_left: Mapped[int | None] = mapped_column(Integer)
    equip_value: Mapped[int | None] = mapped_column(Integer)  # value of everything carried
    start_equip_value: Mapped[int | None] = mapped_column(Integer)  # value before buying
    armor: Mapped[int | None] = mapped_column(Integer)
    has_helmet: Mapped[bool | None] = mapped_column(Boolean)
    has_defuser: Mapped[bool | None] = mapped_column(Boolean)
    inventory: Mapped[str | None] = mapped_column(Text)  # JSON list of item names
    survived: Mapped[bool | None] = mapped_column(Boolean)  # alive when the round ended


class Kill(Base):
    __tablename__ = "kills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String)
    round: Mapped[int] = mapped_column(Integer)
    tick: Mapped[int] = mapped_column(Integer)
    time_s: Mapped[float | None] = mapped_column(Float)
    after_round_end: Mapped[bool] = mapped_column(Boolean)
    attacker_steamid: Mapped[str | None] = mapped_column(String)
    attacker_side: Mapped[str | None] = mapped_column(String)
    attacker_place: Mapped[str | None] = mapped_column(String)
    attacker_x: Mapped[float | None] = mapped_column(Float)
    attacker_y: Mapped[float | None] = mapped_column(Float)
    attacker_z: Mapped[float | None] = mapped_column(Float)
    victim_steamid: Mapped[str | None] = mapped_column(String)
    victim_side: Mapped[str | None] = mapped_column(String)
    victim_place: Mapped[str | None] = mapped_column(String)
    victim_x: Mapped[float | None] = mapped_column(Float)
    victim_y: Mapped[float | None] = mapped_column(Float)
    victim_z: Mapped[float | None] = mapped_column(Float)
    assister_steamid: Mapped[str | None] = mapped_column(String)
    flash_assist: Mapped[bool | None] = mapped_column(Boolean)
    weapon: Mapped[str | None] = mapped_column(String)
    headshot: Mapped[bool | None] = mapped_column(Boolean)
    through_smoke: Mapped[bool | None] = mapped_column(Boolean)
    attacker_blind: Mapped[bool | None] = mapped_column(Boolean)
    noscope: Mapped[bool | None] = mapped_column(Boolean)
    wallbang: Mapped[bool | None] = mapped_column(Boolean)
    distance: Mapped[float | None] = mapped_column(Float)
    is_suicide: Mapped[bool | None] = mapped_column(Boolean)
    is_teamkill: Mapped[bool | None] = mapped_column(Boolean)
    # filled by metrics.py (Phase 2); empty for kills after the round ended
    is_opening: Mapped[bool | None] = mapped_column(Boolean)  # first real kill of the round
    is_trade: Mapped[bool | None] = mapped_column(Boolean)  # this kill avenged a teammate within the trade window
    was_traded: Mapped[bool | None] = mapped_column(Boolean)  # this death was avenged within the trade window
    t_alive_after: Mapped[int | None] = mapped_column(Integer)  # living Ts after this tick's deaths
    ct_alive_after: Mapped[int | None] = mapped_column(Integer)  # living CTs after this tick's deaths
    __table_args__ = (Index("ix_kills_match_round", "match_id", "round"),)


class Damage(Base):
    __tablename__ = "damages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String)
    round: Mapped[int] = mapped_column(Integer)
    tick: Mapped[int] = mapped_column(Integer)
    time_s: Mapped[float | None] = mapped_column(Float)
    after_round_end: Mapped[bool] = mapped_column(Boolean)
    attacker_steamid: Mapped[str | None] = mapped_column(String)
    attacker_side: Mapped[str | None] = mapped_column(String)
    victim_steamid: Mapped[str | None] = mapped_column(String)
    victim_side: Mapped[str | None] = mapped_column(String)
    weapon: Mapped[str | None] = mapped_column(String)
    hitgroup: Mapped[str | None] = mapped_column(String)
    dmg_health_raw: Mapped[int | None] = mapped_column(Integer)  # as reported, can exceed remaining health
    dmg_armor: Mapped[int | None] = mapped_column(Integer)
    health_after: Mapped[int | None] = mapped_column(Integer)
    dmg_health: Mapped[int | None] = mapped_column(Integer)  # health really removed; use this for ADR
    is_self: Mapped[bool | None] = mapped_column(Boolean)
    is_teamdamage: Mapped[bool | None] = mapped_column(Boolean)
    __table_args__ = (Index("ix_damages_match_round", "match_id", "round"),)


class Grenade(Base):
    """One thrown grenade: where it was thrown from and where it went off."""

    __tablename__ = "grenades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String)
    round: Mapped[int] = mapped_column(Integer)
    type: Mapped[str | None] = mapped_column(String)  # smoke / flash / he / molotov / decoy
    thrower_steamid: Mapped[str | None] = mapped_column(String)
    thrower_side: Mapped[str | None] = mapped_column(String)
    throw_tick: Mapped[int | None] = mapped_column(Integer)
    throw_time_s: Mapped[float | None] = mapped_column(Float)
    throw_x: Mapped[float | None] = mapped_column(Float)
    throw_y: Mapped[float | None] = mapped_column(Float)
    throw_z: Mapped[float | None] = mapped_column(Float)
    throw_pitch: Mapped[float | None] = mapped_column(Float)  # setang <pitch> <yaw> 0
    throw_yaw: Mapped[float | None] = mapped_column(Float)
    throw_place: Mapped[str | None] = mapped_column(String)
    throw_speed: Mapped[float | None] = mapped_column(Float)  # 0 = standing throw
    throw_in_air: Mapped[bool | None] = mapped_column(Boolean)  # True = jump throw
    det_tick: Mapped[int | None] = mapped_column(Integer)  # empty if it never went off
    det_time_s: Mapped[float | None] = mapped_column(Float)
    land_x: Mapped[float | None] = mapped_column(Float)
    land_y: Mapped[float | None] = mapped_column(Float)
    land_z: Mapped[float | None] = mapped_column(Float)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    enemies_blinded: Mapped[int | None] = mapped_column(Integer)  # flashes only
    teammates_blinded: Mapped[int | None] = mapped_column(Integer)
    enemy_blind_s: Mapped[float | None] = mapped_column(Float)
    team_blind_s: Mapped[float | None] = mapped_column(Float)
    land_place: Mapped[str | None] = mapped_column(String)  # approximate, see places.py
    __table_args__ = (Index("ix_grenades_match_round", "match_id", "round"),)


class Blind(Base):
    """One player blinded by one flashbang."""

    __tablename__ = "blinds"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String)
    round: Mapped[int] = mapped_column(Integer)
    tick: Mapped[int] = mapped_column(Integer)
    time_s: Mapped[float | None] = mapped_column(Float)
    thrower_steamid: Mapped[str | None] = mapped_column(String)
    thrower_side: Mapped[str | None] = mapped_column(String)
    victim_steamid: Mapped[str | None] = mapped_column(String)
    victim_side: Mapped[str | None] = mapped_column(String)
    duration_s: Mapped[float | None] = mapped_column(Float)
    is_self: Mapped[bool | None] = mapped_column(Boolean)
    is_teammate: Mapped[bool | None] = mapped_column(Boolean)
    __table_args__ = (Index("ix_blinds_match_round", "match_id", "round"),)


class BombEvent(Base):
    __tablename__ = "bomb_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String)
    round: Mapped[int] = mapped_column(Integer)
    tick: Mapped[int] = mapped_column(Integer)
    time_s: Mapped[float | None] = mapped_column(Float)
    event: Mapped[str] = mapped_column(String)  # plant / defuse / explode
    steamid: Mapped[str | None] = mapped_column(String)
    place: Mapped[str | None] = mapped_column(String)
    x: Mapped[float | None] = mapped_column(Float)
    y: Mapped[float | None] = mapped_column(Float)
    z: Mapped[float | None] = mapped_column(Float)
    site: Mapped[str | None] = mapped_column(String)  # A / B
    __table_args__ = (Index("ix_bomb_match_round", "match_id", "round"),)


class Position(Base):
    """Where each living player is, once per second while the round is live."""

    __tablename__ = "positions"
    match_id: Mapped[str] = mapped_column(String, primary_key=True)
    round: Mapped[int] = mapped_column(Integer, primary_key=True)
    tick: Mapped[int] = mapped_column(Integer, primary_key=True)
    steamid: Mapped[str] = mapped_column(String, primary_key=True)
    time_s: Mapped[int | None] = mapped_column(Integer)
    side: Mapped[str | None] = mapped_column(String)
    x: Mapped[float | None] = mapped_column(Float)
    y: Mapped[float | None] = mapped_column(Float)
    z: Mapped[float | None] = mapped_column(Float)
    place: Mapped[str | None] = mapped_column(String)
    health: Mapped[int | None] = mapped_column(Integer)
    yaw: Mapped[float | None] = mapped_column(Float)


class TeamRound(Base):
    """One team in one played round: how the round went for them (metrics.py).

    The main table for everything after Phase 2. Kills, deaths, openings,
    trades, advantages and clutches only use kills from before the round ended.
    """

    __tablename__ = "team_rounds"
    match_id: Mapped[str] = mapped_column(String, primary_key=True)
    round: Mapped[int] = mapped_column(Integer, primary_key=True)
    team: Mapped[str] = mapped_column(String, primary_key=True)  # "A" or "B"
    side: Mapped[str | None] = mapped_column(String)  # "T" or "CT" in this round
    is_us: Mapped[bool] = mapped_column(Boolean)
    won: Mapped[bool] = mapped_column(Boolean)
    buy_type: Mapped[str | None] = mapped_column(String)  # pistol / eco / semi / full
    enemy_buy_type: Mapped[str | None] = mapped_column(String)
    equip_total: Mapped[int | None] = mapped_column(Integer)  # value of the team's gear at freeze end
    players: Mapped[int] = mapped_column(Integer)  # players at freeze end, normally 5
    opening: Mapped[str | None] = mapped_column(String)  # "won" / "lost" / empty if no real kill
    opening_time_s: Mapped[float | None] = mapped_column(Float)
    opening_traded: Mapped[bool | None] = mapped_column(Boolean)  # our opening death was traded (only if lost)
    kills: Mapped[int] = mapped_column(Integer)  # kills of enemies
    deaths: Mapped[int] = mapped_column(Integer)  # deaths of any kind (also suicides, teamkills, the bomb)
    traded_deaths: Mapped[int] = mapped_column(Integer)
    untraded_deaths: Mapped[int] = mapped_column(Integer)  # killed by an enemy and not traded
    best_advantage: Mapped[int] = mapped_column(Integer)  # max of (own alive - enemy alive)
    worst_disadvantage: Mapped[int] = mapped_column(Integer)  # min of (own alive - enemy alive)
    clutch_n: Mapped[int] = mapped_column(Integer)  # N of the first 1vN, 0 if there was none
    clutch_won: Mapped[bool | None] = mapped_column(Boolean)  # empty when clutch_n is 0
    smokes: Mapped[int] = mapped_column(Integer)
    flashes: Mapped[int] = mapped_column(Integer)
    hes: Mapped[int] = mapped_column(Integer)
    molotovs: Mapped[int] = mapped_column(Integer)  # molotovs and incendiaries
    enemies_blinded: Mapped[int] = mapped_column(Integer)  # for 1 second or more
    teammates_blinded: Mapped[int] = mapped_column(Integer)
    utility_damage: Mapped[int] = mapped_column(Integer)  # HE and fire damage to enemies
    planted: Mapped[bool] = mapped_column(Boolean)  # a plant before the round ended (either side's row)
    plant_site: Mapped[str | None] = mapped_column(String)
    plant_time_s: Mapped[float | None] = mapped_column(Float)
    postplant_s: Mapped[float | None] = mapped_column(Float)  # seconds from the plant to the round end
    alive_at_plant: Mapped[int | None] = mapped_column(Integer)
    enemy_alive_at_plant: Mapped[int | None] = mapped_column(Integer)
    survivors: Mapped[int] = mapped_column(Integer)  # alive when the round ended
    flags: Mapped[str] = mapped_column(Text)  # JSON list, e.g. ["lost_opening", "team_flash"]


class PlayerRoundStat(Base):
    """One player in one played round (metrics.py).

    kills, died, assists, damage and headshot_kills count like the in-game
    scoreboard (also after the round ended), so they add up to it. Everything
    else (openings, trades, survived, clutches) only uses the live round.
    """

    __tablename__ = "player_round_stats"
    match_id: Mapped[str] = mapped_column(String, primary_key=True)
    round: Mapped[int] = mapped_column(Integer, primary_key=True)
    steamid: Mapped[str] = mapped_column(String, primary_key=True)
    team: Mapped[str | None] = mapped_column(String)
    side: Mapped[str | None] = mapped_column(String)
    kills: Mapped[int] = mapped_column(Integer)
    died: Mapped[bool] = mapped_column(Boolean)
    assists: Mapped[int] = mapped_column(Integer)
    damage: Mapped[int] = mapped_column(Integer)  # health removed from enemies; ADR = sum / rounds
    headshot_kills: Mapped[int] = mapped_column(Integer)
    opening_kill: Mapped[bool] = mapped_column(Boolean)
    opening_death: Mapped[bool] = mapped_column(Boolean)
    trade_kills: Mapped[int] = mapped_column(Integer)
    was_traded: Mapped[bool] = mapped_column(Boolean)
    survived: Mapped[bool] = mapped_column(Boolean)  # alive when the round ended
    kast: Mapped[bool] = mapped_column(Boolean)  # kill, assist, survived or traded
    multi_kill: Mapped[int] = mapped_column(Integer)  # number of kills if 2 or more, else 0
    clutch_n: Mapped[int] = mapped_column(Integer)  # N if this player was the last one in a 1vN, else 0
    clutch_won: Mapped[bool | None] = mapped_column(Boolean)
    flash_assists: Mapped[int] = mapped_column(Integer)
    enemies_blinded: Mapped[int] = mapped_column(Integer)
    teammates_blinded: Mapped[int] = mapped_column(Integer)
    utility_damage: Mapped[int] = mapped_column(Integer)


class RoundSummary(Base):
    """One played round as compact JSON, from our point of view (summaries.py)."""

    __tablename__ = "round_summaries"
    match_id: Mapped[str] = mapped_column(String, primary_key=True)
    round: Mapped[int] = mapped_column(Integer, primary_key=True)
    json: Mapped[str] = mapped_column(Text)


class MatchDigest(Base):
    """The whole match as one JSON document: what the LLM reads (summaries.py)."""

    __tablename__ = "match_digests"
    match_id: Mapped[str] = mapped_column(String, primary_key=True)
    json: Mapped[str] = mapped_column(Text)
    token_estimate: Mapped[int] = mapped_column(Integer)  # characters / 4


class RoundPlay(Base):
    """What one team did in one round: a play label and its features (plays.py)."""

    __tablename__ = "round_plays"
    match_id: Mapped[str] = mapped_column(String, primary_key=True)
    round: Mapped[int] = mapped_column(Integer, primary_key=True)
    team: Mapped[str] = mapped_column(String, primary_key=True)
    side: Mapped[str | None] = mapped_column(String)
    label: Mapped[str] = mapped_column(String)  # T: rush/execute/split/fake/default/slow/unknown; CT: stack/aggression/standard
    site: Mapped[str | None] = mapped_column(String)
    confidence: Mapped[float] = mapped_column(Float)  # 1.0 for a manual label
    features: Mapped[str] = mapped_column(Text)  # JSON: zones at 10/20/30/45 s, waves, site hit, entry, plant
    name: Mapped[str | None] = mapped_column(String)  # the team's own name for the play, set by hand
    label_source: Mapped[str] = mapped_column(String)  # "rule" or "manual" (manual always wins)


class LLMCall(Base):
    """Every call to a language model: who, what for, tokens and cost (llm.py)."""

    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[str] = mapped_column(String)
    provider: Mapped[str] = mapped_column(String)  # anthropic / manual / fake
    model: Mapped[str] = mapped_column(String)
    purpose: Mapped[str | None] = mapped_column(String)  # report / ask / trends ...
    match_id: Mapped[str | None] = mapped_column(String)  # or another key, such as a question id
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[float] = mapped_column(Float)
    ok: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)


class Report(Base):
    """A match report written by the LLM and checked by code (report.py)."""

    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, default="match")  # "match" or "trends" (Phase 5)
    match_id: Mapped[str] = mapped_column(String, index=True)  # for trends: "trends:<map>:<last>"
    created_at: Mapped[str] = mapped_column(String)
    provider: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[float] = mapped_column(Float)
    report_json: Mapped[str] = mapped_column(Text)
    report_md: Mapped[str] = mapped_column(Text)


class MapPlace(Base):
    """Grid of known area names per map (shared by all matches)."""

    __tablename__ = "map_places"
    map: Mapped[str] = mapped_column(String, primary_key=True)
    gx: Mapped[int] = mapped_column(Integer, primary_key=True)
    gy: Mapped[int] = mapped_column(Integer, primary_key=True)
    gz: Mapped[int] = mapped_column(Integer, primary_key=True)
    place: Mapped[str] = mapped_column(String, primary_key=True)
    n: Mapped[int] = mapped_column(Integer)


# tables that hold rows of a single match, in the order they are written
MATCH_MODELS = [Match, Player, Round, PlayerRound, Kill, Damage, Grenade, Blind, BombEvent, Position]
# tables computed from the ones above by metrics.py and summaries.py
# (deleted with the match too, and rebuilt after every ingest)
METRIC_MODELS = [TeamRound, PlayerRoundStat, RoundPlay, RoundSummary, MatchDigest]


# SQL views for trends and the question agent (Phase 5): only the owner's team.
# matches.file_time stands in for the match date (the demo has no date).
VIEWS = {
    "our_rounds": """
        SELECT tr.*, m.map, m.file_time
        FROM team_rounds tr JOIN matches m ON m.match_id = tr.match_id
        WHERE tr.is_us = 1""",
    "our_player_rounds": """
        SELECT s.*, p.name, m.map, m.file_time
        FROM player_round_stats s
        JOIN players p ON p.match_id = s.match_id AND p.steamid = s.steamid
        JOIN matches m ON m.match_id = s.match_id
        WHERE p.is_us = 1""",
    # every kill in played rounds; by_us / of_us say whether we killed or died
    "our_kills": """
        SELECT k.*, m.map, m.file_time,
               (k.attacker_side = CASE WHEN r.t_team = m.us_team THEN 'T' ELSE 'CT' END) AS by_us,
               (k.victim_side = CASE WHEN r.t_team = m.us_team THEN 'T' ELSE 'CT' END) AS of_us
        FROM kills k
        JOIN matches m ON m.match_id = k.match_id
        JOIN rounds r ON r.match_id = k.match_id AND r.round = k.round
        WHERE m.us_team IS NOT NULL AND r.played = 1""",
}


def get_engine(db_path: str | Path) -> Engine:
    """Open (and create if needed) the SQLite database.

    Raises SchemaMismatchError if the file was made by another version of the
    code. Every command calls this, so none of them works on an old database.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    existing = set(inspect(engine).get_table_names())
    if existing:
        found = None
        if "meta" in existing:
            with engine.connect() as conn:
                found = conn.execute(select(Meta.value).where(Meta.key == "schema_version")).scalar_one_or_none()
        if found != str(SCHEMA_VERSION):
            engine.dispose()
            if found is not None and found.isdigit() and int(found) > SCHEMA_VERSION:
                raise SchemaMismatchError(
                    f"The database was made by a newer version of this tool (schema {found}, "
                    f"this code knows {SCHEMA_VERSION}). Update the code, or delete {db_path.as_posix()} "
                    "and run `python -m cs2review.ingest` again."
                )
            raise SchemaMismatchError(
                f"The database was made by an older version. Delete {db_path.as_posix()} "
                "and run `python -m cs2review.ingest` again."
            )
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for name, sql in VIEWS.items():
            conn.exec_driver_sql(f"CREATE VIEW IF NOT EXISTS {name} AS {sql}")
        if not existing:
            conn.execute(Meta.__table__.insert().values(key="schema_version", value=str(SCHEMA_VERSION)))
    return engine


def has_match(engine: Engine, match_id: str) -> bool:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(Match).where(Match.match_id == match_id)).scalar_one() > 0


def list_matches(engine: Engine) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(select(Match).order_by(Match.ingested_at), conn)


def load_place_cells(engine: Engine, map_name: str) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(select(MapPlace).where(MapPlace.map == map_name), conn)


def delete_match(engine: Engine, match_id: str) -> None:
    with engine.begin() as conn:
        for model in MATCH_MODELS + METRIC_MODELS:
            conn.execute(delete(model).where(model.match_id == match_id))


def save_match(engine: Engine, tables: dict[str, pd.DataFrame]) -> None:
    """Write all tables of one match in a single transaction (replacing old rows)."""
    match_id = str(tables["matches"]["match_id"].iloc[0])
    with engine.begin() as conn:
        # old metrics would point at kills that no longer exist; metrics.py rebuilds them
        for model in MATCH_MODELS + METRIC_MODELS:
            conn.execute(delete(model).where(model.match_id == match_id))
        for model in MATCH_MODELS:
            frame = tables[model.__tablename__]
            if not frame.empty:
                frame.to_sql(model.__tablename__, conn, if_exists="append", index=False)

        # grow the per-map grid of area names
        new_cells = tables.get("map_places")
        if new_cells is not None and not new_cells.empty:
            map_name = str(new_cells["map"].iloc[0])
            old = pd.read_sql(select(MapPlace).where(MapPlace.map == map_name), conn)
            merged = places.merge_cells(old, new_cells)
            merged.insert(0, "map", map_name)
            conn.execute(delete(MapPlace).where(MapPlace.map == map_name))
            merged.to_sql(MapPlace.__tablename__, conn, if_exists="append", index=False)
