"""Unit tests for Phase 4: llm.py, report.py, notify.py. No demo, no network.

The LLM is a fake provider that returns canned JSON (PLAN.md section 8:
real model calls are never part of the test suite).

Run with:  python tests/test_report.py     (or: python -m pytest)
"""

import io
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from cs2review.config import LLMSettings, load_config
from cs2review.db import get_engine
from cs2review.llm import LLM, BudgetExceeded, FakeProvider, LLMError, ManualAnswerNeeded, extract_json
from cs2review.notify import post_to_discord, split_message
from cs2review.report import ReportInvalid, rewatch_tick, validate_report, write_report

LENGTHS = {1: 113, 2: 71, 3: 70, 4: 45}
FREEZE = {1: 1761, 2: 10699, 3: 16960, 4: 23185}


def good_report(**changes) -> dict:
    report = {
        "worked": [
            {"title": "Opening duels", "rounds": [1, 3], "why": "We got the first kill."},
            {"title": "Post-plants", "rounds": [1, 4], "why": "We held the bomb."},
            {"title": "Utility", "rounds": [3], "why": "Smokes cut Catwalk."},
        ],
        "problems": [
            {"title": "Untraded deaths", "rounds": [2, 4], "why": "Nobody traded.", "fix": "Swing in pairs."},
            {"title": "Team flashes", "rounds": [1, 3], "why": "We blinded each other.", "fix": "Call flashes."},
            {"title": "Late rotations", "rounds": [2, 3], "why": "Slow to rotate.", "fix": "Rotate on contact."},
        ],
        "rewatch": [
            {"round": 2, "t": 21, "why": "Opening death in Middle."},
            {"round": 2, "t": 58, "why": "The lost post-plant."},
            {"round": 3, "t": 38, "why": "The opening kill."},
            {"round": 4, "t": 18, "why": "Fast B hit."},
            {"round": 1, "t": 3, "why": "Pistol setup."},
        ],
        "practice": "Trade each other's entries within two seconds.",
    }
    report.update(changes)
    return report


def make_db(tmp: str):
    """A database with one hand-made match: a digest, four rounds and the match row."""
    engine = get_engine(Path(tmp) / "t.sqlite")
    digest = {
        "match": {"match_id": "m1", "map": "de_mirage", "us": "Our Five", "them": "team B",
                  "final_score": "3-1", "won": True, "halves": [{"rounds": "1-4", "side": "T"}], "rounds_played": 4},
        "players": [], "team": {},
        "rounds": [{"round": r, "result": {"won": True, "how": "ct_eliminated", "length_s": n, "alive": "1v0"}}
                   for r, n in LENGTHS.items()],
    }
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO matches (match_id, file_name, map, tick_rate, rounds_total, rounds_played, score_a, score_b) "
            "VALUES ('m1', 'm1.dem', 'de_mirage', 64, 4, 4, 3, 1)"))
        for r, f in FREEZE.items():
            conn.execute(text(
                "INSERT INTO rounds (match_id, round, start_tick, freeze_end_tick, end_tick, window_end_tick, played, "
                "score_a_before, score_b_before, bomb_planted) VALUES ('m1', :r, 0, :f, :f + 5000, :f + 6000, 1, 0, 0, 0)"),
                {"r": r, "f": f})
        conn.execute(text("INSERT INTO match_digests (match_id, json, token_estimate) VALUES ('m1', :j, 100)"),
                     {"j": json.dumps(digest)})
    return engine


def fake_llm(answers, engine=None, **settings) -> LLM:
    return LLM(FakeProvider(answers), LLMSettings(**settings), engine)


def calls_logged(engine) -> list[tuple]:
    with engine.connect() as conn:
        return list(conn.execute(text("SELECT purpose, provider, ok FROM llm_calls ORDER BY id")))


# --------------------------------------------------------------------------
# checking the model's answer
# --------------------------------------------------------------------------

def test_good_report_passes():
    report, problems = validate_report(json.dumps(good_report()), LENGTHS)
    assert problems == [] and report["practice"].startswith("Trade")


def test_answer_with_fences_and_chatter_is_read():
    text_ = "Here you go:\n```json\n" + json.dumps(good_report()) + "\n```\nGood luck!"
    assert validate_report(text_, LENGTHS)[1] == []
    assert extract_json('{"a": 1}') == '{"a": 1}'


def test_each_check_fails_when_it_should():
    def problems(**changes):
        return validate_report(json.dumps(good_report(**changes)), LENGTHS)[1]

    assert validate_report("no json here", LENGTHS)[1][0].startswith("the answer is not valid JSON")
    assert "required shape" in validate_report(json.dumps({"worked": []}), LENGTHS)[1][0]
    assert any('"worked" must have exactly 3' in p for p in problems(worked=good_report()["worked"][:2]))
    assert any('"rewatch" must have exactly 5' in p for p in problems(rewatch=good_report()["rewatch"][:4]))
    bad_round = good_report()["worked"]
    bad_round[0] = {"title": "x", "rounds": [1, 9], "why": "y"}
    assert any("not played: [9]" in p for p in problems(worked=bad_round))
    one_round = good_report()["problems"]
    one_round[1] = {"title": "Lonely", "rounds": [3, 3], "why": "y", "fix": "z"}
    assert any("at least 2 different rounds" in p for p in problems(problems=one_round))
    late = good_report()["rewatch"]
    late[0] = {"round": 4, "t": 46, "why": "after the end"}
    assert any("t=46 is outside round 4" in p for p in problems(rewatch=late))
    assert any('"practice" is empty' in p for p in problems(practice="  "))
    no_rounds = good_report()["worked"]
    no_rounds[2] = {"title": "x", "rounds": [], "why": "y"}
    assert any("cites no rounds" in p for p in problems(worked=no_rounds))


def test_rewatch_tick():
    assert rewatch_tick(10699, 21, 64) == 10699 + 16 * 64
    assert rewatch_tick(10699, 3, 64) == 10699  # never before the round went live


# --------------------------------------------------------------------------
# the whole flow with a fake model
# --------------------------------------------------------------------------

def test_report_is_written_checked_rendered_and_stored():
    with tempfile.TemporaryDirectory() as tmp:
        engine = make_db(tmp)
        llm = fake_llm([json.dumps(good_report())], engine)
        row = write_report(engine, "m1", llm)
        assert not row["retried"]
        md = row["report_md"]
        assert md.startswith("**Match review: de_mirage, we won 3-1** (Our Five vs team B, match m1)")
        assert "1. **Opening duels** (rounds 1, 3): We got the first kill." in md
        assert "   Fix: Swing in pairs." in md
        assert f"`playdemo m1.dem; demo_gototick {10699 + 16 * 64}`" in md
        assert "**Practice:** Trade each other's entries" in md
        request = llm.provider.calls[0]
        assert "never blame a single player" in request["system"].lower()
        assert '"rounds_played":4' in request["user"] and "1, 2, 3, 4" in request["user"]
        assert request["json_schema"]["required"] == ["worked", "problems", "rewatch", "practice"]
        with engine.connect() as conn:
            stored = conn.execute(text("SELECT match_id, provider, model, report_md FROM reports")).fetchall()
        assert len(stored) == 1 and stored[0][1] == "fake" and stored[0][3] == md
        assert calls_logged(engine) == [("report", "fake", 1)]
        engine.dispose()


def test_one_retry_with_the_problems_then_success():
    with tempfile.TemporaryDirectory() as tmp:
        engine = make_db(tmp)
        bad = good_report(rewatch=good_report()["rewatch"][:3])
        llm = fake_llm([json.dumps(bad), json.dumps(good_report())], engine)
        row = write_report(engine, "m1", llm)
        assert row["retried"]
        retry_prompt = llm.provider.calls[1]["user"]
        assert "It had these problems" in retry_prompt and '"rewatch" must have exactly 5 items, it has 3' in retry_prompt
        assert calls_logged(engine) == [("report", "fake", 1), ("report_retry", "fake", 1)]
        engine.dispose()


def test_fails_loudly_after_two_bad_answers():
    with tempfile.TemporaryDirectory() as tmp:
        engine = make_db(tmp)
        llm = fake_llm(["not json", json.dumps(good_report(practice=""))], engine)
        try:
            write_report(engine, "m1", llm)
        except ReportInvalid as exc:
            assert '"practice" is empty' in str(exc)
        else:
            raise AssertionError("two bad answers must fail")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM reports")).scalar_one() == 0
        engine.dispose()


def test_manual_provider_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        engine = make_db(tmp)
        settings = LLMSettings(provider="manual", out_dir=Path(tmp) / "out")
        llm = LLM.from_config(settings, engine)
        try:
            write_report(engine, "m1", llm)
        except ManualAnswerNeeded as exc:
            assert "answer_m1.json" in str(exc)
        else:
            raise AssertionError("the first manual run must stop and ask for the answer")
        prompt = (Path(tmp) / "out" / "prompt_m1.md").read_text(encoding="utf-8")
        assert "# Instructions" in prompt and "# Answer format" in prompt and '"practice"' in prompt

        # the owner pastes the prompt into a chat app and saves the answer (with fences, as chat apps do)
        (Path(tmp) / "out" / "answer_m1.json").write_text("```json\n" + json.dumps(good_report()) + "\n```", encoding="utf-8")
        row = write_report(engine, "m1", LLM.from_config(settings, engine))
        assert row["provider"] == "manual" and row["cost_usd"] == 0 and "**What worked**" in row["report_md"]
        engine.dispose()


def test_manual_provider_writes_a_retry_prompt_for_a_bad_answer():
    with tempfile.TemporaryDirectory() as tmp:
        engine = make_db(tmp)
        out = Path(tmp) / "out"
        out.mkdir()
        (out / "answer_m1.json").write_text(json.dumps(good_report(practice="")), encoding="utf-8")
        settings = LLMSettings(provider="manual", out_dir=out)
        try:
            write_report(engine, "m1", LLM.from_config(settings, engine))
        except ManualAnswerNeeded as exc:
            assert "answer_m1_retry.json" in str(exc)
        retry = (out / "prompt_m1_retry.md").read_text(encoding="utf-8")
        assert '"practice" is empty' in retry
        engine.dispose()


# --------------------------------------------------------------------------
# cost, budget, settings
# --------------------------------------------------------------------------

def test_cost_is_logged_and_the_cap_stops_a_call():
    provider = FakeProvider([json.dumps(good_report())] * 3)
    provider.name = "anthropic"  # pretend to be the paid provider so costs are counted
    llm = LLM(provider, LLMSettings(price_in_per_mtok=0.10, price_out_per_mtok=0.50, max_cost_usd_per_run=0.01))
    result = llm.complete("system", "user " * 400, max_tokens=4000)
    assert result.cost_usd == round(result.input_tokens * 0.10 / 1e6 + result.output_tokens * 0.50 / 1e6, 6)
    assert result.cost_usd > 0 and llm.spent_usd == result.cost_usd
    assert llm.calls[0]["cost_usd"] == result.cost_usd

    tight = LLM(provider, LLMSettings(max_cost_usd_per_run=0.0001))
    try:
        tight.complete("system", "user", max_tokens=4000)  # 4000 output tokens could cost $0.002
    except BudgetExceeded as exc:
        assert "max_cost_usd_per_run" in str(exc)
    else:
        raise AssertionError("the cap must stop the call")
    assert len(provider.calls) == 1  # the provider was never asked


def test_prompt_over_50k_tokens_is_refused():
    llm = fake_llm(["{}"], max_cost_usd_per_run=100)
    try:
        llm.complete("s", "x" * 210_000, max_tokens=10)
    except LLMError as exc:
        assert "50,000" in str(exc)
    else:
        raise AssertionError("a huge prompt must be refused")


def test_anthropic_provider_needs_a_key():
    saved = {k: os.environ.pop(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN") if k in os.environ}
    try:
        LLM.from_config(LLMSettings(provider="anthropic"))
    except LLMError as exc:
        assert "ANTHROPIC_API_KEY" in str(exc)
    else:
        raise AssertionError("no key, no provider")
    finally:
        os.environ.update(saved)


def test_llm_settings_from_config_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.toml"
        path.write_text('[llm]\nprovider = "manual"\nmax_cost_usd_per_run = 0.5\neffort = "low"\n', encoding="utf-8")
        cfg = load_config(path)
        assert cfg.llm.provider == "manual" and cfg.llm.max_cost_usd_per_run == 0.5 and cfg.llm.effort == "low"
        assert cfg.llm.model == "claude-haiku-5-5"
    assert load_config(Path("does-not-exist.toml")).llm.provider == "anthropic"


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------

def test_split_message_respects_the_limit():
    text_ = "\n".join(f"line {i} " + "x" * 50 for i in range(100))
    chunks = split_message(text_, limit=500)
    assert all(len(c) <= 500 for c in chunks)
    assert "".join(c + "\n" for c in chunks).replace("\n", "") == text_.replace("\n", "")
    assert split_message("a" * 1200, limit=500) == ["a" * 500, "a" * 500, "a" * 200]
    assert split_message("short") == ["short"]


def test_post_to_discord_sends_each_chunk_without_mentions():
    sent = []

    class Response(io.BytesIO):
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(request: urllib.request.Request, timeout=None):
        sent.append(json.loads(request.data))
        return Response()

    n = post_to_discord("hello\n" + "y" * 2500, webhook_url="https://discord.example/webhook", opener=opener)
    assert n == 3 and sent[0]["content"] == "hello"
    assert all(s["allowed_mentions"] == {"parse": []} for s in sent)

    saved = os.environ.pop("DISCORD_WEBHOOK_URL", None)
    try:
        post_to_discord("x", opener=opener)
    except RuntimeError as exc:
        assert "DISCORD_WEBHOOK_URL" in str(exc)
    finally:
        if saved is not None:
            os.environ["DISCORD_WEBHOOK_URL"] = saved


if __name__ == "__main__":
    tests = [f for name, f in sorted(globals().items()) if name.startswith("test_") and callable(f)]
    for test in tests:
        test()
        print("ok  ", test.__name__)
    print(f"\n{len(tests)} tests passed")
