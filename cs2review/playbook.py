"""Phase 7: the playbook. Plays that worked for opponents and pros, as cards.

    python -m cs2review.ingest --kind pro          # pro demos you downloaded by hand into pro_inbox/
    python -m cs2review.playbook build             # find plays and write the cards (ingest does not)
    python -m cs2review.playbook list --map de_mirage
    python -m cs2review.playbook show pb_1a2b3c4d  # steps, lineups and demo examples of one card
    python -m cs2review.playbook status pb_1a2b3c4d trying
    python -m cs2review.playbook beat-us           # teams that beat us, per map

Where cards come from: T-side rounds labelled execute, split or fake (Phase
6), played by our opponents in our matches or by either team in pro demos.
Rounds are grouped by a signature: map, site, and the landing areas of the
smokes and molotovs in the play's utility wave. One group is one play. A
group becomes a card when it was won at least once; "seen" and "won" count
every round of the group.

Lineups: `setpos x y z; setang pitch yaw 0` from where the grenade was
thrown. They work only on a private server with sv_cheats 1, and they are
NOT verified: the throw event may fire slightly before the grenade leaves
the hand. Every card starts with lineup_verified = false; the owner tests
three lineups in a practice server (PLAN.md section 9).

Difficulty: per smoke or molotov 1 point, or 2 when it is a jump or running
throw; plus 1 if two of the grenades must land within 2 seconds of each
other. 3 or less is easy, 7 or more is hard.

The status you give a card (new, suggested, trying, adopted, dropped) is kept
in data/playbook_status.json, so rebuilding never loses it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, delete, text

from .config import Config, load_config
from .db import Playbook, SchemaMismatchError, get_engine
from .plays import play_wave
from .report import rewatch_tick
from .zones import load_zones

CANDIDATE_LABELS = ("execute", "split", "fake")
STATUSES = ("new", "suggested", "trying", "adopted", "dropped")
RUNNING_SPEED = 50  # throw_speed above this is a running throw
SYNC_WINDOW_S = 2.0  # two grenades landing this close together must be timed
MAX_EXAMPLES = 3


def throw_kind(in_air, speed) -> str:
    if bool(in_air):
        return "jump"
    if pd.notna(speed) and float(speed) > RUNNING_SPEED:
        return "running"
    return "standing"


def lineup_command(x, y, z, pitch, yaw) -> str | None:
    if any(pd.isna(v) for v in (x, y, z, pitch, yaw)):
        return None
    return f"setpos {x:.1f} {y:.1f} {z:.1f}; setang {pitch:.2f} {yaw:.2f} 0"


def difficulty(utility_steps: list[dict], landing_times: list[float]) -> int:
    points = sum(2 if s["throw"] in ("jump", "running") else 1 for s in utility_steps)
    times = sorted(t for t in landing_times if pd.notna(t))
    if any(b - a <= SYNC_WINDOW_S for a, b in zip(times, times[1:])):
        points += 1
    return points


def signature_of(map_name: str, site, areas: list[str]) -> str:
    return f"{map_name}|T|{site or '?'}|{','.join(areas)}"


def card_id(signature: str) -> str:
    return "pb_" + hashlib.sha1(signature.encode("utf-8")).hexdigest()[:8]


def load_statuses(path) -> dict[str, str]:
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_statuses(path, statuses: dict[str, str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(statuses, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# finding candidate rounds
# --------------------------------------------------------------------------

CANDIDATES_SQL = """
    SELECT rp.match_id, rp.round, rp.team, rp.label, rp.site, rp.features, tr.won,
           m.map, m.source_kind, m.us_team, m.file_time, m.file_name, m.tick_rate,
           m.team_a_name, m.team_b_name, r.freeze_end_tick
    FROM round_plays rp
    JOIN team_rounds tr ON tr.match_id = rp.match_id AND tr.round = rp.round AND tr.team = rp.team
    JOIN matches m ON m.match_id = rp.match_id
    JOIN rounds r ON r.match_id = rp.match_id AND r.round = rp.round
    WHERE rp.side = 'T' AND rp.label IN ('execute', 'split', 'fake')
      AND (m.source_kind = 'pro' OR (m.us_team IS NOT NULL AND rp.team != m.us_team))
    ORDER BY m.file_time, rp.match_id, rp.round
"""


def candidate_rounds(engine: Engine, cfg: Config) -> pd.DataFrame:
    """Every candidate round with its signature and the grenades of its wave."""
    with engine.connect() as conn:
        rows = pd.read_sql(text(CANDIDATES_SQL), conn)
        grenades = pd.read_sql(text(
            "SELECT * FROM grenades WHERE thrower_side = 'T' AND type IN ('smoke', 'molotov')"), conn)
    if rows.empty:
        return rows
    zones_by_map = {m: load_zones(m, cfg.maps_dir) for m in rows["map"].unique()}
    rows["won"] = rows["won"].fillna(0).astype(bool)
    signatures, waves, areas_col = [], [], []
    for row in rows.itertuples():
        zones = zones_by_map.get(row.map)
        f = json.loads(row.features)
        wave = play_wave(f, row.label, row.site, zones, cfg.plays) if zones else None
        areas: list[str] = []
        if wave:
            g = grenades[(grenades["match_id"] == row.match_id) & (grenades["round"] == row.round)
                         & grenades["det_time_s"].between(wave["t"], wave["end"] + 1)]
            areas = sorted({str(p) for p in g["land_place"] if p and zones.zone(p) == wave["zone"]})
        signatures.append(signature_of(row.map, row.site, areas))
        waves.append(wave)
        areas_col.append(areas)
    rows["signature"], rows["wave"], rows["areas"] = signatures, waves, areas_col
    return rows


# --------------------------------------------------------------------------
# one card
# --------------------------------------------------------------------------

def build_card(engine: Engine, cfg: Config, group: pd.DataFrame) -> dict:
    """The card of one play, from its most recent won round."""
    won = group[group["won"]]
    example = won.iloc[-1]
    label = Counter(group["label"]).most_common(1)[0][0]
    areas = example["areas"]
    title = f"{example['site'] or '?'} {label}"
    title += f": utility on {', '.join(areas)}" if areas else " (no utility wave)"

    with engine.connect() as conn:
        g = pd.read_sql(text("SELECT * FROM grenades WHERE match_id = :m AND round = :r AND thrower_side = 'T' "
                             "ORDER BY throw_tick"), conn, params={"m": example["match_id"], "r": int(example["round"])})
        pos = pd.read_sql(text("SELECT time_s, steamid, place FROM positions WHERE match_id = :m AND round = :r AND side = 'T'"),
                          conn, params={"m": example["match_id"], "r": int(example["round"])})
        plant = pd.read_sql(text("SELECT steamid, time_s FROM bomb_events WHERE match_id = :m AND round = :r AND event = 'plant'"),
                            conn, params={"m": example["match_id"], "r": int(example["round"])})

    roles: dict[str, str] = {}

    def role(steamid) -> str:
        if steamid not in roles:
            roles[steamid] = f"P{len(roles) + 1}"
        return roles[steamid]

    steps: list[dict] = []
    utility_steps: list[dict] = []
    landing: list[float] = []
    wave = example["wave"]
    zones = load_zones(example["map"], cfg.maps_dir)
    if wave:
        in_wave = g[g["type"].isin(["smoke", "molotov"]) & g["det_time_s"].between(wave["t"], wave["end"] + 1)]
        in_wave = in_wave[[zones.zone(p) == wave["zone"] for p in in_wave["land_place"]]]
        for gr in in_wave.itertuples():
            step = {"role": role(gr.thrower_steamid), "t": int(gr.throw_time_s) if pd.notna(gr.throw_time_s) else int(gr.det_time_s),
                    "do": gr.type, "from": gr.throw_place, "to": gr.land_place,
                    "lineup": lineup_command(gr.throw_x, gr.throw_y, gr.throw_z, gr.throw_pitch, gr.throw_yaw),
                    "throw": throw_kind(gr.throw_in_air, gr.throw_speed)}
            steps.append({k: v for k, v in step.items() if v is not None})
            utility_steps.append(step)
            landing.append(gr.det_time_s)

    f = json.loads(example["features"])
    hit = f.get("site_hit")
    if hit and zones:
        at_hit = pos[pos["time_s"] == hit["t"]]
        for steamid in sorted(s for s, p in zip(at_hit["steamid"], at_hit["place"]) if zones.site(p) == hit["site"]):
            before = pos[(pos["steamid"] == steamid) & (pos["time_s"] < hit["t"])
                         & (pos["time_s"] >= hit["t"] - cfg.plays.entry_window_s)].sort_values("time_s", ascending=False)
            via = next((str(p) for p in before["place"] if zones.site(p) != hit["site"]), None)
            step = {"role": role(steamid), "t": int(hit["t"]), "do": "enter"}
            if via:
                step["via"] = via
            steps.append(step)
    if not plant.empty:
        steps.append({"role": role(plant.iloc[0]["steamid"]), "t": int(plant.iloc[0]["time_s"]), "do": "plant",
                      "site": example["site"]})
    steps.sort(key=lambda s: (s["t"], s["role"]))

    examples = []
    for ex in won.iloc[::-1].head(MAX_EXAMPLES).itertuples():
        start_t = ex.wave["t"] if ex.wave else (json.loads(ex.features).get("site_hit") or {}).get("t", 0)
        tick = rewatch_tick(int(ex.freeze_end_tick), int(start_t), int(ex.tick_rate)) if pd.notna(ex.freeze_end_tick) else None
        team_name = ex.team_a_name if ex.team == "A" else ex.team_b_name
        item = {"match_id": ex.match_id, "round": int(ex.round), "source": ex.source_kind,
                "team": team_name if isinstance(team_name, str) and team_name else f"team {ex.team}"}
        if tick is not None:
            item["tick"] = tick
            item["command"] = f"playdemo {ex.file_name}; demo_gototick {tick}"
        examples.append(item)

    times = [t for t in group["file_time"] if isinstance(t, str) and t]
    first, last = (min(times), max(times)) if times else (None, None)
    window = (datetime.fromisoformat(last) - datetime.fromisoformat(first)).days if first else None
    return {
        "title": title, "label": label, "steps": steps, "examples": examples,
        "evidence": {"seen": int(len(group)), "won": int(group["won"].sum()), "window_days": window},
        "lineup_verified": False,
        "_difficulty": difficulty(utility_steps, landing), "_first": first, "_last": last,
    }


def build_playbook(engine: Engine, cfg: Config) -> pd.DataFrame:
    """Rebuild every card from the stored rounds. Statuses come from the status file."""
    rows = candidate_rounds(engine, cfg)
    statuses = load_statuses(cfg.playbook_status_path)
    cards = []
    if not rows.empty:
        for signature, group in rows.groupby("signature", sort=True):
            if not group["won"].any():
                continue
            card = build_card(engine, cfg, group)
            first = group.iloc[0]
            zones = load_zones(first["map"], cfg.maps_dir)
            last_update = zones.last_map_update if zones else None
            pid = card_id(signature)
            first_seen, last_seen, points = card.pop("_first"), card.pop("_last"), card.pop("_difficulty")
            cards.append({
                "id": pid, "signature": signature, "map": first["map"], "side": "T", "site": first["site"],
                "title": card["title"], "source_kind": "pro" if (group["source_kind"] == "pro").any() else "opponent",
                "seen": card["evidence"]["seen"], "won": card["evidence"]["won"],
                "first_seen": first_seen, "last_seen": last_seen, "difficulty": points,
                "status": statuses.get(pid, "new"),
                # seen only before Valve last changed the map: may not work any more
                "stale": bool(last_update and last_seen and last_seen[:10] < last_update),
                "needs_review": False, "card": json.dumps(card, ensure_ascii=False),
            })
    table = pd.DataFrame(cards, columns=[c.name for c in Playbook.__table__.columns])
    with engine.begin() as conn:
        conn.execute(delete(Playbook).where(Playbook.source_kind.in_(["opponent", "pro"])))
        if not table.empty:
            table.to_sql(Playbook.__tablename__, conn, if_exists="append", index=False)
    return table


def set_status(engine: Engine, cfg: Config, card: str, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"Unknown status '{status}'. Use one of: {', '.join(STATUSES)}.")
    with engine.begin() as conn:
        n = conn.execute(text("UPDATE playbook SET status = :s WHERE id = :i"), {"s": status, "i": card}).rowcount
    if not n:
        raise ValueError(f"Unknown card id '{card}'. See python -m cs2review.playbook list.")
    statuses = load_statuses(cfg.playbook_status_path)
    statuses[card] = status
    save_statuses(cfg.playbook_status_path, statuses)


def teams_that_beat_us(engine: Engine) -> pd.DataFrame:
    sql = """
        SELECT m.map, m.file_time, m.match_id,
               CASE WHEN m.us_team = 'A' THEN m.team_b_name ELSE m.team_a_name END AS opponent,
               CASE WHEN m.us_team = 'A' THEN m.score_a ELSE m.score_b END AS us,
               CASE WHEN m.us_team = 'A' THEN m.score_b ELSE m.score_a END AS them,
               (SELECT COUNT(*) FROM round_plays rp JOIN team_rounds tr
                  ON tr.match_id = rp.match_id AND tr.round = rp.round AND tr.team = rp.team
                WHERE rp.match_id = m.match_id AND rp.team != m.us_team AND rp.side = 'T'
                  AND rp.label IN ('execute', 'split', 'fake') AND tr.won = 1) AS their_winning_plays
        FROM matches m
        WHERE m.source_kind = 'own' AND m.us_team IS NOT NULL
          AND (CASE WHEN m.us_team = 'A' THEN m.score_a < m.score_b ELSE m.score_b < m.score_a END)
        ORDER BY m.map, m.file_time
    """
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn)


def format_card(row: dict) -> str:
    card = json.loads(row["card"])
    if row["source_kind"] == "web":  # found by the web scout: plain-text steps and a link
        src = card.get("source", {})
        lines = [f"{row['id']}  {row['map']}  {row['side']} {row['site'] or '-'}  {card['title']}",
                 f"  from the web, status {row['status']}" + ("  NEEDS REVIEW" if row["needs_review"] else "")
                 + ("  STALE" if row["stale"] else ""),
                 f"  source: {src.get('url')}" + (f" (published {src['published']})" if src.get("published") else "")
                 + (f" at {src['video_timestamp']}" if src.get("video_timestamp") else ""), "  steps:"]
        lines += [f"    {i}. {s['do']}" for i, s in enumerate(card["steps"], 1)]
        lines.append("  no lineup commands (web cards have none until a matching pro round is found)")
        return "\n".join(lines)
    ev = card["evidence"]
    level = "easy" if row["difficulty"] <= 3 else "hard" if row["difficulty"] >= 7 else "medium"
    lines = [f"{row['id']}  {row['map']}  {card['title']}",
             f"  seen {ev['seen']}, won {ev['won']} ({row['source_kind']}), difficulty {row['difficulty']} ({level}), "
             f"status {row['status']}" + ("  STALE: older than the last map update" if row["stale"] else ""),
             "  steps (t = seconds after the round went live):"]
    for s in card["steps"]:
        what = s["do"]
        if s["do"] in ("smoke", "molotov"):
            what += f" from {s.get('from', '?')} to {s.get('to', '?')} ({s.get('throw', '?')} throw)"
        elif s["do"] == "enter":
            what += f" the site via {s.get('via', '?')}"
        elif s["do"] == "plant":
            what += f" at {s.get('site') or '?'}"
        lines.append(f"    {s['t']:>3}s  {s['role']}: {what}")
        if s.get("lineup"):
            lines.append(f"          lineup: {s['lineup']}")
    lines.append("  examples to watch:")
    for ex in card["examples"]:
        lines.append(f"    {ex['team']} in {ex['match_id']} round {ex['round']}: {ex.get('command', '(no tick)')}")
    if not card["lineup_verified"]:
        lines.append("  lineups are not verified yet (setpos/setang need sv_cheats 1 on a private server)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The playbook: plays from opponents and pro demos.")
    ap.add_argument("--config", default="config.toml")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="find plays and write the cards")
    lp = sub.add_parser("list", help="list the cards")
    lp.add_argument("--map")
    lp.add_argument("--status", choices=STATUSES)
    sp = sub.add_parser("show", help="one card with its steps, lineups and examples")
    sp.add_argument("id")
    st = sub.add_parser("status", help="set a card's status")
    st.add_argument("id")
    st.add_argument("status", choices=STATUSES)
    sub.add_parser("beat-us", help="teams that beat us, per map")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = load_config(args.config)
    try:
        engine = get_engine(cfg.db_path)
    except SchemaMismatchError as exc:
        print(exc)
        return 1

    if args.command == "build":
        table = build_playbook(engine, cfg)
        print(f"{len(table)} card(s) in the playbook.")
        for row in table.itertuples():
            print(f"  {row.id}  {row.map:<12} {row.title}  (seen {row.seen}, won {row.won}, {row.source_kind})")
        return 0
    if args.command == "list":
        sql = "SELECT * FROM playbook WHERE (:map IS NULL OR map = :map) AND (:st IS NULL OR status = :st) ORDER BY map, won DESC, seen DESC"
        with engine.connect() as conn:
            table = pd.read_sql(text(sql), conn, params={"map": args.map, "st": args.status})
        if table.empty:
            print("No cards. Ingest pro demos (--kind pro) or matches, then run: python -m cs2review.playbook build")
            return 1
        for row in table.itertuples():
            print(f"{row.id}  {row.map:<12} {row.title:<50} seen {row.seen:>2}, won {row.won:>2}  "
                  f"diff {row.difficulty}  {row.status}{'  stale' if row.stale else ''}")
        return 0
    if args.command == "show":
        with engine.connect() as conn:
            table = pd.read_sql(text("SELECT * FROM playbook WHERE id = :i"), conn, params={"i": args.id})
        if table.empty:
            print(f"Unknown card id {args.id}.")
            return 1
        print(format_card(table.iloc[0].to_dict()))
        return 0
    if args.command == "status":
        try:
            set_status(engine, cfg, args.id, args.status)
        except ValueError as exc:
            print(exc)
            return 1
        print(f"{args.id} is now '{args.status}'.")
        return 0
    if args.command == "beat-us":
        table = teams_that_beat_us(engine)
        if table.empty:
            print("No lost matches of ours in the database.")
            return 0
        for row in table.itertuples():
            name = row.opponent if isinstance(row.opponent, str) and row.opponent else "(no team name)"
            print(f"{row.map:<12} {str(row.file_time)[:10]}  lost {row.us}-{row.them} to {name:<20} "
                  f"their winning executes/splits/fakes: {row.their_winning_plays}  ({row.match_id})")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
