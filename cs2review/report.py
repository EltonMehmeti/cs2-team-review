"""Phase 4: the match report. The LLM writes JSON, the code checks and renders it.

    python -m cs2review.report                       # latest match, provider from config.toml
    python -m cs2review.report MATCH_ID --provider manual
    python -m cs2review.report MATCH_ID --post       # also post it to Discord

With --provider manual the first run writes out/prompt_<match>.md. Paste it
into any chat app, save the JSON answer as out/answer_<match>.json and run
the same command again.

What the code checks before a report is accepted (the model only explains,
code counts):
  * exactly 3 "worked", 3 "problems", 5 "rewatch" items and a "practice" line
  * every cited round exists and was played
  * every problem cites at least 2 rounds (otherwise it is not recurring)
  * every rewatch second "t" lies inside that round
If the answer fails, the model gets one retry with the list of problems.
Then the command fails loudly instead of posting something unchecked.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import Engine, text

from .config import load_config
from .db import Report, SchemaMismatchError, get_engine, list_matches
from .llm import LLM, LLMError, LLMResult, ManualAnswerNeeded, extract_json
from .summaries import build_digest, to_json

PROMPTS = Path(__file__).parent / "prompts"
COUNTS = {"worked": 3, "problems": 3, "rewatch": 5}
MIN_PROBLEM_ROUNDS = 2
REWATCH_LEAD_S = 5  # start the replay this many seconds before the moment
MAX_OUTPUT_TOKENS = 4000

# The JSON schema sent to the model (structured outputs). Counts are checked in
# code, because not every schema keyword is supported by every provider.
_ITEM = {"type": "string"}
_ROUNDS = {"type": "array", "items": {"type": "integer"}}
REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "worked": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": _ITEM, "rounds": _ROUNDS, "why": _ITEM},
            "required": ["title", "rounds", "why"], "additionalProperties": False}},
        "problems": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": _ITEM, "rounds": _ROUNDS, "why": _ITEM, "fix": _ITEM},
            "required": ["title", "rounds", "why", "fix"], "additionalProperties": False}},
        "rewatch": {"type": "array", "items": {
            "type": "object",
            "properties": {"round": {"type": "integer"}, "t": {"type": "integer"}, "why": _ITEM},
            "required": ["round", "t", "why"], "additionalProperties": False}},
        "practice": _ITEM,
    },
    "required": ["worked", "problems", "rewatch", "practice"],
    "additionalProperties": False,
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Worked(_Strict):
    title: str
    rounds: list[int]
    why: str


class Problem(_Strict):
    title: str
    rounds: list[int]
    why: str
    fix: str


class Rewatch(_Strict):
    round: int
    t: int
    why: str


class ReportModel(_Strict):
    worked: list[Worked]
    problems: list[Problem]
    rewatch: list[Rewatch]
    practice: str


class ReportInvalid(LLMError):
    """The model's report failed the checks twice."""


# --------------------------------------------------------------------------
# checking
# --------------------------------------------------------------------------

def round_lengths(digest: dict) -> dict[int, int]:
    """Played round -> its length in whole seconds, from the digest."""
    return {int(r["round"]): int(r["result"]["length_s"]) for r in digest["rounds"]}


def validate_report(answer_text: str, lengths: dict[int, int]) -> tuple[dict | None, list[str]]:
    """Parse and check the model's answer. Returns (report or None, problems)."""
    try:
        raw = json.loads(extract_json(answer_text))
    except (json.JSONDecodeError, LLMError) as exc:
        return None, [f"the answer is not valid JSON ({exc})"]
    try:
        report = ReportModel.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:8])
        return None, [f"the JSON does not have the required shape: {details}"]

    problems: list[str] = []
    for key, n in COUNTS.items():
        got = len(getattr(report, key))
        if got != n:
            problems.append(f'"{key}" must have exactly {n} items, it has {got}')
    if not report.practice.strip():
        problems.append('"practice" is empty')

    def check_rounds(where: str, rounds: list[int]) -> None:
        unknown = sorted({r for r in rounds if r not in lengths})
        if unknown:
            problems.append(f"{where} cites rounds that were not played: {unknown}")

    for i, item in enumerate(report.worked, 1):
        if not item.rounds:
            problems.append(f"worked item {i} cites no rounds")
        check_rounds(f"worked item {i}", item.rounds)
    for i, item in enumerate(report.problems, 1):
        if len(set(item.rounds)) < MIN_PROBLEM_ROUNDS:
            problems.append(f"problem {i} ('{item.title}') needs at least {MIN_PROBLEM_ROUNDS} different rounds")
        check_rounds(f"problem {i}", item.rounds)
    for i, item in enumerate(report.rewatch, 1):
        if item.round not in lengths:
            problems.append(f"rewatch {i} points at round {item.round}, which was not played")
        elif not 0 <= item.t <= lengths[item.round]:
            problems.append(f"rewatch {i}: t={item.t} is outside round {item.round} (0 to {lengths[item.round]} s)")
    return report.model_dump(), problems


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def rewatch_tick(freeze_end_tick: int, t: int, tick_rate: int) -> int:
    """The demo tick to jump to: REWATCH_LEAD_S seconds before the moment.

    Not verified yet: whether demo_gototick in the game counts ticks like the
    parser does. The owner must try one (PLAN.md, Phase 4).
    """
    return int(freeze_end_tick) + max(int(t) - REWATCH_LEAD_S, 0) * int(tick_rate)


def _rounds_text(rounds: list[int]) -> str:
    rounds = sorted(set(rounds))
    return ("round " if len(rounds) == 1 else "rounds ") + ", ".join(str(r) for r in rounds)


def render_markdown(report: dict, digest: dict, demo_file: str, freeze_ticks: dict[int, int], tick_rate: int,
                    model: str) -> str:
    m = digest["match"]
    if m.get("won") is True:
        verdict = f"we won {m['final_score']}"
    elif m.get("won") is False:
        verdict = f"we lost {m['final_score']}"
    else:
        verdict = f"draw {m['final_score']}"
    lines = [f"**Match review: {m['map']}, {verdict}** ({m['us']} vs {m['them']}, match {m['match_id']})", ""]
    lines.append("**What worked**")
    for i, item in enumerate(report["worked"], 1):
        lines.append(f"{i}. **{item['title']}** ({_rounds_text(item['rounds'])}): {item['why']}")
    lines += ["", "**Problems**"]
    for i, item in enumerate(report["problems"], 1):
        lines.append(f"{i}. **{item['title']}** ({_rounds_text(item['rounds'])}): {item['why']}")
        lines.append(f"   Fix: {item['fix']}")
    lines += ["", "**Rewatch together**"]
    for item in report["rewatch"]:
        lines.append(f"- Round {item['round']}, {item['t']} s in: {item['why']}")
        freeze = freeze_ticks.get(int(item["round"]))
        if freeze is not None:
            lines.append(f"  `playdemo {demo_file}; demo_gototick {rewatch_tick(freeze, item['t'], tick_rate)}`")
    lines += ["", f"**Practice:** {report['practice']}", "",
              f"_Numbers come from the demo, checked against the game's scoreboard. Text by {model}. "
              f"The demo_gototick commands are not verified yet._"]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# the whole flow
# --------------------------------------------------------------------------

def load_digest(engine: Engine, match_id: str) -> dict:
    with engine.connect() as conn:
        row = conn.execute(text("SELECT json FROM match_digests WHERE match_id = :m"), {"m": match_id}).fetchone()
    if row is not None:
        return json.loads(row[0])
    digest, _ = build_digest(engine, match_id)
    return digest


def build_prompts(digest: dict) -> tuple[str, str]:
    system = (PROMPTS / "report_system.txt").read_text(encoding="utf-8")
    user = (PROMPTS / "report_user.txt").read_text(encoding="utf-8").format(
        digest=to_json(digest), rounds=", ".join(str(r["round"]) for r in digest["rounds"]),
    )
    return system, user


def write_report(engine: Engine, match_id: str, llm: LLM) -> dict:
    """Ask the model, check the answer (one retry), render and store. Returns the stored row."""
    digest = load_digest(engine, match_id)
    lengths = round_lengths(digest)
    system, user = build_prompts(digest)

    results: list[LLMResult] = []
    first = llm.complete(system, user, MAX_OUTPUT_TOKENS, json_schema=REPORT_SCHEMA, purpose="report", key=match_id)
    results.append(first)
    report, problems = validate_report(first.text, lengths)
    if problems:
        retry_user = (
            user + "\n\nYour previous answer was:\n\n" + first.text.strip()
            + "\n\nIt had these problems:\n" + "\n".join(f"- {p}" for p in problems)
            + "\n\nReturn a corrected JSON object."
        )
        second = llm.complete(system, retry_user, MAX_OUTPUT_TOKENS, json_schema=REPORT_SCHEMA,
                              purpose="report_retry", key=f"{match_id}_retry")
        results.append(second)
        report, problems = validate_report(second.text, lengths)
        if problems:
            raise ReportInvalid("The model's report failed the checks twice:\n" + "\n".join(f"  - {p}" for p in problems))

    with engine.connect() as conn:
        match = pd.read_sql(text("SELECT file_name, tick_rate FROM matches WHERE match_id = :m"), conn, params={"m": match_id}).iloc[0]
        rounds = pd.read_sql(text("SELECT round, freeze_end_tick FROM rounds WHERE match_id = :m AND played = 1"),
                             conn, params={"m": match_id})
    freeze_ticks = {int(r): int(f) for r, f in zip(rounds["round"], rounds["freeze_end_tick"]) if pd.notna(f)}
    md = render_markdown(report, digest, str(match["file_name"]), freeze_ticks, int(match["tick_rate"]), llm.model)

    row = {
        "match_id": match_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": llm.provider_name,
        "model": llm.model,
        "input_tokens": sum(r.input_tokens for r in results),
        "output_tokens": sum(r.output_tokens for r in results),
        "cost_usd": round(sum(r.cost_usd for r in results), 6),
        "report_json": json.dumps(report, ensure_ascii=False),
        "report_md": md,
    }
    with engine.begin() as conn:
        conn.execute(Report.__table__.insert().values(**row))
    row["retried"] = len(results) > 1
    return row


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write the match report with an LLM, check it, and optionally post it.")
    ap.add_argument("match_id", nargs="?", help="match to review (default: the latest one)")
    ap.add_argument("--provider", choices=["anthropic", "manual"], help="override [llm] provider in config.toml")
    ap.add_argument("--post", action="store_true", help="post the report to Discord (DISCORD_WEBHOOK_URL)")
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
    matches = list_matches(engine)
    if matches.empty:
        print("No matches in the database yet. Run:  python -m cs2review.ingest")
        return 1
    match_id = args.match_id or matches["match_id"].iloc[-1]
    if match_id not in set(matches["match_id"]):
        print(f"Unknown match id: {match_id}. Use python -m cs2review.check --list.")
        return 1

    try:
        llm = LLM.from_config(cfg.llm, engine, provider=args.provider)
        row = write_report(engine, match_id, llm)
    except ManualAnswerNeeded as exc:
        print(exc)
        return 0
    except LLMError as exc:
        print(f"FAILED: {exc}")
        return 1

    print(row["report_md"])
    print(f"\n({row['provider']} / {row['model']}: {row['input_tokens']} tokens in, {row['output_tokens']} out, "
          f"${row['cost_usd']:.4f}{', after one retry' if row['retried'] else ''})")
    if args.post:
        from .notify import post_to_discord
        try:
            n = post_to_discord(row["report_md"])
        except RuntimeError as exc:
            print(f"Not posted: {exc}")
            return 1
        print(f"Posted to Discord in {n} message(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
