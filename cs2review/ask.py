"""Phase 5: ask questions about our matches in plain words.

    python -m cs2review.ask "What is our round win rate on CT side of Mirage?"

A small agent: the model may call three tools, at most MAX_TOOL_CALLS times
per question, and must then answer:

  run_sql(query)          one read-only SELECT; the database is opened read-only
  describe_schema()       tables, views, columns and what they mean
  get_round(match, round) the stored summary of one round

The model never counts rows itself: SQL does the counting. Needs the
anthropic provider (the manual provider cannot use tools).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine, inspect, text

from .config import load_config
from .db import SchemaMismatchError, get_engine
from .llm import LLM, LLMError

PROMPTS = Path(__file__).parent / "prompts"
MAX_TOOL_CALLS = 8
MAX_ROWS = 200  # LIMIT added to queries that have none
QUERY_TIMEOUT_S = 5.0
MAX_RESULT_CHARS = 20_000

# Words that change a database. The connection is read-only anyway; this list
# only gives the model a clear message instead of an SQLite error.
WRITE_WORDS = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|reindex|replace\s+into)\b", re.I)
SCHEMA_TABLES = ["our_rounds", "our_player_rounds", "our_kills", "matches", "players", "rounds", "team_rounds",
                 "player_round_stats", "kills", "damages", "grenades", "blinds", "bomb_events", "positions", "round_summaries"]

TOOLS = [
    {
        "name": "run_sql",
        "description": "Run one read-only SQLite SELECT (or WITH ... SELECT) statement on the team's database and get "
                       f"the rows as JSON. A LIMIT {MAX_ROWS} is added when the query has none.",
        "strict": True,
        "input_schema": {"type": "object", "properties": {"query": {"type": "string", "description": "the SELECT statement"}},
                         "required": ["query"], "additionalProperties": False},
    },
    {
        "name": "describe_schema",
        "description": "List the tables and views with their columns and a one-line meaning of each.",
        "strict": True,
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "get_round",
        "description": "Get the compact summary of one round: buys, positions at 20 s, utility, kills, bomb, result, flags.",
        "strict": True,
        "input_schema": {"type": "object",
                         "properties": {"match_id": {"type": "string"}, "round": {"type": "integer"}},
                         "required": ["match_id", "round"], "additionalProperties": False},
    },
]


class QueryRefused(ValueError):
    """The query is not a single read-only SELECT."""


def check_query(query: str) -> str:
    """Return the query ready to run (with a LIMIT), or raise QueryRefused."""
    sql = query.strip()
    while sql.endswith(";"):
        sql = sql[:-1].rstrip()
    if not sql:
        raise QueryRefused("The query is empty.")
    if ";" in sql:
        raise QueryRefused("Only one statement is allowed (no ';' inside the query).")
    if not re.match(r"^(select|with)\b", sql, re.I):
        raise QueryRefused("Only SELECT or WITH ... SELECT queries are allowed.")
    if WRITE_WORDS.search(sql):
        raise QueryRefused("This query would change the database. Only reading is allowed.")
    if not re.search(r"\blimit\b", sql, re.I):
        sql += f" LIMIT {MAX_ROWS}"
    return sql


def run_sql(db_path: str | Path, query: str, timeout_s: float = QUERY_TIMEOUT_S) -> dict:
    """Run a checked query on a read-only connection. Returns rows or {"error": ...}."""
    try:
        sql = check_query(query)
    except QueryRefused as exc:
        return {"error": str(exc)}
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"  # read-only: SQLite itself refuses any write
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        deadline = time.monotonic() + timeout_s
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        cursor = conn.execute(sql)
        rows = cursor.fetchmany(MAX_ROWS + 1)
        columns = [d[0] for d in cursor.description or []]
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc):
            return {"error": f"The query took longer than {timeout_s:.0f} seconds and was stopped. Make it simpler."}
        return {"error": f"SQLite error: {exc}"}
    except sqlite3.Error as exc:
        return {"error": f"SQLite error: {exc}"}
    finally:
        conn.close()
    return {"columns": columns, "rows": [list(r) for r in rows[:MAX_ROWS]], "row_count": min(len(rows), MAX_ROWS),
            "truncated": len(rows) > MAX_ROWS}


def describe_schema(engine: Engine) -> str:
    notes = (PROMPTS / "schema_notes.txt").read_text(encoding="utf-8").strip()
    insp = inspect(engine)
    names = set(insp.get_table_names()) | set(insp.get_view_names())
    lines = [notes, "", "Columns:"]
    for name in SCHEMA_TABLES:
        if name in names:
            cols = [c["name"] for c in insp.get_columns(name)]
            lines.append(f"- {name}: {', '.join(cols)}")
    return "\n".join(lines)


def get_round(engine: Engine, match_id: str, round_: int) -> dict:
    with engine.connect() as conn:
        row = conn.execute(text("SELECT json FROM round_summaries WHERE match_id = :m AND round = :r"),
                           {"m": match_id, "r": int(round_)}).fetchone()
    if row is None:
        return {"error": f"No summary for match {match_id} round {round_}. Use run_sql on matches/rounds to find valid ones."}
    return json.loads(row[0])


@dataclass
class AskResult:
    answer: str
    tool_calls: list[dict] = field(default_factory=list)  # {"name", "input", "error"}
    cost_usd: float = 0.0


def _tool_result(content: dict | str) -> tuple[str, bool]:
    if isinstance(content, str):
        return content, False
    is_error = "error" in content
    body = json.dumps(content, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(body) > MAX_RESULT_CHARS:
        body = body[:MAX_RESULT_CHARS] + '..."(cut: too long, ask for fewer rows or columns)"'
    return body, is_error


def ask(question: str, engine: Engine, db_path: str | Path, llm: LLM, max_tool_calls: int = MAX_TOOL_CALLS) -> AskResult:
    """Answer one question with up to max_tool_calls tool calls."""
    schema = describe_schema(engine)
    system = (PROMPTS / "ask_system.txt").read_text(encoding="utf-8").replace("{schema}", schema)
    messages: list = [{"role": "user", "content": question}]
    result = AskResult(answer="")
    executed = 0
    for _ in range(max_tool_calls + 1):  # every turn before the answer runs at least one tool
        turn = llm.tool_turn(system, messages, TOOLS, max_tokens=4000, purpose="ask")
        result.cost_usd += turn.cost_usd
        messages.append(turn.assistant_message)
        if turn.done:
            result.answer = turn.text.strip()
            return result
        if executed >= max_tool_calls:
            break  # it was told the limit is reached and still asked for tools
        tool_results = []
        for call in turn.tool_calls:
            if executed >= max_tool_calls:
                content: dict | str = {"error": f"The limit of {max_tool_calls} tool calls is reached. Answer now with what you have."}
                body, is_error = _tool_result(content)
                tool_results.append({"type": "tool_result", "tool_use_id": call.id, "content": body, "is_error": True})
                continue
            executed += 1
            if call.name == "run_sql":
                content = run_sql(db_path, str(call.input.get("query", "")))
            elif call.name == "describe_schema":
                content = schema
            elif call.name == "get_round":
                content = get_round(engine, str(call.input.get("match_id", "")), int(call.input.get("round", 0)))
            else:
                content = {"error": f"Unknown tool {call.name}."}
            body, is_error = _tool_result(content)
            result.tool_calls.append({"name": call.name, "input": call.input, "error": is_error})
            tool_results.append({"type": "tool_result", "tool_use_id": call.id, "content": body, "is_error": is_error})
        if executed >= max_tool_calls:
            tool_results.append({"type": "text", "text": f"That was the last of {max_tool_calls} tool calls. Answer now."})
        # all results of one turn go back in a single user message
        messages.append({"role": "user", "content": tool_results})
    result.answer = "I could not finish within the tool-call limit. Try a narrower question."
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ask a question about our matches.")
    ap.add_argument("question", help='for example: "Who has the most untraded deaths on Mirage?"')
    ap.add_argument("--show-tools", action="store_true", help="print the tool calls the model made")
    ap.add_argument("--config", default="config.toml")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = load_config(args.config)
    try:
        engine = get_engine(cfg.db_path)
        llm = LLM.from_config(cfg.llm, engine, provider="anthropic")
        result = ask(args.question, engine, cfg.db_path, llm)
    except SchemaMismatchError as exc:
        print(exc)
        return 1
    except LLMError as exc:
        print(f"FAILED: {exc}")
        return 1
    if args.show_tools:
        for call in result.tool_calls:
            print(f"[{call['name']}] {json.dumps(call['input'], ensure_ascii=False)}" + ("  -> error" if call["error"] else ""))
        print()
    print(result.answer)
    print(f"\n({len(result.tool_calls)} tool call(s), ${result.cost_usd:.4f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
