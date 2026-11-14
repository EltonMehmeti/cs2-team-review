"""Phase 5: trends across matches, for our team only.

    python -m cs2review.trends                        # every map, every match
    python -m cs2review.trends --map de_mirage --last 10
    python -m cs2review.trends --map de_mirage --report --provider manual   # LLM summary

Rule for honesty: every rate shows n (how many rounds it is based on). A rate
from fewer than MIN_N rounds is printed as "not enough data (n=5)", because
a 3-of-5 streak says almost nothing.

Words:
  5v4 up / 4v5 down  we won / lost the opening duel with five players each
  pistol conversion  after winning a pistol round, the share of the next two
                     rounds we also won (the money advantage should carry)
  post-plant         T side, bomb planted: did we win?
  retake             CT side, bomb planted: did we win it back?
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

import pandas as pd
from sqlalchemy import Engine, text

from .config import load_config
from .db import SchemaMismatchError, get_engine

MIN_N = 8  # print no rate from fewer rounds than this
SIDES = ("T", "CT")


def rate(won: int, n: int) -> dict:
    """{"won", "n", "pct"}; pct is empty when n is below MIN_N."""
    return {"won": int(won), "n": int(n), "pct": round(100 * won / n) if n >= MIN_N else None}


def rate_text(r: dict) -> str:
    if r["n"] == 0:
        return "no rounds"
    if r["pct"] is None:
        return f"not enough data (n={r['n']})"
    return f"{r['pct']}% ({r['won']}/{r['n']})"


def _read(engine: Engine, sql: str, **params) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


def pick_matches(engine: Engine, map_name: str | None = None, last: int | None = None) -> list[str]:
    """Our matches, newest last (by file time, the best date we have)."""
    sql = "SELECT match_id FROM matches WHERE us_team IS NOT NULL"
    params = {}
    if map_name:
        sql += " AND map = :map"
        params["map"] = map_name
    sql += " ORDER BY file_time, ingested_at, match_id"
    ids = list(_read(engine, sql, **params)["match_id"])
    return ids[-last:] if last else ids


def _bool(frame: pd.DataFrame, columns) -> pd.DataFrame:
    for c in columns:
        frame[c] = frame[c].fillna(0).astype(bool)
    return frame


def compute_trends(engine: Engine, map_name: str | None = None, last: int | None = None, cfg=None) -> dict:
    """All trend numbers as one dict (the trend digest). Rates follow the MIN_N rule.

    With cfg, each map also lists the playbook cards we are trying and how
    often we ran them since (Phase 9).
    """
    ids = pick_matches(engine, map_name, last)
    out: dict = {"filter": {"map": map_name or "all", "last": last, "matches": len(ids)}, "maps": {}}
    if not ids:
        return out
    marks = ",".join(f":m{i}" for i in range(len(ids)))
    params = {f"m{i}": m for i, m in enumerate(ids)}
    rounds = _bool(_read(engine, f"SELECT * FROM our_rounds WHERE match_id IN ({marks})", **params), ["won", "planted"])
    enemy = _read(engine, f"SELECT match_id, round, players FROM team_rounds WHERE is_us = 0 AND match_id IN ({marks})", **params)
    rounds = rounds.merge(enemy.rename(columns={"players": "enemy_players"}), on=["match_id", "round"], how="left")
    players = _bool(_read(engine, f"SELECT * FROM our_player_rounds WHERE match_id IN ({marks})", **params),
                    ["kast", "opening_kill", "opening_death", "was_traded", "survived"])
    kills = _bool(_read(engine, f"SELECT * FROM our_kills WHERE match_id IN ({marks})", **params),
                  ["is_opening", "was_traded", "after_round_end", "is_suicide", "is_teamkill", "by_us", "of_us"])
    plays = _read(engine, f"""
        SELECT rp.match_id, rp.round, rp.label, rp.site, rp.name FROM round_plays rp
        JOIN matches m ON m.match_id = rp.match_id AND rp.team = m.us_team
        WHERE rp.match_id IN ({marks})""", **params)

    for map_, mr in rounds.groupby("map", sort=True):
        block: dict = {"matches": int(mr["match_id"].nunique()), "sides": {}}
        for side in SIDES:
            sr = mr[mr["side"] == side].sort_values(["match_id", "round"])
            if sr.empty:
                continue
            s: dict = {"rounds": rate(sr["won"].sum(), len(sr))}
            s["by_buy"] = {b: rate(g["won"].sum(), len(g)) for b, g in sr.groupby(sr["buy_type"].fillna("unknown"), sort=True)}
            duel = sr[sr["opening"].notna()]
            s["opening_duels"] = rate((duel["opening"] == "won").sum(), len(duel))
            # where our opening deaths happen (the victim's area)
            side_kills = kills[(kills["map"] == map_) & kills["is_opening"] & kills["of_us"]]
            side_kills = side_kills.merge(sr[["match_id", "round"]], on=["match_id", "round"])
            s["opening_deaths_at"] = dict(Counter(side_kills["victim_place"].fillna("unknown")).most_common(5))
            traded, untraded = int(sr["traded_deaths"].sum()), int(sr["untraded_deaths"].sum())
            s["trades"] = rate(traded, traded + untraded)
            even = sr[(sr["players"] == 5) & (sr["enemy_players"] == 5)]
            s["after_5v4_up"] = rate(even.loc[even["opening"] == "won", "won"].sum(), (even["opening"] == "won").sum())
            s["after_4v5_down"] = rate(even.loc[even["opening"] == "lost", "won"].sum(), (even["opening"] == "lost").sum())
            # pistol conversion: the two rounds after a won pistol round, same side
            conv_won = conv_n = 0
            by_round = {(m, r): w for m, r, w in zip(mr["match_id"], mr["round"], mr["won"])}
            side_of = {(m, r): sd for m, r, sd in zip(mr["match_id"], mr["round"], mr["side"])}
            for m, r in zip(sr.loc[(sr["buy_type"] == "pistol") & sr["won"], "match_id"],
                            sr.loc[(sr["buy_type"] == "pistol") & sr["won"], "round"]):
                for nxt in (r + 1, r + 2):
                    if (m, nxt) in by_round and side_of[(m, nxt)] == side:
                        conv_n += 1
                        conv_won += int(by_round[(m, nxt)])
            s["pistol_conversion"] = rate(conv_won, conv_n)
            planted = sr[sr["planted"]]
            label = "postplant" if side == "T" else "retake"
            s[label] = {site: rate(g["won"].sum(), len(g)) for site, g in planted.groupby(planted["plant_site"].fillna("?"), sort=True)}
            flags = Counter(f for fl in sr["flags"] for f in json.loads(fl))
            s["flags"] = dict(sorted(flags.items()))
            # Phase 6: win rate per play (the IGL's name for it when one was set)
            sp = plays.merge(sr[["match_id", "round", "won"]], on=["match_id", "round"]) if not plays.empty else plays
            s["by_play"] = {}
            if not sp.empty:
                sp = sp.assign(play=[_play_name(l, st, n) for l, st, n in zip(sp["label"], sp["site"], sp["name"])])
                s["by_play"] = {p: rate(g["won"].sum(), len(g)) for p, g in sp.groupby("play", sort=True)}
            block["sides"][side] = s

        # per player on this map
        mp = players[players["map"] == map_]
        mk = kills[(kills["map"] == map_) & ~kills["after_round_end"] & ~kills["is_suicide"] & ~kills["is_teamkill"]]
        untraded = mk[mk["of_us"] & ~mk["was_traded"]].groupby("victim_steamid").size()
        table = []
        for steamid, g in mp.groupby("steamid"):
            n = len(g)
            table.append({
                "name": str(g["name"].iloc[0]), "rounds": n,
                "k": int(g["kills"].sum()), "d": int(g["died"].sum()), "a": int(g["assists"].sum()),
                "adr": round(float(g["damage"].sum()) / n, 1) if n else 0.0,
                "kast": rate(g["kast"].sum(), n),
                "opening_kills": int(g["opening_kill"].sum()), "opening_deaths": int(g["opening_death"].sum()),
                "trade_kills": int(g["trade_kills"].sum()), "untraded_deaths": int(untraded.get(steamid, 0)),
                "teammates_blinded": int(g["teammates_blinded"].sum()),
            })
        block["players"] = sorted(table, key=lambda p: (-p["rounds"], p["name"]))
        if cfg is not None:
            from .suggest import feedback
            block["trying"] = [{k: f[k] for k in ("id", "title", "status", "since", "runs")} for f in feedback(engine, cfg, map_)]
        out["maps"][map_] = block
    return out


def _play_name(label, site, name) -> str:
    if isinstance(name, str) and name:
        return name
    return f"{label} {site}" if isinstance(site, str) and site else str(label)


def format_trends(trends: dict) -> str:
    f = trends["filter"]
    lines = [f"Trends for our team: map {f['map']}, {f['matches']} match(es)"
             + (f" (last {f['last']})" if f["last"] else ""),
             f"Rates need at least {MIN_N} rounds; n is always shown.", ""]
    if not trends["maps"]:
        lines.append("No matches of your team yet. Check team_steam_ids in config.toml and run the ingest.")
        return "\n".join(lines)
    for map_, block in trends["maps"].items():
        lines.append(f"=== {map_} ({block['matches']} match(es)) ===")
        for side, s in block["sides"].items():
            lines.append(f"-- {side} side --")
            lines.append(f"  round win rate        {rate_text(s['rounds'])}")
            for buy, r in s["by_buy"].items():
                lines.append(f"    {buy:<8} buy        {rate_text(r)}")
            lines.append(f"  opening duels won     {rate_text(s['opening_duels'])}")
            if s["opening_deaths_at"]:
                where = ", ".join(f"{k} {v}" for k, v in s["opening_deaths_at"].items())
                lines.append(f"    our opening deaths: {where}")
            lines.append(f"  deaths traded         {rate_text(s['trades'])}")
            lines.append(f"  won after 5v4 up      {rate_text(s['after_5v4_up'])}")
            lines.append(f"  won after 4v5 down    {rate_text(s['after_4v5_down'])}")
            lines.append(f"  pistol conversion     {rate_text(s['pistol_conversion'])}")
            label = "post-plant" if side == "T" else "retake"
            sites = s["postplant" if side == "T" else "retake"]
            if sites:
                for site, r in sites.items():
                    lines.append(f"  {label + ' ' + site:<22}{rate_text(r)}")
            else:
                lines.append(f"  {label:<22}no plants")
            if s.get("by_play"):
                lines.append("  by play:")
                for play, r in sorted(s["by_play"].items(), key=lambda kv: -kv[1]["n"]):
                    lines.append(f"    {play[:20]:<20}{rate_text(r)}")
            if s["flags"]:
                lines.append("  flags: " + ", ".join(f"{k} {v}" for k, v in s["flags"].items()))
        lines.append("")
        lines.append(f"  {'player':<22}{'rnds':>5}{'K':>5}{'D':>5}{'A':>5}{'ADR':>7}  {'KAST':<22}{'oK':>4}{'oD':>4}{'trK':>5}{'untr':>6}")
        for p in block["players"]:
            lines.append(f"  {p['name'][:21]:<22}{p['rounds']:>5}{p['k']:>5}{p['d']:>5}{p['a']:>5}{p['adr']:>7}  "
                         f"{rate_text(p['kast']):<22}{p['opening_kills']:>4}{p['opening_deaths']:>4}"
                         f"{p['trade_kills']:>5}{p['untraded_deaths']:>6}")
        lines.append("  (oK/oD = opening kills/deaths, trK = trade kills, untr = deaths nobody traded)")
        if block.get("trying"):
            lines.append("")
            lines.append("  plays we are trying (from the playbook):")
            for f in block["trying"]:
                lines.append(f"    {f['title'][:40]:<40} {f['status']:<8} ran {f['runs']['n']}x since {f['since'] or '?'}, "
                             f"won {rate_text(f['runs'])}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Trends across matches for our team.")
    ap.add_argument("--map", help="only this map, e.g. de_mirage")
    ap.add_argument("--last", type=int, help="only the last N matches")
    ap.add_argument("--json", action="store_true", help="print the numbers as JSON (the trend digest)")
    ap.add_argument("--report", action="store_true", help="write a short trend report with the LLM")
    ap.add_argument("--provider", choices=["anthropic", "manual"], help="LLM provider for --report")
    ap.add_argument("--post", action="store_true", help="post the trend report to Discord")
    ap.add_argument("--config", default="config.toml")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = load_config(args.config)
    try:
        engine = get_engine(cfg.db_path)
    except SchemaMismatchError as exc:
        print(exc)
        return 1
    trends = compute_trends(engine, args.map, args.last, cfg=cfg)
    if args.json:
        print(json.dumps(trends, ensure_ascii=False, indent=2))
    else:
        print(format_trends(trends))
    if args.report:
        from .trend_report import run_trend_report
        return run_trend_report(engine, cfg, trends, provider=args.provider, post=args.post)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
