"""Phase 6: play detection. Label what each team did in each round.

    python -m cs2review.plays build                    # (re)label every match (ingest does it too)
    python -m cs2review.plays list --map de_mirage     # how often each play ran and how it went
    python -m cs2review.plays review --map de_mirage   # correct unclear labels by hand (for the IGL)
    python -m cs2review.plays coverage --map de_mirage # share of our T rounds with a label
    python -m cs2review.plays show MATCH_ID ROUND      # the features behind one round's labels

This is the least certain part of the project, so it is rule-based,
measurable and correctable. It works with zones from maps/<map>.toml.

Features per team per round (saved as JSON in round_plays.features):
  zones_at       living players per zone at 10, 20, 30 and 45 seconds
  first_contact  first damage between the teams (seconds)
  waves          utility waves: 2+ grenades landing in one zone within 8 s
  site_hit       T side: first moment 3+ attackers stand inside a bomb site
  entry_zones    zones those attackers came from in the 10 s before the hit
  plant          site and time of the plant

T-side labels (first rule that matches, thresholds in [plays] of config.toml):
  rush      site hit before 25 s
  execute   a wave of 2+ smokes/molotovs on a site's zone, then a site hit there within 15 s
  split     site hit with attackers arriving from 2 or more zones
  fake      utility wave on one site, then the hit or plant on the other
  default   at 30 s players in 3+ zones and no site hit before 40 s
  slow      no site hit before 60 s
  unknown   nothing matched
CT-side labels: stack (4+ CTs in one site's zone at 20 s), aggression (a CT
in the T zone before 25 s), otherwise standard. CT features also hold the
setup at 20 s ("A2-B2-Mid1") and the rotation time after the T site hit.

Manual labels (from `plays review`) always win over the rules and are never
overwritten. They live in data/manual_plays.json, outside the database, so
rebuilding the database keeps them. Back that file up.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

import pandas as pd
from sqlalchemy import Engine, delete, text

from .config import Config, PlayRules, load_config
from .db import RoundPlay, SchemaMismatchError, get_engine
from .zones import UNKNOWN, MapZones, ensure_zones

ZONE_TIMES = (10, 20, 30, 45)
T_LABELS = ["rush", "execute", "split", "fake", "default", "slow", "unknown"]
CT_LABELS = ["stack", "aggression", "standard"]
# how sure each rule is; a guess, so `plays review` can show the unsure ones first
CONFIDENCE = {"rush": 0.8, "execute": 0.8, "split": 0.7, "fake": 0.5, "default": 0.5, "slow": 0.5, "unknown": 0.0,
              "stack": 0.7, "aggression": 0.7, "standard": 0.6}
GRENADE_KEY = {"smoke": "smokes", "molotov": "molotovs", "flash": "flashes", "he": "hes"}


# --------------------------------------------------------------------------
# features (pure functions on small DataFrames)
# --------------------------------------------------------------------------

def zone_counts(pos: pd.DataFrame, zones: MapZones, second: int) -> dict[str, int]:
    """Living players per zone at one second. pos: time_s, place (one side only)."""
    at = pos[pos["time_s"] == second]
    counts: dict[str, int] = {}
    for place in at["place"]:
        z = zones.zone(place)
        counts[z] = counts.get(z, 0) + 1
    return dict(sorted(counts.items()))


def utility_waves(grenades: pd.DataFrame, zones: MapZones, rules: PlayRules) -> list[dict]:
    """Grenades landing in the same zone within wave_window_s of the first one."""
    g = grenades[grenades["type"].isin(GRENADE_KEY) & grenades["det_time_s"].notna()].copy()
    if g.empty:
        return []
    g["zone"] = [zones.zone(p) for p in g["land_place"]]
    waves = []
    for zone, grp in g.sort_values("det_time_s").groupby("zone", sort=True):
        times, types = list(grp["det_time_s"]), list(grp["type"])
        i = 0
        while i < len(times):
            j = i
            while j < len(times) and times[j] <= times[i] + rules.wave_window_s:
                j += 1
            if j - i >= rules.wave_min_grenades:
                wave = {"zone": zone, "t": int(times[i]), "end": int(times[j - 1])}
                for kind, key in GRENADE_KEY.items():
                    wave[key] = sum(1 for k in types[i:j] if k == kind)
                waves.append(wave)
            i = j
    return sorted(waves, key=lambda w: (w["t"], w["zone"]))


def find_site_hit(pos: pd.DataFrame, zones: MapZones, rules: PlayRules) -> dict | None:
    """First second with site_hit_min_players attackers inside one bomb site."""
    for second, at in pos.sort_values("time_s").groupby("time_s", sort=True):
        per_site: dict[str, list[str]] = {}
        for steamid, place in zip(at["steamid"], at["place"]):
            site = zones.site(place)
            if site:
                per_site.setdefault(site, []).append(steamid)
        best = sorted(per_site.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        if best and len(best[0][1]) >= rules.site_hit_min_players:
            return {"site": best[0][0], "t": int(second), "players": sorted(best[0][1])}
    return None


def entry_routes(pos: pd.DataFrame, hit: dict, zones: MapZones, rules: PlayRules) -> tuple[list[str], list[str]]:
    """Where the attackers in the site were just before they entered it (areas, zones)."""
    areas, zone_set = set(), set()
    for steamid in hit["players"]:
        before = pos[(pos["steamid"] == steamid) & (pos["time_s"] < hit["t"])
                     & (pos["time_s"] >= hit["t"] - rules.entry_window_s)].sort_values("time_s", ascending=False)
        for place in before["place"]:
            if zones.site(place) != hit["site"]:
                areas.add(str(place))
                zone_set.add(zones.zone(place))
                break
    return sorted(areas), sorted(z for z in zone_set if z != UNKNOWN)


def setup_string(counts: dict[str, int]) -> str:
    """{"A": 2, "B": 2, "Mid": 1} -> "A2-B2-Mid1"."""
    return "-".join(f"{zone}{n}" for zone, n in sorted(counts.items()))


def round_features(side: str, pos: pd.DataFrame, grenades: pd.DataFrame, plant: dict | None,
                   first_contact: float | None, length_s: float, zones: MapZones, rules: PlayRules,
                   t_hit: dict | None = None) -> dict:
    """The features of one team in one round. pos and grenades hold this team's rows only.

    For the CT team pass t_hit, the T team's site hit, to measure the rotation.
    """
    f: dict = {
        "zones_at": {str(s): zone_counts(pos, zones, s) for s in ZONE_TIMES if s <= length_s},
        "first_contact_s": None if first_contact is None else round(float(first_contact), 1),
        "waves": utility_waves(grenades, zones, rules),
        "length_s": int(length_s),
    }
    if side == "T":
        hit = find_site_hit(pos, zones, rules)
        f["site_hit"] = None if hit is None else {"site": hit["site"], "t": hit["t"]}
        f["entry_areas"], f["entry_zones"] = entry_routes(pos, hit, zones, rules) if hit else ([], [])
        f["plant"] = plant
    else:
        f["setup_20s"] = setup_string(f["zones_at"].get("20", {})) if "20" in f["zones_at"] else None
        early = pos[(pos["time_s"] < rules.aggression_before_s)]
        in_t = early[[zones.zone(p) == "T" for p in early["place"]]]
        f["aggression_s"] = int(in_t["time_s"].min()) if not in_t.empty else None
        f["rotation_s"] = None
        if t_hit:
            site_zone = zones.site_zone(t_hit["site"])
            at_hit = zone_counts(pos, zones, t_hit["t"]).get(site_zone, 0)
            for second in sorted(pos.loc[pos["time_s"] > t_hit["t"], "time_s"].unique()):
                if zone_counts(pos, zones, int(second)).get(site_zone, 0) > at_hit:
                    f["rotation_s"] = int(second) - int(t_hit["t"])
                    break
    return f


# --------------------------------------------------------------------------
# labels
# --------------------------------------------------------------------------

def label_t(f: dict, zones: MapZones, rules: PlayRules) -> tuple[str, str | None]:
    """(label, site) for a T-side round, the first rule that matches."""
    if not zones.sites:
        return "unknown", None
    hit, plant, length = f.get("site_hit"), f.get("plant"), f.get("length_s", 0)
    if hit and hit["t"] < rules.rush_before_s:
        return "rush", hit["site"]
    zone_to_site = {zones.site_zone(s): s for s in zones.sites}
    heavy = [w for w in f["waves"] if w["smokes"] + w["molotovs"] >= rules.execute_min_utility]
    if hit:
        for w in heavy:
            if zone_to_site.get(w["zone"]) == hit["site"] and w["t"] <= hit["t"] <= w["end"] + rules.execute_hit_within_s:
                return "execute", hit["site"]
        if len(f.get("entry_zones", [])) >= 2:
            return "split", hit["site"]
    target = hit or plant
    if target:
        for w in heavy:
            other = zone_to_site.get(w["zone"])
            if other and other != target["site"] and w["t"] <= target["t"]:
                return "fake", target["site"]
    at30 = {z: n for z, n in f["zones_at"].get(str(int(rules.default_at_s)), {}).items() if z != UNKNOWN}
    no_hit_until = hit["t"] if hit else length
    if len(at30) >= rules.default_min_zones and no_hit_until >= rules.default_no_hit_before_s:
        return "default", (target or {}).get("site")
    if no_hit_until >= rules.slow_no_hit_before_s:
        return "slow", (target or {}).get("site")
    return "unknown", (target or {}).get("site")


def label_ct(f: dict, zones: MapZones, rules: PlayRules) -> tuple[str, str | None]:
    at20 = f["zones_at"].get("20", {})
    for site in sorted(zones.sites):
        if at20.get(zones.site_zone(site), 0) >= rules.stack_min:
            return "stack", site
    if f.get("aggression_s") is not None:
        return "aggression", None
    return "standard", None


# --------------------------------------------------------------------------
# manual labels (a JSON file, so they survive a database rebuild)
# --------------------------------------------------------------------------

def label_key(match_id: str, round_: int, team: str) -> str:
    return f"{match_id}:{int(round_)}:{team}"


def load_manual(path: str | Path) -> dict[str, dict]:
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_manual(path: str | Path, labels: dict[str, dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(labels, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# one match
# --------------------------------------------------------------------------

def compute_plays(tables: dict[str, pd.DataFrame], zones: MapZones, rules: PlayRules,
                  manual: dict[str, dict] | None = None, match_id: str | None = None) -> pd.DataFrame:
    """One row per team per played round: side, label, site, confidence, features, name, label_source."""
    manual = manual or {}
    rounds = tables["rounds"]
    rounds = rounds[rounds["played"].fillna(0).astype(bool)]
    team_rounds = tables["team_rounds"]
    pos_all, gren_all, dmg_all = tables["positions"], tables["grenades"], tables["damages"]
    rows = []
    for r in rounds.sort_values("round").itertuples():
        rnd, length = int(r.round), float(r.duration_s or 0)
        tr = team_rounds[team_rounds["round"] == rnd]
        if tr.empty:
            continue
        pos = pos_all[pos_all["round"] == rnd]
        gren = gren_all[(gren_all["round"] == rnd)]
        if not gren.empty:
            gren = gren[gren["det_time_s"].isna() | (gren["det_time_s"] <= length)]
        dmg = dmg_all[(dmg_all["round"] == rnd)]
        if not dmg.empty:
            dmg = dmg[~dmg["after_round_end"].fillna(0).astype(bool) & dmg["attacker_steamid"].notna()
                      & (dmg["attacker_side"] != dmg["victim_side"])]
        first_contact = float(dmg["time_s"].min()) if not dmg.empty else None
        by_side = {row.side: row for row in tr.itertuples()}
        t_row = by_side.get("T")
        plant = None
        if t_row is not None and bool(t_row.planted) and pd.notna(t_row.plant_time_s):
            plant = {"site": t_row.plant_site, "t": int(t_row.plant_time_s)}
        features = {}
        features["T"] = round_features("T", pos[pos["side"] == "T"], gren[gren["thrower_side"] == "T"] if not gren.empty else gren,
                                       plant, first_contact, length, zones, rules)
        features["CT"] = round_features("CT", pos[pos["side"] == "CT"], gren[gren["thrower_side"] == "CT"] if not gren.empty else gren,
                                        None, first_contact, length, zones, rules, t_hit=features["T"].get("site_hit"))
        for side, row in by_side.items():
            f = features[side]
            label, site = label_t(f, zones, rules) if side == "T" else label_ct(f, zones, rules)
            entry = {"match_id": match_id, "round": rnd, "team": row.team, "side": side, "label": label, "site": site,
                     "confidence": CONFIDENCE[label], "features": json.dumps(f, ensure_ascii=False, sort_keys=True),
                     "name": None, "label_source": "rule"}
            fixed = manual.get(label_key(match_id, rnd, row.team))
            if fixed:
                entry.update({"label": fixed.get("label", label), "site": fixed.get("site", site),
                              "name": fixed.get("name"), "confidence": 1.0, "label_source": "manual"})
            rows.append(entry)
    columns = ["match_id", "round", "team", "side", "label", "site", "confidence", "features", "name", "label_source"]
    return pd.DataFrame(rows, columns=columns)


PLAY_TABLES = {
    "rounds": "SELECT round, played, duration_s FROM rounds WHERE match_id = :m",
    "team_rounds": "SELECT round, team, side, won, planted, plant_site, plant_time_s FROM team_rounds WHERE match_id = :m",
    "positions": "SELECT round, time_s, steamid, side, place FROM positions WHERE match_id = :m",
    "grenades": "SELECT round, type, thrower_side, det_time_s, land_place FROM grenades WHERE match_id = :m",
    "damages": "SELECT round, time_s, attacker_steamid, attacker_side, victim_side, after_round_end FROM damages WHERE match_id = :m",
}


def build_plays(engine: Engine, match_id: str, cfg: Config) -> tuple[pd.DataFrame, MapZones, bool]:
    """Label one stored match and save round_plays. Returns (rows, zones, zone file created)."""
    with engine.connect() as conn:
        map_name = conn.execute(text("SELECT map FROM matches WHERE match_id = :m"), {"m": match_id}).scalar_one()
        tables = {name: pd.read_sql(text(sql), conn, params={"m": match_id}) for name, sql in PLAY_TABLES.items()}
    zones, created = ensure_zones(engine, map_name, cfg.maps_dir)
    rows = compute_plays(tables, zones, cfg.plays, load_manual(cfg.manual_labels_path), match_id)
    with engine.begin() as conn:
        conn.execute(delete(RoundPlay).where(RoundPlay.match_id == match_id))
        if not rows.empty:
            rows.to_sql(RoundPlay.__tablename__, conn, if_exists="append", index=False)
    return rows, zones, created


def set_manual_label(engine: Engine, cfg: Config, match_id: str, round_: int, team: str,
                     label: str, name: str | None = None, site: str | None = None) -> None:
    """Save a label from the IGL: in the JSON file (kept forever) and in round_plays."""
    labels = load_manual(cfg.manual_labels_path)
    entry = {"label": label}
    if name:
        entry["name"] = name
    if site:
        entry["site"] = site
    labels[label_key(match_id, round_, team)] = entry
    save_manual(cfg.manual_labels_path, labels)
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE round_plays SET label = :label, name = :name, site = COALESCE(:site, site), confidence = 1.0, "
            "label_source = 'manual' WHERE match_id = :m AND round = :r AND team = :t"),
            {"label": label, "name": name, "site": site, "m": match_id, "r": int(round_), "t": team})


# --------------------------------------------------------------------------
# reading the labels back
# --------------------------------------------------------------------------

def _our_plays(engine: Engine, map_name: str | None, whose: str = "us") -> pd.DataFrame:
    sql = """
        SELECT rp.*, tr.won, m.map, m.us_team
        FROM round_plays rp
        JOIN team_rounds tr ON tr.match_id = rp.match_id AND tr.round = rp.round AND tr.team = rp.team
        JOIN matches m ON m.match_id = rp.match_id
        WHERE (:map IS NULL OR m.map = :map)
    """
    with engine.connect() as conn:
        df = pd.read_sql(text(sql), conn, params={"map": map_name})
    if whose == "us":
        df = df[df["team"] == df["us_team"]]
    elif whose == "them":
        df = df[df["us_team"].notna() & (df["team"] != df["us_team"])]
    df["won"] = df["won"].fillna(0).astype(bool)
    return df


def play_name(label: str, site, name) -> str:
    if isinstance(name, str) and name:
        return name
    return f"{label} {site}" if isinstance(site, str) and site else label


def coverage(engine: Engine, map_name: str | None = None) -> dict:
    """Phase 6 target: the share of our T rounds with a label other than unknown."""
    df = _our_plays(engine, map_name)
    t = df[df["side"] == "T"]
    labelled = int((t["label"] != "unknown").sum())
    return {"t_rounds": int(len(t)), "labelled": labelled,
            "pct": round(100 * labelled / len(t)) if len(t) else None,
            "manual": int((t["label_source"] == "manual").sum())}


def format_feature_lines(f: dict) -> list[str]:
    lines = []
    for second, counts in f.get("zones_at", {}).items():
        lines.append(f"  at {second:>2}s: " + (setup_string(counts) or "nobody alive"))
    if f.get("first_contact_s") is not None:
        lines.append(f"  first contact at {f['first_contact_s']}s")
    for w in f.get("waves", []):
        kinds = ", ".join(f"{w[k]} {k}" for k in ("smokes", "molotovs", "flashes", "hes") if w[k])
        lines.append(f"  utility wave on {w['zone']} at {w['t']}-{w['end']}s: {kinds}")
    if f.get("site_hit"):
        lines.append(f"  site hit {f['site_hit']['site']} at {f['site_hit']['t']}s, coming from {', '.join(f.get('entry_areas', [])) or '?'}")
    if f.get("plant"):
        lines.append(f"  plant {f['plant']['site']} at {f['plant']['t']}s")
    if f.get("setup_20s"):
        lines.append(f"  setup at 20s: {f['setup_20s']}")
    if f.get("aggression_s") is not None:
        lines.append(f"  a CT was in the T zone at {f['aggression_s']}s")
    if f.get("rotation_s") is not None:
        lines.append(f"  CTs rotated {f['rotation_s']}s after the site hit")
    return lines


def review(engine: Engine, cfg: Config, map_name: str | None = None, below: float | None = None,
           input_fn: Callable[[str], str] = input, print_fn: Callable[[str], None] = print) -> int:
    """Show our unclear rounds and let the IGL set a label and a name. Returns how many were set."""
    below = cfg.plays.review_below if below is None else below
    df = _our_plays(engine, map_name)
    todo = df[(df["label_source"] == "rule") & (df["confidence"] < below)].sort_values(["match_id", "round"])
    if todo.empty:
        print_fn("Nothing to review: every round of ours has a confident or manual label.")
        return 0
    print_fn(f"{len(todo)} round(s) to review. Enter = keep the label, s = skip, q = quit.")
    labels = T_LABELS + CT_LABELS
    changed = 0
    for row in todo.itertuples():
        print_fn(f"\n{row.match_id} round {row.round} ({row.side}, {'won' if row.won else 'lost'}): "
                 f"rule says {play_name(row.label, row.site, None)} (confidence {row.confidence:.1f})")
        for line in format_feature_lines(json.loads(row.features)):
            print_fn(line)
        choices = T_LABELS if row.side == "T" else CT_LABELS
        answer = input_fn(f"label [{'/'.join(choices)}]: ").strip().lower()
        if answer == "q":
            break
        if answer == "s":
            continue
        label = answer or row.label
        if label not in labels:
            print_fn(f"  '{label}' is not a label; skipped.")
            continue
        site = input_fn("site [A/B, Enter = keep]: ").strip().upper() or None
        name = input_fn("name of the play, e.g. 'A split via palace' (Enter = none): ").strip() or None
        set_manual_label(engine, cfg, row.match_id, int(row.round), row.team, label, name, site)
        changed += 1
    print_fn(f"\nSaved {changed} label(s) to {cfg.manual_labels_path}.")
    return changed


def play_table(engine: Engine, map_name: str | None = None, whose: str = "us") -> pd.DataFrame:
    df = _our_plays(engine, map_name, whose)
    if df.empty:
        return pd.DataFrame(columns=["side", "play", "rounds", "won"])
    df["play"] = [play_name(l, s, n) for l, s, n in zip(df["label"], df["site"], df["name"])]
    out = df.groupby(["side", "play"]).agg(rounds=("won", "size"), won=("won", "sum")).reset_index()
    return out.sort_values(["side", "rounds", "play"], ascending=[False, False, True]).reset_index(drop=True)


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Play detection: label, list, review.")
    ap.add_argument("--config", default="config.toml")
    sub = ap.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="label every match (or the given ones)")
    b.add_argument("match_ids", nargs="*")
    for name, text_ in (("list", "plays and their results"), ("review", "correct labels by hand"),
                        ("coverage", "share of our T rounds with a label")):
        p = sub.add_parser(name, help=text_)
        p.add_argument("--map")
        if name == "list":
            p.add_argument("--team", choices=["us", "them", "all"], default="us")
        if name == "review":
            p.add_argument("--below", type=float, help="review labels with less confidence than this")
    s = sub.add_parser("show", help="features of one round")
    s.add_argument("match_id")
    s.add_argument("round", type=int)
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
        with engine.connect() as conn:
            ids = args.match_ids or [r[0] for r in conn.execute(text("SELECT match_id FROM matches ORDER BY ingested_at"))]
        for match_id in ids:
            rows, zones, created = build_plays(engine, match_id, cfg)
            counts = rows[rows["side"] == "T"]["label"].value_counts().to_dict()
            print(f"ok     {match_id}: T-side labels {counts}")
            if created:
                print(f"       wrote a draft {zones.path}: sort its areas into zones, then run build again")
        return 0
    if args.command == "list":
        from .trends import rate, rate_text
        table = play_table(engine, args.map, args.team)
        if table.empty:
            print("No labelled rounds yet. Run: python -m cs2review.plays build")
            return 1
        for row in table.itertuples():
            print(f"{row.side:<3} {row.play:<28} {row.rounds:>4} round(s)   won {rate_text(rate(row.won, row.rounds))}")
        return 0
    if args.command == "coverage":
        c = coverage(engine, args.map)
        if not c["t_rounds"]:
            print("No T-side rounds of ours yet.")
            return 1
        print(f"{c['labelled']} of {c['t_rounds']} T-side rounds have a label other than unknown ({c['pct']}%), "
              f"{c['manual']} set by hand. Phase 6 target: 80%.")
        return 0
    if args.command == "review":
        review(engine, cfg, args.map, args.below)
        return 0
    if args.command == "show":
        with engine.connect() as conn:
            rows = pd.read_sql(text("SELECT * FROM round_plays WHERE match_id = :m AND round = :r ORDER BY side DESC"),
                               conn, params={"m": args.match_id, "r": args.round})
        if rows.empty:
            print("No labels for that round.")
            return 1
        for row in rows.itertuples():
            print(f"team {row.team} ({row.side}): {play_name(row.label, row.site, row.name)} "
                  f"[{row.label_source}, confidence {row.confidence:.1f}]")
            for line in format_feature_lines(json.loads(row.features)):
                print(line)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
