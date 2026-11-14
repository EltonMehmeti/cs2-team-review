"""Unit tests for Phase 9: weakness tags, suggestions and the feedback loop.

Uses the synthetic database (tests/fixtures.py) plus hand-made playbook
cards. No demo, no network.

Run with:  python tests/test_suggest.py     (or: python -m pytest)
"""

import json
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from cs2review.config import Config
from cs2review.playbook import card_id, load_statuses, set_status, signature_of
from cs2review.suggest import feedback, find_weaknesses, load_tags, main, pick_suggestions, render, score_card
from cs2review.trends import compute_trends, format_trends
from fixtures import build_db

REPO = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 14)


def card_row(pid, site="B", side="T", seen=5, won=4, difficulty=3, status="new", stale=0, source="pro", needs_review=0,
             last_seen="2026-11-01T00:00:00+00:00", map_name="de_mirage", signature=None, title=None):
    card = {"title": title or f"{site} card {pid}", "label": "execute",
            "steps": [{"role": "P1", "t": 17, "do": "smoke", "from": "TSpawn", "to": "Apartments",
                       "lineup": "setpos 1.0 2.0 3.0; setang 4.00 5.00 0", "throw": "standing"}],
            "examples": [{"match_id": "m2", "round": 1, "team": "Pros", "command": "playdemo m2.dem; demo_gototick 11000"}],
            "evidence": {"seen": seen, "won": won, "window_days": 0}, "lineup_verified": False}
    return {"id": pid, "signature": signature or f"sig-{pid}", "map": map_name, "side": side, "site": site,
            "title": card["title"], "source_kind": source, "seen": seen, "won": won, "first_seen": last_seen,
            "last_seen": last_seen, "difficulty": difficulty, "status": status, "stale": stale,
            "needs_review": needs_review, "card": json.dumps(card)}


class Env:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "maps").mkdir()
        shutil.copy(REPO / "maps" / "de_mirage.toml", root / "maps" / "de_mirage.toml")
        self.cfg = Config(db_path=root / "t.sqlite", maps_dir=root / "maps", playbook_status_path=root / "status.json",
                          manual_labels_path=root / "labels.json")
        self.engine = build_db(self.cfg.db_path)
        self.config_file = root / "config.toml"
        self.config_file.write_text(
            f'db_path = "{self.cfg.db_path.as_posix()}"\nmaps_dir = "{self.cfg.maps_dir.as_posix()}"\n'
            f'playbook_status_path = "{self.cfg.playbook_status_path.as_posix()}"\n'
            f'manual_labels_path = "{self.cfg.manual_labels_path.as_posix()}"\n', encoding="utf-8")
        return self

    def add_cards(self, *rows):
        with self.engine.begin() as conn:
            for row in rows:
                cols = ", ".join(row)
                conn.execute(text(f"INSERT INTO playbook ({cols}) VALUES ({', '.join(':' + c for c in row)})"), row)

    def __exit__(self, *exc):
        self.engine.dispose()
        self.tmp.cleanup()


def test_every_tag_query_runs():
    tags = load_tags()
    assert {"a_hits_failing", "b_hits_failing", "openings_lost_mid", "retake_b_weak", "force_buys_losing"} <= set(tags)
    with Env() as env:
        found = find_weaknesses(env.engine, env.cfg)
        assert found == []  # the synthetic team wins 70% and dies in Jungle: no weakness with n >= 8


def test_openings_lost_mid_uses_the_zone_files():
    with Env() as env:
        with env.engine.begin() as conn:
            conn.execute(text("UPDATE kills SET victim_place = 'Middle' WHERE victim_steamid = 'a1' AND is_opening = 1"))
        found = find_weaknesses(env.engine, env.cfg)
        mid = [w for w in found if w["tag"] == "openings_lost_mid"]
        assert mid == [{"tag": "openings_lost_mid", "description": load_tags()["openings_lost_mid"]["description"],
                        "side": "T", "site": None, "map": "de_mirage", "n": 10, "rate": 100}]
        with env.engine.connect() as conn:
            assert conn.execute(text("SELECT zone FROM zone_areas WHERE map = 'de_mirage' AND area = 'Middle'")).scalar_one() == "Mid"


def test_scoring():
    tag = {"site": "B"}
    score, parts = score_card(card_row("x", site="B", seen=5, won=4, difficulty=3), tag, TODAY)
    assert parts == {"fit": 3.0, "evidence": 1.6, "ease": 0.7, "freshness": 1.0} and score == 6.3
    _, parts = score_card(card_row("x", site="A", seen=2, won=2, difficulty=8, last_seen="2026-06-01"), tag, TODAY)
    assert parts == {"fit": 1.8, "evidence": 0.0, "ease": 0.2, "freshness": 0.2}  # seen < 3: no evidence points
    _, parts = score_card(card_row("x", last_seen=None), None, TODAY)
    assert parts["fit"] == 1.0 and parts["freshness"] == 0.3


TAG_B = {"b_test": {"description": "Test: our B rounds", "side": "T", "site": "B",
                    "sql": "SELECT map, COUNT(*) AS n, 10 AS rate FROM our_rounds WHERE side = 'T' GROUP BY map HAVING COUNT(*) >= 8"}}


def test_picks_follow_the_weakness_and_skip_unusable_cards():
    with Env() as env:
        env.add_cards(
            card_row("pb_best", site="B", seen=6, won=5, difficulty=2),
            card_row("pb_ok", site="B", seen=4, won=2, difficulty=5),
            card_row("pb_other_site", site="A", seen=9, won=9, difficulty=1),
            card_row("pb_stale", site="B", stale=1),
            card_row("pb_dropped", site="B", status="dropped"),
            card_row("pb_trying", site="B", status="trying"),
            card_row("pb_web_unreviewed", site="B", source="web", needs_review=1, seen=0, won=0),
            card_row("pb_web_reviewed", site="B", source="web", needs_review=0, seen=0, won=0, difficulty=0),
            card_row("pb_ct", site="B", side="CT"),
            card_row("pb_inferno", site="B", map_name="de_inferno"),
        )
        picks = pick_suggestions(env.engine, env.cfg, today=TODAY, tags=TAG_B)
        assert [p["card"]["id"] for p in picks] == ["pb_best", "pb_ok", "pb_web_reviewed"]
        assert all(p["tag"]["tag"] == "b_test" for p in picks)
        text_ = render(picks, TODAY)
        assert text_.startswith("**Three plays to try this week** (2026-11-14)")
        assert "Why: Test: our B rounds on de_mirage (10% over 20 rounds). This play won 5 of 6 times we saw it." in text_
        assert "`setpos 1.0 2.0 3.0; setang 4.00 5.00 0` (standing throw)" in text_
        assert "Watch: `playdemo m2.dem; demo_gototick 11000`" in text_


def test_fallback_when_no_weakness_has_data():
    with Env() as env:
        env.add_cards(card_row("pb_a", site="A", seen=9, won=8), card_row("pb_b", site="B", seen=3, won=1),
                      card_row("pb_inferno", map_name="de_inferno", seen=9, won=9))
        picks = pick_suggestions(env.engine, env.cfg, today=TODAY, tags={})
        # scores without a tag: fit 1 + evidence + ease 0.7 + freshness 1
        assert [p["card"]["id"] for p in picks] == ["pb_inferno", "pb_a", "pb_b"]  # 4.7, 4.48, 3.37
        assert all(p["tag"] is None for p in picks)
        assert "No weakness has enough rounds yet" in render(picks, TODAY)
        assert "No suggestions this week" in render([], TODAY)


def test_command_marks_cards_suggested_and_stores_the_post():
    with Env() as env:
        env.add_cards(card_row("pb_1", site="A", seen=5, won=4), card_row("pb_2", site="B", status="suggested"))
        assert main(["--config", str(env.config_file), "--dry-run"]) == 0
        assert load_statuses(env.cfg.playbook_status_path) == {}
        assert main(["--config", str(env.config_file)]) == 0
        assert load_statuses(env.cfg.playbook_status_path) == {"pb_1": "suggested"}
        with env.engine.connect() as conn:
            kind, md = conn.execute(text("SELECT kind, report_md FROM reports")).fetchone()
        assert kind == "suggestions" and "pb_1" in md and "pb_2" in md


def test_feedback_counts_our_runs_since_the_status_was_set():
    with Env() as env:
        signature = signature_of("de_mirage", "B", ["Apartments", "BackAlley"])
        pid = card_id(signature)
        env.add_cards(card_row(pid, site="B", signature=signature, title="B exec Apps + Alley"),
                      card_row("pb_named", site="A", title="A split"))
        set_status(env.engine, env.cfg, pid, "trying", today="2026-11-05")
        set_status(env.engine, env.cfg, "pb_named", "adopted", today="2026-10-01")
        features = {"zones_at": {}, "length_s": 60, "entry_areas": [], "entry_zones": [], "plant": None,
                    "site_hit": {"site": "B", "t": 30},
                    "waves": [{"zone": "B", "t": 20, "end": 22, "smokes": 2, "molotovs": 0, "flashes": 0, "hes": 0}]}
        with env.engine.begin() as conn:
            # we ran the B execute in m1 round 1 (before the card was set to trying: does not count)
            # and in m2 rounds 1 and 3 (after: counts; round 3 was lost)
            for match, rnd in (("m1", 1), ("m2", 1), ("m2", 3)):
                conn.execute(text(
                    "INSERT INTO round_plays (match_id, round, team, side, label, site, confidence, features, label_source) "
                    "VALUES (:m, :r, 'A', 'T', 'execute', 'B', 0.8, :f, 'rule')"), {"m": match, "r": rnd, "f": json.dumps(features)})
                for i, place in enumerate(("Apartments", "BackAlley")):
                    conn.execute(text(
                        "INSERT INTO grenades (match_id, round, type, thrower_steamid, thrower_side, det_time_s, land_place) "
                        "VALUES (:m, :r, 'smoke', 'a4', 'T', :d, :p)"), {"m": match, "r": rnd, "d": 20.0 + i, "p": place})
            # the IGL named rounds after the A card in `plays review`: one on Mirage, one on Inferno
            for match in ("m2", "m3"):
                conn.execute(text("INSERT INTO round_plays (match_id, round, team, side, label, site, confidence, features, "
                                  "name, label_source) VALUES (:m, 2, 'A', 'T', 'split', 'A', 1.0, '{}', 'A split', 'manual')"),
                             {"m": match})
        rows = {f["id"]: f for f in feedback(env.engine, env.cfg)}
        assert rows[pid]["runs"] == {"won": 1, "n": 2, "pct": None} and rows[pid]["since"] == "2026-11-05"
        assert rows[pid]["rounds"] == ["m2 r1", "m2 r3"]
        # the Mirage round counts (round 2 was won); the Inferno one does not: the card is for Mirage
        assert rows["pb_named"]["runs"] == {"won": 1, "n": 1, "pct": None} and rows["pb_named"]["rounds"] == ["m2 r2"]
        assert feedback(env.engine, env.cfg, "de_inferno") == []

        trends = compute_trends(env.engine, "de_mirage", cfg=env.cfg)
        trying = {t["id"]: t for t in trends["maps"]["de_mirage"]["trying"]}
        assert trying[pid]["runs"]["n"] == 2
        assert "plays we are trying (from the playbook):" in format_trends(trends)


if __name__ == "__main__":
    tests = [f for name, f in sorted(globals().items()) if name.startswith("test_") and callable(f)]
    for test in tests:
        test()
        print("ok  ", test.__name__)
    print(f"\n{len(tests)} tests passed")
