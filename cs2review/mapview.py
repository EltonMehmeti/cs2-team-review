"""Draw a map from the area grid, coloured by zone, to check a zone file (Phase 6).

    python -m cs2review.mapview de_mirage            # writes out/de_mirage_zones.png
    python -m cs2review.mapview de_mirage --out m.png

Every dot is a grid cell where a player has stood (map_places), seen from
above. The colour is its zone from maps/<map>.toml, and every area is
labelled "AreaName (zone)" at its centre. Grey dots are areas in no zone yet.
If an area sits in the wrong zone, fix maps/<map>.toml and draw again.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from .config import load_config
from .db import SchemaMismatchError, get_engine, load_place_cells
from .places import CELL
from .zones import UNKNOWN, ensure_zones

# categorical colours in a fixed order (validated for colour-blind readers;
# every area also carries a text label, so colour is never the only cue)
ZONE_COLOURS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
NO_ZONE = "#a8a69f"
SURFACE = "#fcfcfb"
INK = "#1f1e1c"
MUTED = "#6b6a63"


def draw_zone_map(cells: pd.DataFrame, zones, out: Path, title: str) -> Path:
    import matplotlib

    matplotlib.use("Agg")  # no window: just write the file
    import matplotlib.pyplot as plt

    if cells.empty:
        raise ValueError("No grid cells for this map yet. Ingest a demo of it first.")
    # one row per cell: the area seen most often there
    cells = cells.sort_values("n", ascending=False).drop_duplicates(["gx", "gy"])
    cells = cells.assign(x=cells["gx"] * CELL, y=cells["gy"] * CELL, zone=[zones.zone(p) for p in cells["place"]])
    zone_names = list(zones.zones)  # file order = fixed colour order
    colour = {z: ZONE_COLOURS[i % len(ZONE_COLOURS)] for i, z in enumerate(zone_names)}
    colour[UNKNOWN] = NO_ZONE

    fig, ax = plt.subplots(figsize=(11, 11), dpi=120)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for zone in zone_names + [UNKNOWN]:
        part = cells[cells["zone"] == zone]
        if part.empty:
            continue
        label = zone if zone != UNKNOWN else "no zone yet"
        ax.scatter(part["x"], part["y"], s=14, c=colour[zone], marker="s", linewidths=0, label=f"{label} ({len(part)} cells)")
    for place, part in cells.groupby("place"):
        ax.annotate(f"{place} ({zones.zone(place)})", (part["x"].median(), part["y"].median()), ha="center", va="center",
                    fontsize=8, color=INK,
                    bbox={"boxstyle": "round,pad=0.2", "fc": SURFACE, "ec": "none", "alpha": 0.85})
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_title(title, color=INK, fontsize=13, loc="left")
    ax.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0), frameon=False, labelcolor=INK, fontsize=9, title="zone",
              title_fontsize=9)
    status = "reviewed" if zones.reviewed else "NOT reviewed yet: check every label, then set reviewed = true"
    fig.text(0.01, 0.01, f"{zones.path} ({status})", color=MUTED, fontsize=8)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Draw a map coloured by zone, to check maps/<map>.toml.")
    ap.add_argument("map", help="map name, e.g. de_mirage")
    ap.add_argument("--out", type=Path, help="PNG file (default: out/<map>_zones.png)")
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
    cells = load_place_cells(engine, args.map)
    if cells.empty:
        print(f"No area grid for {args.map} yet. Ingest a demo of that map first.")
        return 1
    zones, created = ensure_zones(engine, args.map, cfg.maps_dir)
    out = args.out or Path(cfg.llm.out_dir) / f"{args.map}_zones.png"
    draw_zone_map(cells, zones, out, f"{args.map}: areas by zone (seen from above)")
    print(f"wrote {out}" + (f" (and a draft zone file {zones.path})" if created else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
