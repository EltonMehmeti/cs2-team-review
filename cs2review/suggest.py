"""Phase 9: three plays to try this week, and proof later of whether they worked.

    python -m cs2review.suggest                  # print this week's three suggestions
    python -m cs2review.suggest --post           # and post them to Discord
    python -m cs2review.suggest --dry-run        # do not mark the cards as "suggested"
    python -m cs2review.suggest --feedback       # how the plays we are trying went

How a suggestion is chosen (no AI, plain rules):
  1. Weakness tags: each tag in cs2review/weakness_tags.toml is an SQL query
     over our own rounds, for example "our A hits win under 35% (n >= 8)".
  2. Playbook cards on the same map and side (and site, when the tag names
     one) are scored:
        fit        3 points for a card on the tag's site, 1.8 otherwise
        evidence   up to 2 points: won / seen, only when seen >= 3
        ease       up to 1 point: easier cards first (1 - difficulty / 10)
        freshness  up to 1 point: seen in the last 30 days 1, 90 days 0.5
     Stale cards, dropped ones, ones we already try or adopted, and web cards
     nobody has reviewed yet are skipped.
  3. The best three different cards are posted with why they were chosen,
     a demo example and the lineups, and their status becomes "suggested".
     When no tag has enough data yet, the best-evidenced cards on our maps
     are suggested instead, and the post says so.

Feedback: for every card marked "trying" or "adopted", our own rounds since
that status was set are matched by the card's signature (same site and the
same smoke/molotov landing areas), or by a round the IGL named after the
card in `plays review` (its id or its title). Trends show how often we ran
it and how it went.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, delete, text

from .config import Config, load_config
from .db import Report, SchemaMismatchError, ZoneArea, get_engine
from .playbook import candidate_rounds, set_status, status_since
from .zones import load_zones

TAGS_FILE = Path(__file__).parent / "weakness_tags.toml"
SUGGEST_COUNT = 3
MIN_EVIDENCE_SEEN = 3
ACTIVE = ("trying", "adopted")


def load_tags(path: Path = TAGS_FILE) -> dict[str, dict]:
    with Path(path).open("rb") as f:
        return tomllib.load(f)


def refresh_zone_areas(engine: Engine, cfg: Config) -> int:
    """Copy the zone files into zone_areas so the tag queries can use zones."""
    with engine.connect() as conn:
        maps = [r[0] for r in conn.execute(text("SELECT DISTINCT map FROM matches"))]
    rows = []
    for map_name in maps:
        zones = load_zones(map_name, cfg.maps_dir)
        if zones:
            rows += [{"map": map_name, "area": area, "zone": zone} for zone, areas in zones.zones.items() for area in areas]
    with engine.begin() as conn:
        conn.execute(delete(ZoneArea))
        if rows:
            conn.execute(ZoneArea.__table__.insert(), rows)
    return len(rows)


def find_weaknesses(engine: Engine, cfg: Config, tags: dict[str, dict] | None = None) -> list[dict]:
    """Every tag that fires, one entry per map."""
    tags = tags if tags is not None else load_tags()
    refresh_zone_areas(engine, cfg)
    found = []
    with engine.connect() as conn:
        for name, tag in tags.items():
            for row in conn.execute(text(tag["sql"])).mappings():
                found.append({"tag": name, "description": tag["description"], "side": tag.get("side"),
                              "site": tag.get("site"), "map": row["map"], "n": int(row["n"]),
                              "rate": None if row["rate"] is None else int(row["rate"])})
    return found


def _days_since(day: str | None, today: date) -> int | None:
    if not day:
        return None
    try:
        return (today - datetime.fromisoformat(str(day)[:19]).date()).days
    except ValueError:
        return None


def score_card(card: dict, tag: dict | None, today: date) -> tuple[float, dict]:
    """Fit + evidence + ease + freshness (see the module docstring)."""
    if tag is None:
        fit = 1.0  # fallback suggestions: no weakness to fit
    else:
        fit = 3.0 if tag.get("site") and card["site"] == tag["site"] else 1.8
    evidence = 2.0 * card["won"] / card["seen"] if card["seen"] >= MIN_EVIDENCE_SEEN else 0.0
    ease = max(0.0, 1.0 - card["difficulty"] / 10)
    days = _days_since(card.get("last_seen"), today)
    freshness = 0.3 if days is None else 1.0 if days <= 30 else 0.5 if days <= 90 else 0.2
    parts = {"fit": round(fit, 2), "evidence": round(evidence, 2), "ease": round(ease, 2), "freshness": freshness}
    return round(fit + evidence + ease + freshness, 3), parts


def eligible_cards(engine: Engine) -> pd.DataFrame:
    with engine.connect() as conn:
        cards = pd.read_sql(text("SELECT * FROM playbook"), conn)
    if cards.empty:
        return cards
    cards["stale"] = cards["stale"].fillna(0).astype(bool)
    cards["needs_review"] = cards["needs_review"].fillna(0).astype(bool)
    ok = (~cards["stale"] & cards["status"].isin(["new", "suggested"])
          & ~((cards["source_kind"] == "web") & cards["needs_review"]))
    return cards[ok]


def pick_suggestions(engine: Engine, cfg: Config, maps: list[str] | None = None, today: date | None = None,
                     count: int = SUGGEST_COUNT, tags: dict[str, dict] | None = None) -> list[dict]:
    today = today or date.today()
    cards = eligible_cards(engine)
    if cards.empty:
        return []
    if maps:
        cards = cards[cards["map"].isin(maps)]
    weaknesses = [w for w in find_weaknesses(engine, cfg, tags) if not maps or w["map"] in maps]
    best: dict[str, dict] = {}
    for w in weaknesses:
        match = cards[(cards["map"] == w["map"]) & (cards["side"] == w["side"])]
        if w["site"]:
            match = match[match["site"] == w["site"]]
        for card in match.to_dict("records"):
            score, parts = score_card(card, w, today)
            if card["id"] not in best or score > best[card["id"]]["score"]:
                best[card["id"]] = {"card": card, "tag": w, "score": score, "parts": parts}
    picks = sorted(best.values(), key=lambda p: (-p["score"], p["card"]["id"]))[:count]

    if len(picks) < count:  # not enough weaknesses with data yet: best evidence on our own maps
        with engine.connect() as conn:
            our_maps = [r[0] for r in conn.execute(text("SELECT DISTINCT map FROM matches WHERE source_kind = 'own'"))]
        chosen = {p["card"]["id"] for p in picks}
        rest = cards[cards["map"].isin(maps or our_maps) & ~cards["id"].isin(chosen)]
        extra = []
        for card in rest.to_dict("records"):
            score, parts = score_card(card, None, today)
            extra.append({"card": card, "tag": None, "score": score, "parts": parts})
        picks += sorted(extra, key=lambda p: (-p["score"], p["card"]["id"]))[:count - len(picks)]
    return picks


def why_text(pick: dict) -> str:
    tag, card = pick["tag"], pick["card"]
    evidence = (f"won {card['won']} of {card['seen']} times we saw it" if card["seen"] else "found on the web, not yet seen in a demo")
    if tag is None:
        return f"No weakness has enough rounds yet; this card has the strongest evidence on {card['map']} ({evidence})."
    return f"{tag['description']} on {tag['map']} ({tag['rate']}% over {tag['n']} rounds). This play {evidence}."


def render(picks: list[dict], today: date) -> str:
    if not picks:
        return ("No suggestions this week: the playbook has no usable cards yet. Ingest opponents' and pro demos, run "
                "python -m cs2review.playbook build, or review web cards.")
    lines = [f"**Three plays to try this week** ({today.isoformat()})", ""]
    for i, pick in enumerate(picks, 1):
        card_row = pick["card"]
        card = json.loads(card_row["card"])
        level = "easy" if card_row["difficulty"] <= 3 else "hard" if card_row["difficulty"] >= 7 else "medium"
        lines.append(f"**{i}. {card['title']}** ({card_row['map']}, {card_row['side']} side, card {card_row['id']})")
        lines.append(f"Why: {why_text(pick)}")
        if card_row["source_kind"] != "web":
            lines.append(f"Difficulty: {card_row['difficulty']} ({level}).")
        for step in card["steps"][:8]:
            if "t" in step:
                what = step["do"] + (f" {step['from']} -> {step['to']}" if step.get("to") else "")
                what += f" via {step['via']}" if step.get("via") else ""
                lines.append(f"- {step['t']}s {step['role']}: {what}")
                if step.get("lineup"):
                    lines.append(f"  `{step['lineup']}` ({step.get('throw', '?')} throw)")
            else:
                lines.append(f"- {step['do']}")
        for ex in card.get("examples", [])[:1]:
            if ex.get("command"):
                lines.append(f"Watch: `{ex['command']}` ({ex['team']}, round {ex['round']})")
        if card.get("source", {}).get("url"):
            lines.append(f"Source: {card['source']['url']}")
        lines.append("")
    lines.append("_Lineups need sv_cheats 1 on a private server and are not verified yet. "
                 "Set a card to trying with: python -m cs2review.plays status <card> trying_")
    return "\n".join(lines)


def feedback(engine: Engine, cfg: Config, map_name: str | None = None) -> list[dict]:
    """For every card we try or adopted: our rounds with that play since the status was set."""
    with engine.connect() as conn:
        cards = pd.read_sql(text("SELECT id, signature, map, title, status FROM playbook WHERE status IN ('trying', 'adopted')"), conn)
        named = pd.read_sql(text("""
            SELECT rp.match_id, rp.round, rp.name, tr.won, m.file_time, m.map
            FROM round_plays rp
            JOIN matches m ON m.match_id = rp.match_id AND rp.team = m.us_team
            JOIN team_rounds tr ON tr.match_id = rp.match_id AND tr.round = rp.round AND tr.team = rp.team
            WHERE m.source_kind = 'own' AND rp.name IS NOT NULL"""), conn)
    if map_name:
        cards = cards[cards["map"] == map_name]
    if cards.empty:
        return []
    ours = candidate_rounds(engine, cfg, whose="us")
    since = status_since(cfg.playbook_status_path)
    out = []
    for card in cards.itertuples():
        start = since.get(card.id) or ""
        rounds: dict[tuple, bool] = {}
        if not ours.empty:
            same = ours[(ours["signature"] == card.signature) & (ours["file_time"].fillna("") >= start)]
            rounds.update({(m, int(r)): bool(w) for m, r, w in zip(same["match_id"], same["round"], same["won"])})
        by_name = named[named["name"].isin([card.id, card.title]) & (named["map"] == card.map)
                        & (named["file_time"].fillna("") >= start)]
        rounds.update({(m, int(r)): bool(w) for m, r, w in zip(by_name["match_id"], by_name["round"], by_name["won"])})
        from .trends import rate
        out.append({"id": card.id, "map": card.map, "title": card.title, "status": card.status, "since": start or None,
                    "runs": rate(sum(rounds.values()), len(rounds)), "rounds": sorted(f"{m} r{r}" for m, r in rounds)})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Three plays to try this week.")
    ap.add_argument("--map", action="append", help="only these maps")
    ap.add_argument("--post", action="store_true", help="post to Discord (DISCORD_WEBHOOK_URL)")
    ap.add_argument("--dry-run", action="store_true", help="do not change card statuses or store the post")
    ap.add_argument("--feedback", action="store_true", help="show how the plays we try went")
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

    if args.feedback:
        from .trends import rate_text
        rows = feedback(engine, cfg)
        if not rows:
            print("No cards are marked trying or adopted.")
        for f in rows:
            print(f"{f['id']}  {f['map']:<12} {f['title']:<45} {f['status']:<8} since {f['since'] or '?'}: "
                  f"ran {f['runs']['n']} time(s), won {rate_text(f['runs'])}")
        return 0

    today = date.today()
    weak = find_weaknesses(engine, cfg)
    if weak:
        print("Weaknesses found:")
        for w in weak:
            print(f"  {w['tag']:<20} {w['map']:<12} {w['rate']}% (n={w['n']})  {w['description']}")
        print()
    picks = pick_suggestions(engine, cfg, args.map, today)
    text_ = render(picks, today)
    print(text_)
    if not picks or args.dry_run:
        return 0
    for pick in picks:
        if pick["card"]["status"] == "new":
            set_status(engine, cfg, pick["card"]["id"], "suggested")
    with engine.begin() as conn:
        conn.execute(Report.__table__.insert().values(
            kind="suggestions", match_id=f"suggestions:{today.isoformat()}",
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), provider="rules", model="rules",
            input_tokens=0, output_tokens=0, cost_usd=0.0,
            report_json=json.dumps([{"id": p["card"]["id"], "score": p["score"], "parts": p["parts"],
                                     "tag": p["tag"]["tag"] if p["tag"] else None} for p in picks]),
            report_md=text_))
    if args.post:
        from .notify import post_to_discord
        try:
            print(f"\nPosted to Discord in {post_to_discord(text_)} message(s).")
        except RuntimeError as exc:
            print(f"\nNot posted: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
