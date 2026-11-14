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
class PlayRules:
    """[plays] in config.toml: thresholds of the play labels (PLAN.md, Phase 6)."""

    rush_before_s: float = 25.0  # rush: site hit before this
    execute_min_utility: int = 2  # execute: a wave with this many smokes + molotovs on a site zone ...
    execute_hit_within_s: float = 15.0  # ... then a site hit there within this many seconds
    wave_window_s: float = 8.0  # grenades landing in one zone within this many seconds form a wave
    wave_min_grenades: int = 2
    site_hit_min_players: int = 3  # a site hit: this many attackers inside a site
    entry_window_s: float = 10.0  # look this far back for where attackers came from
    default_at_s: float = 30.0  # default: at this time players in ...
    default_min_zones: int = 3  # ... this many zones, and
    default_no_hit_before_s: float = 40.0  # no site hit before this
    slow_no_hit_before_s: float = 60.0  # slow: no site hit before this
    stack_min: int = 4  # CT stack: this many CTs in one site zone at 20 s
    aggression_before_s: float = 25.0  # CT aggression: a CT in the T zone before this
    review_below: float = 0.6  # `plays review` shows labels with less confidence


@dataclass
class Config:
    # steamID64 of each of your five players, as strings
    team_steam_ids: list[str] = field(default_factory=list)
    # where you drop downloaded demos (.dem, .dem.zst, .dem.gz, .dem.bz2)
    inbox_dir: Path = Path("inbox")
    # where unpacked .dem files are kept
    demos_dir: Path = Path("demos")
    # pro demos you downloaded by hand (python -m cs2review.ingest --kind pro)
    pro_inbox_dir: Path = Path("pro_inbox")
    # statuses you give playbook cards (new/trying/adopted/dropped); kept outside the database
    playbook_status_path: Path = Path("data/playbook_status.json")
    # the SQLite database file
    db_path: Path = Path("data/cs2review.sqlite")
    # names for buy types ([buy_types] in config.toml)
    buy_types: BuyThresholds = field(default_factory=BuyThresholds)
    # the language model ([llm] in config.toml)
    llm: LLMSettings = field(default_factory=LLMSettings)
    # one zone file per map (Phase 6): maps/de_mirage.toml
    maps_dir: Path = Path("maps")
    # labels you set by hand in `plays review`; kept outside the database so
    # that rebuilding the database never loses them. Back this file up.
    manual_labels_path: Path = Path("data/manual_plays.json")
    # play detection thresholds ([plays] in config.toml)
    plays: PlayRules = field(default_factory=lambda: PlayRules())


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
    for key in ("maps_dir", "manual_labels_path", "pro_inbox_dir", "playbook_status_path"):
        if key in raw:
            setattr(cfg, key, Path(raw[key]))
    for key, value in raw.get("plays", {}).items():
        if hasattr(cfg.plays, key):
            setattr(cfg.plays, key, type(getattr(cfg.plays, key))(value))
    return cfg
