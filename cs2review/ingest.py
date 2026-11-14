"""Read demo files and store them in the database.

    python -m cs2review.ingest                 # everything in the inbox folder
    python -m cs2review.ingest path/to/x.dem   # specific files or folders
    python -m cs2review.ingest --force         # re-read matches already stored
    python -m cs2review.ingest --kind pro      # pro demos from pro_inbox/ (downloaded by hand)

Run this after you have finished playing, not during a match: it is harmless
to the game, but there is no reason to make your PC do extra work mid-round.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

from demoparser2 import DemoParser

from .check import check_match
from .config import load_config
from .db import SchemaMismatchError, get_engine, has_match, load_place_cells, save_match
from .metrics import build_match_metrics
from .plays import build_plays
from .parse import parse_demo
from .summaries import build_digest
from .unpack import is_demo_file, match_id_for, unpack_demo


def find_demos(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found.extend(sorted(p for p in path.iterdir() if is_demo_file(p)))
        elif is_demo_file(path):
            found.append(path)
        else:
            print(f"skip   {path} (not a .dem, .dem.zst, .dem.gz or .dem.bz2 file)")
    return found


def ingest_file(src: Path, engine, cfg, force: bool = False, kind: str = "own") -> str:
    """Ingest one file. Returns 'new', 'skipped' or raises."""
    match_id = match_id_for(src)
    if has_match(engine, match_id) and not force:
        print(f"skip   {src.name} (already in the database, use --force to re-read)")
        return "skipped"

    started = time.time()
    dem = unpack_demo(src, cfg.demos_dir)
    map_name = DemoParser(str(dem)).parse_header().get("map_name", "unknown")
    tables = parse_demo(
        dem,
        team_steam_ids=cfg.team_steam_ids if kind == "own" else [],  # a pro match has no "us"
        known_cells=load_place_cells(engine, map_name),
        match_id=match_id,
    )
    tables["matches"]["source_kind"] = kind
    save_match(engine, tables)
    build_match_metrics(engine, match_id, cfg.buy_types)  # Phase 2: team_rounds, player_round_stats
    _, zones, zones_created = build_plays(engine, match_id, cfg)  # Phase 6: round_plays
    _, tokens = build_digest(engine, match_id)  # Phase 3: round_summaries, match_digests

    m = tables["matches"].iloc[0]
    ok, findings = check_match(engine, match_id)
    print(
        f"ok     {src.name}: {m['map']}, team A {m['score_a']} : {m['score_b']} team B, "
        f"{len(tables['kills'])} kills, {len(tables['grenades'])} grenades, "
        f"digest ~{tokens} tokens, {time.time() - started:.1f}s"
    )
    if zones_created:
        print(f"       note: wrote a draft zone file {zones.path}; sort its areas so plays can be labelled")
    if kind == "own" and cfg.team_steam_ids and m["us_team"] is None:
        print("       note: none of your team_steam_ids played in this match")
    if ok:
        print("       check OK: matches the demo's own scoreboard")
    else:
        print("       CHECK FAILED (run python -m cs2review.check for details):")
        for finding in findings:
            print(f"         - {finding}")
    return "new"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Parse CS2 demos into the SQLite database.")
    ap.add_argument("paths", nargs="*", type=Path, help="demo files or folders (default: the inbox folder)")
    ap.add_argument("--force", action="store_true", help="re-read matches that are already stored")
    ap.add_argument("--kind", choices=["own", "pro"], default="own",
                    help="own = our matches (default, from the inbox); pro = pro demos (default folder pro_inbox/)")
    ap.add_argument("--config", default="config.toml")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):  # player names can contain any character
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cfg = load_config(args.config)
    try:
        engine = get_engine(cfg.db_path)
    except SchemaMismatchError as exc:
        print(exc)
        return 1
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)

    default_dir = cfg.inbox_dir if args.kind == "own" else cfg.pro_inbox_dir
    default_dir.mkdir(parents=True, exist_ok=True)
    demos = find_demos(args.paths or [default_dir])
    if not demos:
        print(f"No demo files found. Put .dem or .dem.zst files into: {default_dir.resolve()}")
        return 1

    failed = 0
    for src in demos:
        try:
            ingest_file(src, engine, cfg, force=args.force, kind=args.kind)
        except ValueError as exc:  # a problem with this file that we can explain
            failed += 1
            print(f"FAILED {src.name}: {exc}")
        except Exception as exc:  # anything else: show where it happened, keep going
            failed += 1
            print(f"FAILED {src.name}: {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=4)
    print(f"\nDone. Database: {cfg.db_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
