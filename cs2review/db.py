"""The SQLite database: table definitions and save/load helpers.

One row in `matches` per demo. Every other table has a match_id column, so
deleting or re-ingesting a match is a simple delete by match_id.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import Boolean, Engine, Float, Index, Integer, String, Text, create_engine, delete, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from . import places


class Base(DeclarativeBase):
    pass


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


def get_engine(db_path: str | Path) -> Engine:
    """Open (and create if needed) the SQLite database."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)
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
        for model in MATCH_MODELS:
            conn.execute(delete(model).where(model.match_id == match_id))


def save_match(engine: Engine, tables: dict[str, pd.DataFrame]) -> None:
    """Write all tables of one match in a single transaction (replacing old rows)."""
    match_id = str(tables["matches"]["match_id"].iloc[0])
    with engine.begin() as conn:
        for model in MATCH_MODELS:
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
