"""Print a scoreboard for a match and check it against the demo's own numbers.

    python -m cs2review.check             # the most recently ingested match
    python -m cs2review.check MATCH_ID    # one match
    python -m cs2review.check --all       # every match, short form
    python -m cs2review.check --list      # list match ids

Why this exists: the demo carries the game's own end-of-match scoreboard.
If the kills, deaths, assists and score we rebuilt from single events agree
with it, the round and event parsing can be trusted.
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd
from sqlalchemy import Engine, text

from .config import load_config
from .db import get_engine, list_matches


def _read(engine: Engine, sql: str, **params) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


def scoreboard(engine: Engine, match_id: str) -> pd.DataFrame:
    """One row per player: our numbers next to the demo's numbers."""
    players = _read(engine, "SELECT * FROM players WHERE match_id = :m", m=match_id)
    kills = _read(engine, "SELECT * FROM kills WHERE match_id = :m", m=match_id)
    damages = _read(engine, "SELECT * FROM damages WHERE match_id = :m", m=match_id)
    match = _read(engine, "SELECT * FROM matches WHERE match_id = :m", m=match_id).iloc[0]
    rounds_played = max(int(match["rounds_played"]), 1)

    # SQLite stores True/False as 1/0
    for column in ("is_suicide", "is_teamkill", "headshot"):
        kills[column] = kills[column].fillna(0).astype(bool)
    for column in ("is_self", "is_teamdamage"):
        damages[column] = damages[column].fillna(0).astype(bool)

    real_kills = kills[~kills["is_suicide"] & ~kills["is_teamkill"]]
    enemy_damage = damages[damages["attacker_steamid"].notna() & ~damages["is_self"] & ~damages["is_teamdamage"]]

    board = players[["steamid", "name", "team", "is_us", "demo_kills", "demo_deaths", "demo_assists", "demo_damage"]].copy()
    sid = board["steamid"]
    board["K"] = sid.map(real_kills.groupby("attacker_steamid").size()).fillna(0).astype(int)
    board["D"] = sid.map(kills.groupby("victim_steamid").size()).fillna(0).astype(int)
    board["A"] = sid.map(kills[kills["assister_steamid"].notna()].groupby("assister_steamid").size()).fillna(0).astype(int)
    board["teamkills"] = sid.map(kills[kills["is_teamkill"]].groupby("attacker_steamid").size()).fillna(0).astype(int)
    board["suicides"] = sid.map(kills[kills["is_suicide"]].groupby("victim_steamid").size()).fillna(0).astype(int)
    hs = sid.map(real_kills[real_kills["headshot"]].groupby("attacker_steamid").size()).fillna(0)
    board["HS%"] = (100 * hs / board["K"].where(board["K"] > 0)).round(0)
    board["damage"] = sid.map(enemy_damage.groupby("attacker_steamid")["dmg_health"].sum()).fillna(0).astype(int)
    board["ADR"] = (board["damage"] / rounds_played).round(1)
    return board.sort_values(["team", "K"], ascending=[True, False]).reset_index(drop=True)


def check_match(engine: Engine, match_id: str) -> tuple[bool, list[str]]:
    """Returns (everything matches, list of human-readable findings)."""
    match = _read(engine, "SELECT * FROM matches WHERE match_id = :m", m=match_id).iloc[0]
    board = scoreboard(engine, match_id)
    problems: list[str] = []
    notes: list[str] = []

    if board["demo_kills"].isna().all():
        notes.append("The demo has no scoreboard totals, so kills/deaths/assists could not be checked.")
    else:
        for row in board.itertuples():
            if pd.isna(row.demo_kills):
                continue
            # the in-game scoreboard takes a kill away for a teamkill or a suicide
            allowed = {row.K, row.K - row.teamkills, row.K - row.suicides, row.K - row.teamkills - row.suicides}
            if int(row.demo_kills) not in allowed:
                problems.append(f"{row.name}: kills {row.K}, demo says {int(row.demo_kills)}")
            if int(row.demo_deaths) != row.D:
                problems.append(f"{row.name}: deaths {row.D}, demo says {int(row.demo_deaths)}")
            if int(row.demo_assists) != row.A:
                problems.append(f"{row.name}: assists {row.A}, demo says {int(row.demo_assists)}")

    if pd.isna(match["demo_score_a"]) or pd.isna(match["demo_score_b"]):
        notes.append("The demo has no final team score, so the round count could not be checked.")
    elif (int(match["score_a"]), int(match["score_b"])) != (int(match["demo_score_a"]), int(match["demo_score_b"])):
        problems.append(
            f"score {int(match['score_a'])}:{int(match['score_b'])}, "
            f"demo says {int(match['demo_score_a'])}:{int(match['demo_score_b'])}"
        )

    # damage is allowed to be slightly off (small hits the demo does not log)
    known = board[board["demo_damage"].notna() & (board["demo_damage"] > 0)]
    if not known.empty:
        worst = float(((known["damage"] - known["demo_damage"]).abs() / known["demo_damage"]).max())
        if worst > 0.10:
            problems.append(f"damage differs from the demo by up to {worst:.0%}")
        elif worst > 0:
            notes.append(f"Damage is within {max(worst, 0.01):.0%} of the demo's totals (small differences are normal).")

    return (not problems), problems + notes


def format_report(engine: Engine, match_id: str, short: bool = False) -> str:
    match = _read(engine, "SELECT * FROM matches WHERE match_id = :m", m=match_id).iloc[0]
    ok, findings = check_match(engine, match_id)
    us = match["us_team"]
    head = (
        f"{match_id}  |  {match['map']}  |  team A {int(match['score_a'])} : {int(match['score_b'])} team B"
        f"  |  {int(match['rounds_played'])} rounds played"
        + (f"  |  you are team {us}" if isinstance(us, str) else "")
    )
    verdict = "CHECK OK: matches the demo's own scoreboard" if ok else "CHECK FAILED"
    if short:
        return f"{head}\n  {verdict}" + "".join(f"\n  - {f}" for f in findings if not ok)

    board = scoreboard(engine, match_id)
    lines = [head, "", f"{'':2}{'player':<24}{'team':<5}{'K':>4}{'D':>4}{'A':>4}{'ADR':>7}{'HS%':>6}   demo K/D/A"]
    for row in board.itertuples():
        mark = "*" if row.is_us else " "
        demo = "-" if pd.isna(row.demo_kills) else f"{int(row.demo_kills)}/{int(row.demo_deaths)}/{int(row.demo_assists)}"
        hs_value = board.at[row.Index, "HS%"]  # "HS%" is not a valid attribute name
        hs = "-" if pd.isna(hs_value) else f"{int(hs_value)}"
        lines.append(
            f"{mark:2}{str(row.name)[:23]:<24}{row.team:<5}{row.K:>4}{row.D:>4}{row.A:>4}{row.ADR:>7}{hs:>6}   {demo}"
        )
    if board["is_us"].any():
        lines.append("  * = your players")

    rounds = _read(engine, "SELECT * FROM rounds WHERE match_id = :m ORDER BY round", m=match_id)
    lines += ["", f"{'rnd':>3}  {'T':<2}{'CT':<3} {'won by':<8}{'how':<15}{'length':>7}  plant"]
    for r in rounds.itertuples():
        length = "-" if pd.isna(r.duration_s) else f"{r.duration_s:.0f}s"
        plant = f"{r.plant_site or '?'} at {r.plant_time_s:.0f}s" if r.bomb_planted and pd.notna(r.plant_time_s) else ""
        winner = f"{r.winner_team} ({r.winner_side})"
        lines.append(f"{r.round:>3}  {r.t_team:<2}{r.ct_team:<3} {winner:<8}{str(r.reason):<15}{length:>7}  {plant}")

    lines += ["", verdict] + [f"  - {f}" for f in findings]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Scoreboard and self-check for ingested matches.")
    ap.add_argument("match_id", nargs="?", help="match to show (default: the latest one)")
    ap.add_argument("--all", action="store_true", help="check every match, short form")
    ap.add_argument("--list", action="store_true", help="list match ids")
    ap.add_argument("--config", default="config.toml")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):  # player names can contain any character
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    engine = get_engine(load_config(args.config).db_path)
    matches = list_matches(engine)
    if matches.empty:
        print("No matches in the database yet. Run:  python -m cs2review.ingest")
        return 1

    if args.list:
        for m in matches.itertuples():
            print(f"{m.match_id}  {m.map}  A {m.score_a}:{m.score_b} B  ({m.source})")
        return 0

    if args.all:
        all_ok = True
        for match_id in matches["match_id"]:
            ok, _ = check_match(engine, match_id)
            all_ok &= ok
            print(format_report(engine, match_id, short=True))
        return 0 if all_ok else 2

    match_id = args.match_id or matches["match_id"].iloc[-1]
    if match_id not in set(matches["match_id"]):
        print(f"Unknown match id: {match_id}. Use --list to see what is in the database.")
        return 1
    print(format_report(engine, match_id))
    ok, _ = check_match(engine, match_id)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
