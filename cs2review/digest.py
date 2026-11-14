"""Print the match digest: the compact JSON the LLM reads (Phase 3).

    python -m cs2review.digest                    # the latest match
    python -m cs2review.digest MATCH_ID           # one match
    python -m cs2review.digest MATCH_ID --pretty  # indented, easier to read
    python -m cs2review.digest MATCH_ID --out digest.json

It also stores the digest in the database (tables round_summaries and
match_digests). Ingest does this for every new demo already.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config
from .db import SchemaMismatchError, get_engine, list_matches
from .summaries import TOKEN_BUDGET, build_digest, to_json


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build and print the match digest (the LLM's input).")
    ap.add_argument("match_id", nargs="?", help="match to digest (default: the latest one)")
    ap.add_argument("--out", type=Path, help="write the JSON to this file instead of the screen")
    ap.add_argument("--pretty", action="store_true", help="indent the JSON")
    ap.add_argument("--budget", type=int, default=TOKEN_BUDGET, help=f"token budget (default {TOKEN_BUDGET})")
    ap.add_argument("--config", default="config.toml")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        engine = get_engine(load_config(args.config).db_path)
    except SchemaMismatchError as exc:
        print(exc)
        return 1
    matches = list_matches(engine)
    if matches.empty:
        print("No matches in the database yet. Run:  python -m cs2review.ingest")
        return 1
    match_id = args.match_id or matches["match_id"].iloc[-1]
    if match_id not in set(matches["match_id"]):
        print(f"Unknown match id: {match_id}. Use python -m cs2review.check --list.")
        return 1

    digest, tokens = build_digest(engine, match_id, budget=args.budget)
    text = to_json(digest, pretty=args.pretty)
    note = f"about {tokens} tokens (budget {args.budget})"
    if digest.get("dropped"):
        note += ", dropped to fit: " + ", ".join(digest["dropped"])
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}: {note}")
    else:
        print(text)
        print(f"\n# {note}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
