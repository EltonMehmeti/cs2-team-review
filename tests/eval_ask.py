"""Evaluate the question agent against tests/eval_questions.yaml.

    python tests/eval_ask.py                 # uses config.toml's database
    python tests/eval_ask.py --only most_untraded

Calls the real model (ANTHROPIC_API_KEY), so it is NOT part of the test
suite. Each run costs about a cent with Claude Haiku 5.5. Target: 8 of 10.
"""

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

QUESTIONS = Path(__file__).parent / "eval_questions.yaml"


def load_questions() -> list[dict]:
    return yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))


def expected_value(db_path, sql: str):
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        row = conn.execute(sql).fetchone()
    return None if row is None else row[0]


def answer_matches(answer: str, kind: str, expected) -> bool:
    """Does the answer contain the expected value?"""
    if expected is None:
        return bool(re.search(r"no data|not enough|cannot|can't|no rounds", answer, re.I))
    if kind == "name":
        return str(expected).lower() in answer.lower()
    numbers = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", answer)]
    target = float(expected)
    tolerance = 1.0 if target != int(target) or "%" in answer else 0.0
    return any(abs(n - target) <= max(tolerance, 1e-9) for n in numbers)


def main() -> int:
    from cs2review.ask import ask
    from cs2review.config import load_config
    from cs2review.db import get_engine
    from cs2review.llm import LLM

    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="run one question id")
    ap.add_argument("--config", default="config.toml")
    args = ap.parse_args()
    cfg = load_config(args.config)
    engine = get_engine(cfg.db_path)
    llm = LLM.from_config(cfg.llm, engine, provider="anthropic")
    passed = total = 0
    for q in load_questions():
        if args.only and q["id"] != args.only:
            continue
        total += 1
        expected = expected_value(cfg.db_path, q["sql"])
        result = ask(q["question"], engine, cfg.db_path, llm)
        ok = answer_matches(result.answer, q["kind"], expected)
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {q['id']}: expected {expected!r}, {len(result.tool_calls)} tool call(s)")
        if not ok:
            print("      answer: " + result.answer.replace("\n", " ")[:400])
    print(f"\n{passed} of {total} passed (target 8 of 10); cost ${llm.spent_usd:.4f}")
    return 0 if total and passed >= min(8, total) else 1


if __name__ == "__main__":
    raise SystemExit(main())
