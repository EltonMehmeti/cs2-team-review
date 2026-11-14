"""Area names for points that have none (grenade landing spots).

The demo names the map area each *player* stands in ("Jungle", "BombsiteA"),
but a grenade's landing point is only x, y, z. So we keep a coarse 3D grid of
where players have stood and which area that was, and give a landing point the
name of the nearest known grid cell.

It is an approximation: good enough to say "the smoke landed in Jungle".
The grid is saved per map and grows with every demo you ingest, so the names
get more reliable over time.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

CELL = 64.0  # grid cell size in game units (a player is 32 units wide)
MAX_DIST = 600.0  # farther than this from any known cell -> try the top-down view
MAX_DIST_FLAT = 300.0  # top-down distance limit for that fallback

CELL_COLUMNS = ["gx", "gy", "gz", "place", "n"]


def empty_cells() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "gx": pd.Series(dtype="int64"),
            "gy": pd.Series(dtype="int64"),
            "gz": pd.Series(dtype="int64"),
            "place": pd.Series(dtype="object"),
            "n": pd.Series(dtype="int64"),
        }
    )


def build_cells(points: pd.DataFrame) -> pd.DataFrame:
    """points has columns x, y, z, place. Returns counts per (cell, place)."""
    pts = points.dropna(subset=["x", "y", "z", "place"])
    pts = pts[pts["place"].astype(str).str.len() > 0]
    if pts.empty:
        return empty_cells()
    cells = pd.DataFrame(
        {
            "gx": np.floor(pts["x"].to_numpy(dtype="float64") / CELL).astype("int64"),
            "gy": np.floor(pts["y"].to_numpy(dtype="float64") / CELL).astype("int64"),
            "gz": np.floor(pts["z"].to_numpy(dtype="float64") / CELL).astype("int64"),
            "place": pts["place"].astype(str).to_numpy(),
        }
    )
    out = cells.groupby(["gx", "gy", "gz", "place"], as_index=False).size()
    return out.rename(columns={"size": "n"})[CELL_COLUMNS]


def merge_cells(a: pd.DataFrame | None, b: pd.DataFrame | None) -> pd.DataFrame:
    """Add up the counts of two cell tables."""
    frames = [f[CELL_COLUMNS] for f in (a, b) if f is not None and not f.empty]
    if not frames:
        return empty_cells()
    both = pd.concat(frames, ignore_index=True)
    return both.groupby(["gx", "gy", "gz", "place"], as_index=False)["n"].sum()


def nearest_place(points: np.ndarray, cells: pd.DataFrame) -> list[str | None]:
    """Name of the nearest known cell for each (x, y, z) row of `points`."""
    points = np.asarray(points, dtype="float64").reshape(-1, 3)
    if len(points) == 0:
        return []
    if cells is None or cells.empty:
        return [None] * len(points)

    # one name per cell: the area seen there most often
    best = cells.sort_values("n", ascending=False).drop_duplicates(["gx", "gy", "gz"])
    centers = (best[["gx", "gy", "gz"]].to_numpy(dtype="float64") + 0.5) * CELL
    names = best["place"].to_numpy()

    out: list[str | None] = []
    for p in points:
        if np.isnan(p).any():
            out.append(None)
            continue
        dist2 = ((centers - p) ** 2).sum(axis=1)
        i = int(dist2.argmin())
        if dist2[i] <= MAX_DIST**2:
            out.append(str(names[i]))
            continue
        # A flash that pops high in the air is far from where anyone can stand.
        # Fall back to the nearest cell on the map seen from above.
        flat2 = ((centers[:, :2] - p[:2]) ** 2).sum(axis=1)
        i = int(flat2.argmin())
        out.append(str(names[i]) if flat2[i] <= MAX_DIST_FLAT**2 else None)
    return out
