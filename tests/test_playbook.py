"""Unit tests for Phase 7: the playbook. No demo needed.

The synthetic database (tests/fixtures.py) gets one match marked as a pro
demo, two of its rounds labelled as a B execute with two smokes, and the
grenades of those smokes. The playbook must turn them into one card.

Run with:  python tests/test_playbook.py     (or: python -m pytest)
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from cs2review.config import Config
from cs2review.playbook import (
    build_playbook, card_id, difficulty, format_card, lineup_command, load_statuses, set_status, signature_of,
    teams_that_beat_us, throw_kind,
)
from fixtures import build_db

REPO = Path(__file__).resolve().parents[1]


def test_throw_kinds_lineups_and_difficulty():
    assert throw_kind(True, 0) == "jump" and throw_kind(False, 245.0) == "running" and throw_kind(False, 3.0) == "standing"
    assert lineup_command(-1162.04, -870.5, -160.0, -12.345, 88.1) == "setpos -1162.0 -870.5 -160.0; setang -12.35 88.10 0"
    assert lineup_command(None, 1, 2, 3, 4) is None
    steps = [{"throw": "standing"}, {"throw": "jump"}, {"throw": "running"}]
    assert difficulty(steps, [20.0, 25.0, 30.0]) == 5  # 1 + 2 + 2
    assert difficulty(steps, [20.0, 21.5, 30.0]) == 6  # two land within 2 s: +1
    assert difficulty([], []) == 0


def test_signature_and_id_are_stable():
    sig = signature_of("de_mirage", "A", ["Jungle", "Stairs"])
    assert sig == "de_mirage|T|A|Jungle,Stairs"
    assert card_id(sig) == card_id("de_mirage|T|A|Jungle,Stairs") and card_id(sig).startswith("pb_")
    assert card_id(sig) != card_id(signature_of("de_mirage", "B", ["Jungle", "Stairs"]))


class Env:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "maps").mkdir()
        shutil.copy(REPO / "maps" / "de_mirage.toml", root / "maps" / "de_mirage.toml")
        self.cfg = Config(db_path=root / "t.sqlite", maps_dir=root / "maps",
                          manual_labels_path=root / "labels.json", playbook_status_path=root / "status.json")
        self.engine = build_db(self.cfg.db_path)
        features = {"zones_at": {}, "first_contact_s": None, "length_s": 60, "entry_areas": [], "entry_zones": [],
                    "plant": None, "site_hit": {"site": "B", "t": 30},
                    "waves": [{"zone": "B", "t": 20, "end": 22, "smokes": 2, "molotovs": 0, "flashes": 0, "hes": 0}]}
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE matches SET source_kind = 'pro', us_team = NULL, team_a_name = 'Pro Five' WHERE match_id = 'm2'"))
            for rnd in (1, 3, 4):  # round 3 was lost (rounds divisible by 3 are lost in the fixtures)
                conn.execute(text(
                    "INSERT INTO round_plays (match_id, round, team, side, label, site, confidence, features, label_source) "
                    "VALUES ('m2', :r, 'A', 'T', 'execute', 'B', 0.8, :f, 'rule')"), {"r": rnd, "f": json.dumps(features)})
                for i, (who, place, jump) in enumerate((("a4", "Apartments", 0), ("a5", "BackAlley", 1))):
                    conn.execute(text(
                        "INSERT INTO grenades (match_id, round, type, thrower_steamid, thrower_side, throw_tick, throw_time_s, "
                        "throw_x, throw_y, throw_z, throw_pitch, throw_yaw, throw_place, throw_speed, throw_in_air, det_time_s, land_place) "
                        "VALUES ('m2', :r, 'smoke', :who, 'T', :tick, :t, -100.0, 200.0, -160.0, -10.0, 90.0, 'TSpawn', 0, :jump, :det, :place)"),
                        {"r": rnd, "who": who, "tick": rnd * 10_000 + 17 * 64, "t": 17.0 + i, "jump": jump, "det": 20.0 + 2 * i,
                         "place": place})
        return self

    def __exit__(self, *exc):
        self.engine.dispose()
        self.tmp.cleanup()


def test_playbook_card_from_pro_rounds():
    with Env() as env:
        table = build_playbook(env.engine, env.cfg)
        assert len(table) == 1
        row = table.iloc[0].to_dict()
        assert row["signature"] == "de_mirage|T|B|Apartments,BackAlley" and row["id"] == card_id(row["signature"])
        assert (row["source_kind"], row["seen"], row["won"], row["status"], row["site"]) == ("pro", 3, 2, "new", "B")
        assert row["difficulty"] == 4  # standing smoke 1 + jump smoke 2 + landing 2 s apart 1
        card = json.loads(row["card"])
        assert card["title"] == "B execute: utility on Apartments, BackAlley" and card["lineup_verified"] is False
        assert card["steps"] == [
            {"role": "P1", "t": 17, "do": "smoke", "from": "TSpawn", "to": "Apartments",
             "lineup": "setpos -100.0 200.0 -160.0; setang -10.00 90.00 0", "throw": "standing"},
            {"role": "P2", "t": 18, "do": "smoke", "from": "TSpawn", "to": "BackAlley",
             "lineup": "setpos -100.0 200.0 -160.0; setang -10.00 90.00 0", "throw": "jump"},
        ]
        assert [(e["round"], e["team"], e["command"]) for e in card["examples"]] == [
            (4, "Pro Five", f"playdemo m2.dem; demo_gototick {40_000 + 15 * 64}"),
            (1, "Pro Five", f"playdemo m2.dem; demo_gototick {10_000 + 15 * 64}"),
        ]
        assert card["evidence"] == {"seen": 3, "won": 2, "window_days": 0}
        text_ = format_card(row)
        assert "smoke from TSpawn to BackAlley (jump throw)" in text_ and "not verified" in text_


def test_status_survives_a_rebuild_and_stale_cards():
    with Env() as env:
        table = build_playbook(env.engine, env.cfg)
        pid = table.iloc[0]["id"]
        set_status(env.engine, env.cfg, pid, "trying")
        assert load_statuses(env.cfg.playbook_status_path) == {pid: "trying"}
        assert build_playbook(env.engine, env.cfg).iloc[0]["status"] == "trying"
        for bad_id, bad_status in ((pid, "maybe"), ("pb_nothere", "trying")):
            try:
                set_status(env.engine, env.cfg, bad_id, bad_status)
            except ValueError:
                pass
            else:
                raise AssertionError("bad id or status must be refused")
        # the map changed after the plays were seen: the card is stale
        zone_file = env.cfg.maps_dir / "de_mirage.toml"
        zone_file.write_text(zone_file.read_text().replace('last_map_update = ""', 'last_map_update = "2026-12-01"'))
        assert bool(build_playbook(env.engine, env.cfg).iloc[0]["stale"])


def test_our_own_rounds_never_become_cards_and_teams_that_beat_us():
    with Env() as env:
        with env.engine.begin() as conn:
            conn.execute(text("UPDATE round_plays SET match_id = 'm1' WHERE match_id = 'm2'"))  # m1 is our own match, team A is us
        assert build_playbook(env.engine, env.cfg).empty
        assert teams_that_beat_us(env.engine).empty  # we won every synthetic match
        with env.engine.begin() as conn:
            conn.execute(text("UPDATE matches SET score_a = 5, score_b = 13, team_b_name = 'Rivals' WHERE match_id = 'm3'"))
        lost = teams_that_beat_us(env.engine)
        assert list(lost["match_id"]) == ["m3"] and lost.iloc[0]["opponent"] == "Rivals"
        assert (int(lost.iloc[0]["us"]), int(lost.iloc[0]["them"])) == (5, 13)


if __name__ == "__main__":
    tests = [f for name, f in sorted(globals().items()) if name.startswith("test_") and callable(f)]
    for test in tests:
        test()
        print("ok  ", test.__name__)
    print(f"\n{len(tests)} tests passed")
