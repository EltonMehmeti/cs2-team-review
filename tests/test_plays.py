"""Unit tests for Phase 6: zones, play features, labels, manual labels, mapview.

No demo needed. Positions and grenades are written by hand on de_mirage's
draft zones (maps/de_mirage.toml).

Run with:  python tests/test_plays.py     (or: python -m pytest)
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from cs2review.config import Config, PlayRules
from cs2review.plays import (
    build_plays, compute_plays, coverage, entry_routes, find_site_hit, label_ct, label_t, load_manual, play_table,
    review, round_features, set_manual_label, setup_string, utility_waves, zone_counts,
)
from cs2review.zones import MapZones, ensure_zones, load_zones, write_draft

REPO = Path(__file__).resolve().parents[1]
MIRAGE = load_zones("de_mirage", REPO / "maps")
RULES = PlayRules()


def pos(rows):
    """rows: (time_s, steamid, place) -> a positions DataFrame."""
    return pd.DataFrame(rows, columns=["time_s", "steamid", "place"])


def gren(rows):
    """rows: (det_time_s, type, land_place)."""
    return pd.DataFrame(rows, columns=["det_time_s", "type", "land_place"])


def features(**changes):
    """T-side features of a dull 90-second round: nothing happens."""
    f = {"zones_at": {"10": {"T": 5}, "20": {"Mid": 5}, "30": {"Mid": 5}, "45": {"Mid": 5}}, "first_contact_s": None,
         "waves": [], "length_s": 90, "site_hit": None, "entry_areas": [], "entry_zones": [], "plant": None}
    f.update(changes)
    return f


def wave(zone, t, end=None, smokes=0, molotovs=0, flashes=0, hes=0):
    return {"zone": zone, "t": t, "end": end if end is not None else t, "smokes": smokes, "molotovs": molotovs,
            "flashes": flashes, "hes": hes}


# --------------------------------------------------------------------------
# zones
# --------------------------------------------------------------------------

def test_mirage_draft_zones():
    assert MIRAGE is not None and not MIRAGE.reviewed
    assert MIRAGE.zone("Jungle") == "A" and MIRAGE.zone("Connector") == "Mid" and MIRAGE.zone("Nowhere") == "?"
    assert MIRAGE.site("BombsiteB") == "B" and MIRAGE.site("Apartments") is None
    assert MIRAGE.site_zone("A") == "A" and MIRAGE.site_zone("B") == "B"
    assert MIRAGE.missing(["Jungle", "NewArea"]) == ["NewArea"]


def test_draft_for_a_new_map():
    with tempfile.TemporaryDirectory() as tmp:
        path = write_draft("de_nuke", ["Outside", "BombsiteA", "BombsiteB", "Ramp", None], tmp)
        zones = load_zones("de_nuke", tmp)
        assert zones.unassigned == ["BombsiteA", "BombsiteB", "Outside", "Ramp"]
        assert zones.zones == {} and zones.sites == {"A": ["BombsiteA"], "B": ["BombsiteB"]}
        assert zones.zone("Outside") == "?" and not zones.reviewed
        try:
            write_draft("de_nuke", ["X"], tmp)
        except FileExistsError:
            pass
        else:
            raise AssertionError("a zone file must never be overwritten")
        assert "NOT SORTED YET" in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# features
# --------------------------------------------------------------------------

def test_zone_counts_and_setup():
    p = pos([(20, "c1", "BombsiteA"), (20, "c2", "Jungle"), (20, "c3", "BombsiteB"), (20, "c4", "Middle"), (21, "c5", "Shop")])
    counts = zone_counts(p, MIRAGE, 20)
    assert counts == {"A": 2, "B": 1, "Mid": 1}
    assert setup_string(counts) == "A2-B1-Mid1"
    assert zone_counts(p, MIRAGE, 99) == {}


def test_utility_waves():
    g = gren([(10.0, "smoke", "Jungle"), (12.5, "smoke", "Stairs"), (17.0, "molotov", "BombsiteA"),  # A: 10-17
              (19.0, "flash", "BombsiteA"),  # 9 s after the first: a new cluster, but alone
              (11.0, "smoke", "Middle"),  # Mid: a single grenade is no wave
              (30.0, "he", "BombsiteB"), (31.0, "flash", "BombsiteB"), (32.0, "decoy", "BombsiteB")])
    waves = utility_waves(g, MIRAGE, RULES)
    assert waves == [wave("A", 10, 17, smokes=2, molotovs=1), wave("B", 30, 31, flashes=1, hes=1)]


def test_site_hit_and_entries():
    p = pos([
        (28, "t1", "Jungle"), (28, "t2", "Connector"), (28, "t3", "TRamp"), (28, "t4", "TRamp"),
        (29, "t1", "BombsiteA"), (29, "t2", "BombsiteA"), (29, "t3", "TRamp"),  # only 2 inside
        (30, "t1", "BombsiteA"), (30, "t2", "BombsiteA"), (30, "t3", "BombsiteA"),  # 3 inside: the hit
    ])
    hit = find_site_hit(p, MIRAGE, RULES)
    assert hit == {"site": "A", "t": 30, "players": ["t1", "t2", "t3"]}
    areas, zones_ = entry_routes(p, hit, MIRAGE, RULES)
    assert areas == ["Connector", "Jungle", "TRamp"] and zones_ == ["A", "Mid"]
    assert find_site_hit(p[p["time_s"] < 30], MIRAGE, RULES) is None


def test_ct_features_aggression_and_rotation():
    p = pos([
        (10, "c1", "TSpawn"), (10, "c2", "BombsiteB"),  # c1 pushed into the T zone at 10 s
        (20, "c1", "Middle"), (20, "c2", "BombsiteB"), (20, "c3", "BombsiteA"),
        (40, "c2", "BombsiteB"), (40, "c3", "BombsiteA"),  # the T hit on A is at 40 s: 1 CT there
        (41, "c2", "Truck"), (41, "c3", "BombsiteA"),
        (44, "c2", "BombsiteA"), (44, "c3", "BombsiteA"),  # c2 arrives: rotation 4 s
    ])
    f = round_features("CT", p, gren([]), None, 12.0, 60, MIRAGE, RULES, t_hit={"site": "A", "t": 40})
    assert f["setup_20s"] == "A1-B1-Mid1" and f["aggression_s"] == 10 and f["rotation_s"] == 4
    assert f["first_contact_s"] == 12.0 and set(f["zones_at"]) == {"10", "20", "30", "45"}
    assert label_ct(f, MIRAGE, RULES) == ("aggression", None)


# --------------------------------------------------------------------------
# labels, rule by rule
# --------------------------------------------------------------------------

def test_t_labels():
    hit = lambda site, t: {"site": site, "t": t}  # noqa: E731
    assert label_t(features(site_hit=hit("B", 18)), MIRAGE, RULES) == ("rush", "B")
    execute = features(site_hit=hit("A", 35), waves=[wave("A", 25, 28, smokes=2, molotovs=1)])
    assert label_t(execute, MIRAGE, RULES) == ("execute", "A")
    late = features(site_hit=hit("A", 50), waves=[wave("A", 25, 28, smokes=2)])  # 22 s after the wave: too late
    assert label_t(late, MIRAGE, RULES)[0] != "execute"
    split = features(site_hit=hit("A", 35), entry_zones=["A", "Mid"])
    assert label_t(split, MIRAGE, RULES) == ("split", "A")
    both = features(site_hit=hit("A", 35), entry_zones=["A", "Mid"], waves=[wave("A", 30, smokes=2)])
    assert label_t(both, MIRAGE, RULES) == ("execute", "A")  # execute comes before split
    fake = features(site_hit=hit("B", 55), waves=[wave("A", 30, 32, smokes=1, molotovs=1)])
    assert label_t(fake, MIRAGE, RULES) == ("fake", "B")
    fake_plant = features(plant={"site": "A", "t": 70}, waves=[wave("B", 40, smokes=2)])
    assert label_t(fake_plant, MIRAGE, RULES) == ("fake", "A")
    default = features(zones_at={"30": {"A": 2, "Mid": 2, "B": 1}}, site_hit=hit("B", 65))
    assert label_t(default, MIRAGE, RULES) == ("default", "B")
    assert label_t(features(), MIRAGE, RULES) == ("slow", None)  # 90 s, no hit, all mid
    short = features(length_s=35)  # ended early by eliminations, no hit: nothing to say
    assert label_t(short, MIRAGE, RULES) == ("unknown", None)
    assert label_t(features(site_hit=hit("A", 45)), MIRAGE, RULES) == ("unknown", "A")
    no_sites = MapZones("de_x", zones={"A": ["X"]})
    assert label_t(features(site_hit=hit("A", 10)), no_sites, RULES) == ("unknown", None)


def test_rules_are_configurable():
    f = features(site_hit={"site": "B", "t": 28})
    assert label_t(f, MIRAGE, RULES)[0] != "rush"
    assert label_t(f, MIRAGE, PlayRules(rush_before_s=30.0))[0] == "rush"


def test_ct_labels():
    stack = {"zones_at": {"20": {"A": 4, "B": 1}}, "aggression_s": 12}
    assert label_ct(stack, MIRAGE, RULES) == ("stack", "A")  # stack comes first
    assert label_ct({"zones_at": {"20": {"A": 2, "B": 2, "Mid": 1}}, "aggression_s": None}, MIRAGE, RULES) == ("standard", None)


# --------------------------------------------------------------------------
# a whole match, manual labels, review, coverage
# --------------------------------------------------------------------------

def match_tables():
    """Round 1: we (A, on T) rush B at 20 s. Round 2: nothing clear (ends at 35 s)."""
    rounds = pd.DataFrame([{"round": 1, "played": 1, "duration_s": 50.0}, {"round": 2, "played": 1, "duration_s": 35.0}])
    team_rounds = pd.DataFrame([
        {"round": r, "team": t, "side": s, "won": int(t == "A"), "planted": int(r == 1 and s == "T"),
         "plant_site": "B" if r == 1 and s == "T" else None, "plant_time_s": 24.0 if r == 1 and s == "T" else None}
        for r in (1, 2) for t, s in (("A", "T"), ("B", "CT"))
    ])
    positions = pos([(20, f"a{i}", "BombsiteB") for i in range(1, 4)] + [(20, "b1", "BombsiteA")])
    positions["round"], positions["side"] = 1, ["T", "T", "T", "CT"]
    tables = {"rounds": rounds, "team_rounds": team_rounds, "positions": positions,
              "grenades": pd.DataFrame(columns=["round", "type", "thrower_side", "det_time_s", "land_place"]),
              "damages": pd.DataFrame([{"round": 1, "time_s": 15.5, "attacker_steamid": "b1", "attacker_side": "CT",
                                        "victim_side": "T", "after_round_end": 0}])}
    return tables


def test_compute_plays_and_manual_override():
    rows = compute_plays(match_tables(), MIRAGE, RULES, match_id="m1")
    t1 = rows[(rows["round"] == 1) & (rows["team"] == "A")].iloc[0]
    assert (t1["side"], t1["label"], t1["site"], t1["label_source"]) == ("T", "rush", "B", "rule")
    f = json.loads(t1["features"])
    assert f["site_hit"] == {"site": "B", "t": 20} and f["plant"] == {"site": "B", "t": 24} and f["first_contact_s"] == 15.5
    assert rows[(rows["round"] == 2) & (rows["team"] == "A")].iloc[0]["label"] == "unknown"
    assert len(rows) == 4

    manual = {"m1:2:A": {"label": "default", "name": "Mid control", "site": "A"}}
    fixed = compute_plays(match_tables(), MIRAGE, RULES, manual=manual, match_id="m1")
    r2 = fixed[(fixed["round"] == 2) & (fixed["team"] == "A")].iloc[0]
    assert (r2["label"], r2["name"], r2["site"], r2["label_source"], r2["confidence"]) == ("default", "Mid control", "A", "manual", 1.0)


class Env:
    """A synthetic database plus a private maps folder and labels file."""

    def __enter__(self):
        from fixtures import build_db
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "maps").mkdir()
        shutil.copy(REPO / "maps" / "de_mirage.toml", root / "maps" / "de_mirage.toml")
        self.cfg = Config(db_path=root / "t.sqlite", maps_dir=root / "maps", manual_labels_path=root / "labels.json")
        self.engine = build_db(self.cfg.db_path)
        return self

    def __exit__(self, *exc):
        self.engine.dispose()
        self.tmp.cleanup()


def test_manual_labels_survive_a_rebuild_and_a_new_database():
    with Env() as env:
        for m in ("m1", "m2", "m3"):
            build_plays(env.engine, m, env.cfg)
        assert (env.cfg.maps_dir / "de_inferno.toml").exists()  # a draft was written for the new map
        set_manual_label(env.engine, env.cfg, "m1", 3, "A", "execute", "B exec with Apps smokes", "B")
        assert load_manual(env.cfg.manual_labels_path) == {
            "m1:3:A": {"label": "execute", "name": "B exec with Apps smokes", "site": "B"}}
        build_plays(env.engine, "m1", env.cfg)  # the rules run again ...
        table = play_table(env.engine, "de_mirage")
        assert "B exec with Apps smokes" in set(table["play"])  # ... but the manual label stays

        # delete the database entirely and rebuild: the labels file brings it back
        env.engine.dispose()
        env.cfg.db_path.unlink()
        from fixtures import build_db
        env.engine = build_db(env.cfg.db_path)
        build_plays(env.engine, "m1", env.cfg)
        assert "B exec with Apps smokes" in set(play_table(env.engine, "de_mirage")["play"])


def test_review_coverage_and_plays_in_summaries_and_trends():
    with Env() as env:
        for m in ("m1", "m2", "m3"):
            build_plays(env.engine, m, env.cfg)
        before = coverage(env.engine, "de_mirage")
        assert before["t_rounds"] == 20 and before["manual"] == 0
        # the fixtures have no positions, so most rounds are "slow" (confidence 0.5): all go to review
        answers = iter(["execute", "B", "B exec", "s", "", "", "", "q"])
        lines = []
        changed = review(env.engine, env.cfg, "de_mirage", input_fn=lambda prompt: next(answers), print_fn=lines.append)
        assert changed == 2
        after = coverage(env.engine, "de_mirage")
        assert after["manual"] == 2 and after["labelled"] >= before["labelled"]
        assert any("rule says" in line for line in lines)

        from cs2review.summaries import build_digest
        digest, _ = build_digest(env.engine, "m1")
        assert digest["rounds"][0]["play"] == "B exec"
        from cs2review.trends import compute_trends
        by_play = compute_trends(env.engine, "de_mirage")["maps"]["de_mirage"]["sides"]["T"]["by_play"]
        assert by_play["B exec"]["n"] == 1 and sum(r["n"] for r in by_play.values()) == 20


def test_ensure_zones_writes_a_draft_from_observed_areas():
    with Env() as env:
        from sqlalchemy import text
        with env.engine.begin() as conn:
            conn.execute(text("INSERT INTO map_places (map, gx, gy, gz, place, n) VALUES ('de_nuke', 0, 0, 0, 'Outside', 3), "
                              "('de_nuke', 1, 0, 0, 'BombsiteA', 2)"))
        zones, created = ensure_zones(env.engine, "de_nuke", env.cfg.maps_dir)
        assert created and zones.unassigned == ["BombsiteA", "Outside"] and zones.sites == {"A": ["BombsiteA"]}
        again, created_again = ensure_zones(env.engine, "de_nuke", env.cfg.maps_dir)
        assert not created_again


def test_mapview_draws_a_png():
    from cs2review.mapview import draw_zone_map
    cells = pd.DataFrame([{"gx": x, "gy": y, "gz": 0, "place": p, "n": 1}
                          for x, y, p in [(0, 0, "BombsiteA"), (1, 0, "BombsiteA"), (5, 5, "Middle"), (9, 9, "Unsorted")]])
    with tempfile.TemporaryDirectory() as tmp:
        out = draw_zone_map(cells, MIRAGE, Path(tmp) / "m.png", "test")
        assert out.exists() and out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


if __name__ == "__main__":
    tests = [f for name, f in sorted(globals().items()) if name.startswith("test_") and callable(f)]
    for test in tests:
        test()
        print("ok  ", test.__name__)
    print(f"\n{len(tests)} tests passed")
