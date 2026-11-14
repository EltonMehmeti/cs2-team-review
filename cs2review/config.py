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
class LLMSettings:
    """[llm] in config.toml. API keys are never here: they come from environment variables."""

    provider: str = "anthropic"  # "anthropic" or "manual" (paste the prompt into any chat app)
    model: str = "claude-haiku-5-5"  # the cheapest capable Claude model on 2026-11-14
    max_cost_usd_per_run: float = 0.25  # hard stop for one command
    price_in_per_mtok: float = 0.10  # USD per million input tokens, only for cost logging
    price_out_per_mtok: float = 0.50  # USD per million output tokens
    effort: str | None = None  # optional: low / medium / high (empty = the model's default)
    out_dir: Path = Path("out")  # manual mode writes prompts here and reads answers from here


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
    # the language model ([llm] in config.toml)
    llm: LLMSettings = field(default_factory=LLMSettings)


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
    llm = raw.get("llm", {})
    for key in ("provider", "model", "effort"):
        if key in llm:
            setattr(cfg.llm, key, str(llm[key]) or None)
    for key in ("max_cost_usd_per_run", "price_in_per_mtok", "price_out_per_mtok"):
        if key in llm:
            setattr(cfg.llm, key, float(llm[key]))
    if "out_dir" in llm:
        cfg.llm.out_dir = Path(llm["out_dir"])
    return cfg
