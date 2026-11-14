"""Unit tests for Phase 5: trends, the trend report and the question agent.

Uses the synthetic database from tests/fixtures.py (no demo) and a fake
model (no network). See fixtures.py for what the made-up matches contain.

Run with:  python tests/test_trends_ask.py     (or: python -m pytest)
"""

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixtures import build_db

import cs2review.ask as ask_module
from cs2review.ask import MAX_ROWS, ask, check_query, QueryRefused, run_sql
from cs2review.config import LLMSettings
from cs2review.db import get_engine
from cs2review.llm import LLM, FakeProvider, LLMError, ToolCall, ToolTurn
from cs2review.trend_report import evidence_text, resolve, validate_trend_report, write_trend_report
from cs2review.trends import compute_trends, format_trends, pick_matches, rate, rate_text


_TEMPLATE = {}


class DB:
    """A temporary copy of the synthetic database for one test (use with `with`).

    The database is built once per test run and copied, which is much faster.
    """

    def __enter__(self):
        if "path" not in _TEMPLATE:
            _TEMPLATE["dir"] = tempfile.TemporaryDirectory()
            path = Path(_TEMPLATE["dir"].name) / "template.sqlite"
            build_db(path).dispose()
            _TEMPLATE["path"] = path
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.sqlite"
        shutil.copy(_TEMPLATE["path"], self.path)
        self.engine = get_engine(self.path)
        return self

    def __exit__(self, *exc):
        self.engine.dispose()
        self.tmp.cleanup()


# --------------------------------------------------------------------------
# trends
# --------------------------------------------------------------------------

def test_rate_honesty_rule():
    assert rate(5, 7) == {"won": 5, "n": 7, "pct": None}
    assert rate(6, 8) == {"won": 6, "n": 8, "pct": 75}
    assert rate_text(rate(5, 7)) == "not enough data (n=7)"
    assert rate_text(rate(6, 8)) == "75% (6/8)"
    assert rate_text(rate(0, 0)) == "no rounds"


def test_trends_numbers():
    with DB() as db:
        t = compute_trends(db.engine, "de_mirage")
        assert t["filter"] == {"map": "de_mirage", "last": None, "matches": 2}
        s = t["maps"]["de_mirage"]["sides"]["T"]
        assert s["rounds"] == {"won": 14, "n": 20, "pct": 70}
        assert s["by_buy"]["full"] == {"won": 12, "n": 18, "pct": 67}
        assert s["by_buy"]["pistol"]["pct"] is None  # 2 rounds only
        assert s["opening_duels"] == {"won": 10, "n": 20, "pct": 50}
        assert s["opening_deaths_at"] == {"Jungle": 10}
        assert s["trades"] == {"won": 4, "n": 10, "pct": 40}
        assert s["after_5v4_up"] == {"won": 6, "n": 10, "pct": 60}
        assert s["after_4v5_down"] == {"won": 8, "n": 10, "pct": 80}
        assert s["pistol_conversion"] == {"won": 2, "n": 4, "pct": None}
        assert s["postplant"] == {"B": {"won": 6, "n": 6, "pct": None}}
        assert s["flags"]["won_opening"] == 10 and s["flags"]["saved"] == 6
        assert "CT" not in t["maps"]["de_mirage"]["sides"]
        ann = next(p for p in t["maps"]["de_mirage"]["players"] if p["name"] == "Ann")
        assert (ann["rounds"], ann["opening_kills"], ann["opening_deaths"], ann["untraded_deaths"]) == (20, 10, 10, 6)
        assert ann["kast"] == {"won": 14, "n": 20, "pct": 70}


def test_trends_filters():
    with DB() as db:
        assert pick_matches(db.engine) == ["m1", "m2", "m3"]
        assert pick_matches(db.engine, last=2) == ["m2", "m3"]
        assert pick_matches(db.engine, "de_mirage", last=1) == ["m2"]
        one = compute_trends(db.engine, "de_mirage", last=1)
        assert one["maps"]["de_mirage"]["sides"]["T"]["rounds"] == {"won": 7, "n": 10, "pct": 70}
        assert set(compute_trends(db.engine)["maps"]) == {"de_inferno", "de_mirage"}
        assert compute_trends(db.engine, "de_nuke")["maps"] == {}


def test_trends_text():
    with DB() as db:
        out = format_trends(compute_trends(db.engine))
        assert "=== de_inferno (1 match(es)) ===" in out and "round win rate        not enough data (n=4)" in out
        assert "round win rate        70% (14/20)" in out
        assert "No matches of your team yet" in format_trends(compute_trends(db.engine, "de_nuke"))


# --------------------------------------------------------------------------
# the trend report
# --------------------------------------------------------------------------

def good_trend_answer(**changes) -> dict:
    answer = {
        "headline": "We convert openings into rounds.",
        "strengths": [
            {"title": "Retaking 4v5", "why": "We win rounds after losing the opening.",
             "evidence": ["de_mirage.T.after_4v5_down"]},
            {"title": "Ann's KAST", "why": "Ann is involved in most rounds.", "evidence": ["de_mirage.players.Ann.kast"]},
        ],
        "weaknesses": [
            {"title": "Trades", "why": "Few deaths are traded.", "fix": "Swing in pairs.", "evidence": ["de_mirage.T.trades"]},
            {"title": "Opening duels", "why": "We lose half of them.", "fix": "Use flashes to peek.",
             "evidence": ["de_mirage.T.opening_duels"]},
            {"title": "Full buys", "why": "Full buys win too rarely.", "fix": "Play for the plant.",
             "evidence": ["de_mirage.T.by_buy.full", "de_mirage.T.flags.full_buy_no_plant"]},
        ],
        "focus": "Trade the first death.",
    }
    answer.update(changes)
    return answer


def test_trend_evidence_paths():
    with DB() as db:
        t = compute_trends(db.engine)
        assert resolve(t, "de_mirage.T.opening_duels") == {"won": 10, "n": 20, "pct": 50}
        assert resolve(t, "de_mirage.T.by_buy.full")["won"] == 12
        assert resolve(t, "de_mirage.players.Ann.opening_kills") == 10
        assert resolve(t, "de_mirage.CT.rounds") is None and resolve(t, "de_dust2.T.rounds") is None
        assert evidence_text(t, "de_mirage.T.trades") == "T trades: 40% (4/10)"
        assert evidence_text(t, "de_mirage.T.flags.saved") == "T flags saved: 6"


def test_trend_report_checks():
    with DB() as db:
        t = compute_trends(db.engine)
        assert validate_trend_report(json.dumps(good_trend_answer()), t)[1] == []
        bad = good_trend_answer()
        bad["weaknesses"][0]["evidence"] = ["de_mirage.T.pistol_conversion"]  # n=4
        bad["weaknesses"][1]["evidence"] = ["de_mirage.T.made_up"]
        bad["weaknesses"][2]["evidence"] = ["de_mirage.T.by_buy"]  # a group, not a rate
        bad["strengths"] = bad["strengths"][:1]
        problems = validate_trend_report(json.dumps(bad), t)[1]
        assert any('"strengths" must have exactly 2' in p for p in problems)
        assert any("too few rounds (n=4)" in p for p in problems)
        assert any("does not exist" in p for p in problems)
        assert any("is a group" in p for p in problems)


def test_trend_report_written_with_numbers_from_code():
    with DB() as db:
        t = compute_trends(db.engine, "de_mirage")
        llm = LLM(FakeProvider([json.dumps(good_trend_answer())]), LLMSettings(), db.engine)
        row = write_trend_report(db.engine, t, llm)
        md = row["report_md"]
        assert md.startswith("**Weekly trends (de_mirage, 2 match(es))**")
        assert "T trades: 40% (4/10)" in md and "players Ann kast: 70% (14/20)" in md
        assert "Fix: Swing in pairs." in md
        assert "do not write any number" in llm.provider.calls[0]["system"].lower()
        with db.engine.connect() as conn:
            from sqlalchemy import text
            assert conn.execute(text("SELECT kind, match_id FROM reports")).fetchall() == [("trends", "trends:de_mirage:all")]


def test_trend_report_needs_data():
    with DB() as db:
        llm = LLM(FakeProvider([]), LLMSettings(), db.engine)
        try:
            write_trend_report(db.engine, compute_trends(db.engine, "de_nuke"), llm)
        except LLMError as exc:
            assert "No trend data" in str(exc)
        else:
            raise AssertionError("no data, no report")


# --------------------------------------------------------------------------
# run_sql: read-only, one statement, a LIMIT and a time limit
# --------------------------------------------------------------------------

def test_destructive_queries_are_refused():
    with DB() as db:
        for query in ["DROP TABLE kills", "DELETE FROM kills", "UPDATE kills SET round = 0",
                      "SELECT 1; DROP TABLE kills", "WITH x AS (SELECT 1) DELETE FROM kills",
                      "INSERT INTO meta VALUES ('a', 'b')", "ATTACH DATABASE 'x.db' AS x", "PRAGMA writable_schema = 1"]:
            result = run_sql(db.path, query)
            assert "error" in result, query
        with sqlite3.connect(db.path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM kills").fetchone()[0] > 0  # nothing was deleted


def test_the_connection_itself_is_read_only():
    # even if the word check let a write through, SQLite would refuse it
    with DB() as db:
        original = ask_module.check_query
        ask_module.check_query = lambda q: q  # switch the word check off for this test
        try:
            result = run_sql(db.path, "DELETE FROM kills")
        finally:
            ask_module.check_query = original
        assert "readonly" in result["error"] or "read-only" in result["error"]
        with sqlite3.connect(db.path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM kills").fetchone()[0] > 0


def test_limit_is_added_and_results_are_capped():
    assert check_query("SELECT * FROM kills;") == f"SELECT * FROM kills LIMIT {MAX_ROWS}"
    assert check_query("select * from kills limit 5") == "select * from kills limit 5"
    assert check_query("WITH a AS (SELECT 1) SELECT * FROM a").endswith(f"LIMIT {MAX_ROWS}")
    assert check_query("SELECT created_at FROM llm_calls").startswith("SELECT created_at")  # 'created' is not 'create'
    try:
        check_query("   ")
    except QueryRefused:
        pass
    else:
        raise AssertionError("an empty query must be refused")
    with DB() as db:
        result = run_sql(db.path, "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 500) SELECT x FROM n")
        assert result["row_count"] == MAX_ROWS and result["truncated"] is False  # the added LIMIT 200 stops it
        result = run_sql(db.path, "SELECT side, COUNT(*) AS n FROM our_rounds GROUP BY side")
        assert result == {"columns": ["side", "n"], "rows": [["T", 24]], "row_count": 1, "truncated": False}


def test_slow_query_is_stopped():
    with DB() as db:
        slow = ("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) "
                "SELECT COUNT(*) FROM n LIMIT 1")
        result = run_sql(db.path, slow, timeout_s=0.3)
        assert "longer than" in result["error"]


# --------------------------------------------------------------------------
# the agent loop
# --------------------------------------------------------------------------

def turn(text="", calls=()):
    calls = list(calls)
    return ToolTurn(text=text, tool_calls=calls, done=not calls,
                    assistant_message={"role": "assistant", "content": text or "(tool calls)"})


def test_agent_answers_from_sql():
    with DB() as db:
        script = [
            turn(calls=[ToolCall("c1", "run_sql", {"query": "SELECT ROUND(100.0 * AVG(won)) AS pct, COUNT(*) AS n FROM our_rounds WHERE map = 'de_mirage'"})]),
            turn("Our round win rate on Mirage is 70% (n=20)."),
        ]
        provider = FakeProvider(script)
        result = ask("What is our win rate on Mirage?", db.engine, db.path, LLM(provider, LLMSettings(), db.engine))
        assert result.answer == "Our round win rate on Mirage is 70% (n=20)."
        assert [(c["name"], c["error"]) for c in result.tool_calls] == [("run_sql", False)]
        second_request = provider.calls[1]["messages"]
        tool_result = second_request[-1]["content"][0]
        assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "c1"
        assert json.loads(tool_result["content"])["rows"] == [[70.0, 20]]
        assert "our_rounds" in provider.calls[0]["system"] and "not enough data" in provider.calls[0]["system"]
        assert [t["name"] for t in provider.calls[0]["tools"]] == ["run_sql", "describe_schema", "get_round"]


def test_agent_tools_get_round_and_schema_and_errors():
    with DB() as db:
        script = [
            turn(calls=[ToolCall("c1", "get_round", {"match_id": "m1", "round": 2}),
                        ToolCall("c2", "describe_schema", {}),
                        ToolCall("c3", "run_sql", {"query": "DROP TABLE kills"})]),
            turn("Done."),
        ]
        provider = FakeProvider(script)
        result = ask("What happened in round 2?", db.engine, db.path, LLM(provider, LLMSettings(), db.engine))
        results = provider.calls[1]["messages"][-1]["content"]
        assert len(results) == 3  # all results of one turn in one message
        assert json.loads(results[0]["content"])["round"] == 2 and not results[0]["is_error"]
        assert "our_player_rounds" in results[1]["content"]
        assert results[2]["is_error"] and [c["error"] for c in result.tool_calls] == [False, False, True]


def test_agent_stops_after_eight_tool_calls():
    with DB() as db:
        def endless(**request):
            n = len(request["messages"])
            if n > 30:
                return turn("I have to stop here.")
            return turn(calls=[ToolCall(f"c{n}", "run_sql", {"query": "SELECT 1"})])

        provider = FakeProvider(endless)
        result = ask("Loop forever", db.engine, db.path, LLM(provider, LLMSettings(), db.engine), max_tool_calls=8)
        assert len(result.tool_calls) == 8 and not any(c["error"] for c in result.tool_calls)
        assert "last of 8 tool calls" in json.dumps(provider.calls[-1]["messages"][-1]["content"])
        assert len(provider.calls) == 9  # 8 turns with a tool call, then one last chance to answer
        assert "could not finish" in result.answer.lower()


def test_agent_answers_after_the_limit_message():
    with DB() as db:
        def until_told(**request):
            last = json.dumps(request["messages"][-1]["content"], default=str)
            if "last of 3 tool calls" in last:
                return turn("Best answer with what I have.")
            return turn(calls=[ToolCall(f"c{len(request['messages'])}", "run_sql", {"query": "SELECT 1"})])

        result = ask("q", db.engine, db.path, LLM(FakeProvider(until_told), LLMSettings(), db.engine), max_tool_calls=3)
        assert result.answer == "Best answer with what I have." and len(result.tool_calls) == 3


def test_manual_provider_cannot_run_the_agent():
    with DB() as db:
        llm = LLM.from_config(LLMSettings(provider="manual", out_dir=Path(db.tmp.name)), db.engine)
        try:
            ask("anything", db.engine, db.path, llm)
        except LLMError as exc:
            assert "cannot use tools" in str(exc)
        else:
            raise AssertionError("manual mode has no tools")


# --------------------------------------------------------------------------
# the evaluation questions
# --------------------------------------------------------------------------

def test_eval_questions_have_working_sql():
    from eval_ask import answer_matches, expected_value, load_questions
    questions = load_questions()
    assert len(questions) == 10 and len({q["id"] for q in questions}) == 10
    with DB() as db:
        got = {q["id"]: expected_value(db.path, q["sql"]) for q in questions}
    assert got["t_win_rate"] == 71.0  # 17 of 24
    assert got["rounds_played"] == 24 and got["opening_duels_won"] == 12
    assert got["most_untraded"] == "Ann" and got["most_opening_kills"] == "Ann"
    assert got["postplant_wins"] == 7 and got["full_buy_win_rate"] == 67.0  # 14 of 21
    assert got["team_flash_rounds"] == 0 and got["trade_rate"] == 42.0  # 5 of 12


def test_eval_answer_matching():
    from eval_ask import answer_matches
    assert answer_matches("Our T-side win rate is 71% (17 of 24 rounds).", "number", 71.0)
    assert answer_matches("About 70%.", "number", 71.0)  # percentages may be off by one
    assert not answer_matches("About 60%.", "number", 71.0)
    assert answer_matches("We played 24 rounds.", "number", 24)
    assert not answer_matches("We played 25 rounds.", "number", 24)
    assert answer_matches("Ann has the most.", "name", "Ann")
    assert answer_matches("There is not enough data.", "number", None)


if __name__ == "__main__":
    tests = [f for name, f in sorted(globals().items()) if name.startswith("test_") and callable(f)]
    for test in tests:
        test()
        print("ok  ", test.__name__)
    print(f"\n{len(tests)} tests passed")
