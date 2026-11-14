"""Map zones: groups of area names, one TOML file per map in maps/ (Phase 6).

    python -m cs2review.zones            # check the zone file of every map in the database
    python -m cs2review.zones de_nuke    # create a draft for a new map

A zone file says which of the game's area names ("TRamp", "Jungle") belong
to which zone ("A", "Mid"), and which areas are the bomb sites. For a map
without a file, the tool writes a draft with every area seen so far under
`unassigned`; the owner sorts them into zones (python -m cs2review.mapview
<map> shows where each area is).
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, text

from .config import load_config
from .db import SchemaMismatchError, get_engine

UNKNOWN = "?"  # zone of an area that is in no zone (or missing)


@dataclass
class MapZones:
    map: str
    zones: dict[str, list[str]] = field(default_factory=dict)
    sites: dict[str, list[str]] = field(default_factory=dict)
    unassigned: list[str] = field(default_factory=list)
    reviewed: bool = False
    last_map_update: str | None = None
    path: Path | None = None

    def __post_init__(self):
        self._zone_of = {area: zone for zone, areas in self.zones.items() for area in areas}
        self._site_of = {area: site for site, areas in self.sites.items() for area in areas}

    def zone(self, place) -> str:
        """The zone of an area name, or "?" if it is in none."""
        if place is None or (isinstance(place, float) and pd.isna(place)):
            return UNKNOWN
        return self._zone_of.get(str(place), UNKNOWN)

    def site(self, place) -> str | None:
        """The bomb site an area belongs to ("A"/"B"), or None."""
        if place is None or (isinstance(place, float) and pd.isna(place)):
            return None
        return self._site_of.get(str(place))

    def site_zone(self, site: str) -> str:
        """The zone that contains a site (for de_mirage: site A is in zone A)."""
        areas = self.sites.get(site, [])
        return self.zone(areas[0]) if areas else UNKNOWN

    def missing(self, places) -> list[str]:
        """Areas from the demos that are in no zone and not yet listed as unassigned."""
        known = set(self._zone_of) | set(self.unassigned)
        return sorted({str(p) for p in places if p and str(p) not in known})


def zone_file(map_name: str, maps_dir: str | Path = "maps") -> Path:
    return Path(maps_dir) / f"{map_name}.toml"


def load_zones(map_name: str, maps_dir: str | Path = "maps") -> MapZones | None:
    path = zone_file(map_name, maps_dir)
    if not path.exists():
        return None
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return MapZones(
        map=map_name,
        zones={k: [str(a) for a in v] for k, v in raw.get("zones", {}).items()},
        sites={k: [str(a) for a in v] for k, v in raw.get("sites", {}).items()},
        unassigned=[str(a) for a in raw.get("unassigned", [])],
        reviewed=bool(raw.get("reviewed", False)),
        last_map_update=str(raw.get("last_map_update") or "") or None,
        path=path,
    )


def _list(items) -> str:
    return "[" + ", ".join(f'"{i}"' for i in items) + "]"


def write_draft(map_name: str, places, maps_dir: str | Path = "maps") -> Path:
    """A new zone file with every observed area unassigned. Never overwrites."""
    path = zone_file(map_name, maps_dir)
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    areas = sorted({str(p) for p in places if p})
    sites = [a for a in areas if a.lower().startswith("bombsite")]
    lines = [
        f"# Zones for {map_name}, generated from the demos. NOT SORTED YET.",
        "# Move every area from `unassigned` into a zone (for example A, B, Mid, T, CT),",
        f"# using the picture from  python -m cs2review.mapview {map_name}",
        "# Then check [sites] and set reviewed = true.",
        "",
        "reviewed = false",
        'last_map_update = ""',
        f"unassigned = {_list(areas)}",
        "",
        "[zones]",
        "",
        "# The bomb sites (guessed from the area names; check them).",
        "[sites]",
    ]
    for area in sites:
        lines.append(f'{area[-1].upper()} = ["{area}"]')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def observed_places(engine: Engine, map_name: str) -> list[str]:
    sql = """
        SELECT DISTINCT place FROM map_places WHERE map = :m
        UNION SELECT DISTINCT p.place FROM positions p JOIN matches m ON m.match_id = p.match_id WHERE m.map = :m
    """
    with engine.connect() as conn:
        return sorted(r[0] for r in conn.execute(text(sql), {"m": map_name}) if r[0])


def ensure_zones(engine: Engine, map_name: str, maps_dir: str | Path = "maps") -> tuple[MapZones, bool]:
    """Load the zone file, writing a draft first if there is none. Returns (zones, created)."""
    zones = load_zones(map_name, maps_dir)
    if zones is not None:
        return zones, False
    write_draft(map_name, observed_places(engine, map_name), maps_dir)
    return load_zones(map_name, maps_dir), True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check or create the zone files of maps.")
    ap.add_argument("maps", nargs="*", help="map names (default: every map in the database)")
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
    with engine.connect() as conn:
        maps = args.maps or [r[0] for r in conn.execute(text("SELECT DISTINCT map FROM matches ORDER BY map"))]
    if not maps:
        print("No matches in the database yet.")
        return 1
    for map_name in maps:
        zones, created = ensure_zones(engine, map_name, cfg.maps_dir)
        places = observed_places(engine, map_name)
        status = "created a draft" if created else ("reviewed" if zones.reviewed else "NOT reviewed yet")
        print(f"{map_name}: {zones.path} ({status})")
        if zones.unassigned:
            print(f"  {len(zones.unassigned)} area(s) still unassigned: {', '.join(zones.unassigned)}")
        new = zones.missing(places)
        if new:
            print(f"  areas seen in demos but missing from the file: {', '.join(new)}")
        if not zones.sites:
            print("  no [sites]: play detection cannot find site hits on this map")
        if not zones.reviewed:
            print(f"  -> check the zones with: python -m cs2review.mapview {map_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
