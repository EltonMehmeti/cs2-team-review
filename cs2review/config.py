"""Settings, read from config.toml (all of them optional)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BuyThresholds:
    """How a team's buy in a round is named (see metrics.buy_type).

    These defaults are a starting point, not a measured truth. Look at
    `python -m cs2review.metrics --buy-histogram` and tune them in config.toml.
    """

    # pistol round: every player started the round with this much money or less
    pistol_max_money: int = 1000
    # otherwise the team's gear value, scaled to five players, decides:
    eco_below: int = 5000  # below this: eco (saving money)
    full_from: int = 20000  # from this: full buy; in between: semi (a partial buy)


@dataclass
class Config:
    # steamID64 of each of your five players, as strings
    team_steam_ids: list[str] = field(default_factory=list)
    # where you drop downloaded demos (.dem, .dem.zst, .dem.gz, .dem.bz2)
    inbox_dir: Path = Path("inbox")
    # where unpacked .dem files are kept
    demos_dir: Path = Path("demos")
    # the SQLite database file
    db_path: Path = Path("data/cs2review.sqlite")
    # names for buy types ([buy_types] in config.toml)
    buy_types: BuyThresholds = field(default_factory=BuyThresholds)


def load_config(path: str | Path = "config.toml") -> Config:
    """Read config.toml if it exists; otherwise return the defaults."""
    cfg = Config()
    path = Path(path)
    if not path.exists():
        return cfg
    with path.open("rb") as f:
        raw = tomllib.load(f)
    cfg.team_steam_ids = [str(x).strip() for x in raw.get("team_steam_ids", [])]
    for key in ("inbox_dir", "demos_dir", "db_path"):
        if key in raw:
            setattr(cfg, key, Path(raw[key]))
    buy = raw.get("buy_types", {})
    for key in ("pistol_max_money", "eco_below", "full_from"):
        if key in buy:
            setattr(cfg.buy_types, key, int(buy[key]))
    return cfg
