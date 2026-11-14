"""Phase 5: the weekly trend report, written by the LLM from the trend numbers.

Run through:  python -m cs2review.trends --map de_mirage --last 10 --report

Same pipeline as the match report: the model returns JSON, the code checks it.
Here a claim cannot cite round numbers (it is about many matches), so each
item cites "evidence": paths into the trend data such as
"de_mirage.T.opening_duels". The code checks that every path exists and has
at least MIN_N rounds behind it, then prints the numbers itself. The model
is told not to write numbers at all.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import Engine

from .db import Report
from .llm import LLM, LLMError, ManualAnswerNeeded, extract_json
from .trends import rate_text

PROMPTS = Path(__file__).parent / "prompts"
COUNTS = {"strengths": 2, "weaknesses": 3}
MAX_EVIDENCE = 3

_S = {"type": "string"}
_EVIDENCE = {"type": "array", "items": {"type": "string"}}
TREND_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": _S,
        "strengths": {"type": "array", "items": {
            "type": "object", "properties": {"title": _S, "why": _S, "evidence": _EVIDENCE},
            "required": ["title", "why", "evidence"], "additionalProperties": False}},
        "weaknesses": {"type": "array", "items": {
            "type": "object", "properties": {"title": _S, "why": _S, "fix": _S, "evidence": _EVIDENCE},
            "required": ["title", "why", "fix", "evidence"], "additionalProperties": False}},
        "focus": _S,
    },
    "required": ["headline", "strengths", "weaknesses", "focus"],
    "additionalProperties": False,
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Strength(_Strict):
    title: str
    why: str
    evidence: list[str]


class Weakness(_Strict):
    title: str
    why: str
    fix: str
    evidence: list[str]


class TrendReport(_Strict):
    headline: str
    strengths: list[Strength]
    weaknesses: list[Weakness]
    focus: str


def resolve(trends: dict, path: str):
    """Find the value a path points at, or None.

    "de_mirage.T.opening_duels"        -> maps.de_mirage.sides.T.opening_duels
    "de_mirage.players.NAME.kast"      -> the player called NAME (names may contain dots)
    """
    parts = path.split(".")
    if len(parts) < 3 or parts[0] not in trends.get("maps", {}):
        return None
    block = trends["maps"][parts[0]]
    if parts[1] == "players":
        if len(parts) < 4:
            return None
        name, field = ".".join(parts[2:-1]), parts[-1]
        player = next((p for p in block["players"] if p["name"] == name), None)
        return None if player is None else player.get(field)
    node = block["sides"].get(parts[1])
    for part in parts[2:]:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def evidence_text(trends: dict, path: str) -> str:
    value = resolve(trends, path)
    label = path.split(".", 1)[1].replace(".", " ").replace("_", " ")
    if isinstance(value, dict) and "won" in value:
        return f"{label}: {rate_text(value)}"
    return f"{label}: {value}"


def validate_trend_report(answer_text: str, trends: dict) -> tuple[dict | None, list[str]]:
    try:
        raw = json.loads(extract_json(answer_text))
    except (json.JSONDecodeError, LLMError) as exc:
        return None, [f"the answer is not valid JSON ({exc})"]
    try:
        report = TrendReport.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:8])
        return None, [f"the JSON does not have the required shape: {details}"]
    problems: list[str] = []
    for key, n in COUNTS.items():
        if len(getattr(report, key)) != n:
            problems.append(f'"{key}" must have exactly {n} items, it has {len(getattr(report, key))}')
    for key in ("headline", "focus"):
        if not getattr(report, key).strip():
            problems.append(f'"{key}" is empty')
    for kind, items in (("strength", report.strengths), ("weakness", report.weaknesses)):
        for i, item in enumerate(items, 1):
            if not 1 <= len(item.evidence) <= MAX_EVIDENCE:
                problems.append(f"{kind} {i} must cite 1 to {MAX_EVIDENCE} evidence paths")
            for path in item.evidence:
                value = resolve(trends, path)
                if value is None:
                    problems.append(f"{kind} {i}: evidence path '{path}' does not exist in the trend data")
                elif isinstance(value, dict) and "pct" in value and value["pct"] is None:
                    problems.append(f"{kind} {i}: '{path}' is based on too few rounds (n={value['n']})")
                elif isinstance(value, dict) and "won" not in value:
                    problems.append(f"{kind} {i}: '{path}' is a group, cite one rate inside it")
    return report.model_dump(), problems


def render_trend_markdown(report: dict, trends: dict, model: str) -> str:
    f = trends["filter"]
    scope = f"{f['map']}, {f['matches']} match(es)" + (f", last {f['last']}" if f["last"] else "")
    lines = [f"**Weekly trends ({scope})**", "", report["headline"], "", "**Going well**"]
    for i, item in enumerate(report["strengths"], 1):
        lines.append(f"{i}. **{item['title']}**: {item['why']}")
        lines.append("   " + "; ".join(evidence_text(trends, p) for p in item["evidence"]))
    lines += ["", "**To work on**"]
    for i, item in enumerate(report["weaknesses"], 1):
        lines.append(f"{i}. **{item['title']}**: {item['why']}")
        lines.append("   " + "; ".join(evidence_text(trends, p) for p in item["evidence"]))
        lines.append(f"   Fix: {item['fix']}")
    lines += ["", f"**Focus next week:** {report['focus']}", "",
              f"_Numbers are computed from the demos (rates need 8+ rounds). Text by {model}._"]
    return "\n".join(lines)


def write_trend_report(engine: Engine, trends: dict, llm: LLM) -> dict:
    """Ask, check (one retry), render, store. Returns the stored row."""
    if not trends.get("maps"):
        raise LLMError("No trend data for our team yet.")
    f = trends["filter"]
    key = f"trends_{f['map']}_{f['last'] or 'all'}"
    system = (PROMPTS / "trends_system.txt").read_text(encoding="utf-8")
    user = (PROMPTS / "trends_user.txt").read_text(encoding="utf-8").format(
        trends=json.dumps(trends, ensure_ascii=False, separators=(",", ":")))
    results = [llm.complete(system, user, 3000, json_schema=TREND_SCHEMA, purpose="trends", key=key)]
    report, problems = validate_trend_report(results[0].text, trends)
    if problems:
        retry = (user + "\n\nYour previous answer was:\n\n" + results[0].text.strip()
                 + "\n\nIt had these problems:\n" + "\n".join(f"- {p}" for p in problems)
                 + "\n\nReturn a corrected JSON object.")
        results.append(llm.complete(system, retry, 3000, json_schema=TREND_SCHEMA, purpose="trends_retry", key=f"{key}_retry"))
        report, problems = validate_trend_report(results[1].text, trends)
        if problems:
            raise LLMError("The model's trend report failed the checks twice:\n" + "\n".join(f"  - {p}" for p in problems))
    md = render_trend_markdown(report, trends, llm.model)
    row = {
        "kind": "trends", "match_id": f"trends:{f['map']}:{f['last'] or 'all'}",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": llm.provider_name, "model": llm.model,
        "input_tokens": sum(r.input_tokens for r in results), "output_tokens": sum(r.output_tokens for r in results),
        "cost_usd": round(sum(r.cost_usd for r in results), 6),
        "report_json": json.dumps(report, ensure_ascii=False), "report_md": md,
    }
    with engine.begin() as conn:
        conn.execute(Report.__table__.insert().values(**row))
    return row


def run_trend_report(engine: Engine, cfg, trends: dict, provider: str | None = None, post: bool = False) -> int:
    """The --report part of `python -m cs2review.trends`."""
    try:
        llm = LLM.from_config(cfg.llm, engine, provider=provider)
        row = write_trend_report(engine, trends, llm)
    except ManualAnswerNeeded as exc:
        print(exc)
        return 0
    except LLMError as exc:
        print(f"FAILED: {exc}")
        return 1
    print("\n" + row["report_md"])
    print(f"\n({row['provider']} / {row['model']}: ${row['cost_usd']:.4f})")
    if post:
        from .notify import post_to_discord
        try:
            print(f"Posted to Discord in {post_to_discord(row['report_md'])} message(s).")
        except RuntimeError as exc:
            print(f"Not posted: {exc}")
            return 1
    return 0
