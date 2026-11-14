"""Settings, read from config.toml (all of them optional)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


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
    return cfg
