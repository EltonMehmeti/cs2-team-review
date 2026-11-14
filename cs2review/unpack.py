"""Unpack downloaded demos into plain .dem files.

FACEIT serves demos as .dem.zst (older ones as .dem.gz). Valve download links
are .dem.bz2. A demo downloaded inside the game is already a plain .dem.
"""

from __future__ import annotations

import bz2
import gzip
import shutil
from pathlib import Path

DEMO_MAGIC = b"PBDEMS2"  # first bytes of every CS2 demo
DEMO_SUFFIXES = (".dem", ".dem.zst", ".dem.gz", ".dem.bz2")


def is_demo_file(path: Path) -> bool:
    return path.is_file() and path.name.lower().endswith(DEMO_SUFFIXES)


def match_id_for(path: Path) -> str:
    """File name without .dem and without the compression suffix."""
    name = path.name
    lower = name.lower()
    for suffix in (".zst", ".gz", ".bz2"):
        if lower.endswith(suffix):
            name = name[: -len(suffix)]
            lower = name.lower()
            break
    if lower.endswith(".dem"):
        name = name[:-4]
    return name


def looks_like_cs2_demo(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(len(DEMO_MAGIC)) == DEMO_MAGIC


def unpack_demo(src: Path, out_dir: Path) -> Path:
    """Return the path of a plain .dem file for `src`, decompressing if needed."""
    src = Path(src)
    lower = src.name.lower()

    if lower.endswith(".dem"):
        dem = src
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        dem = out_dir / f"{match_id_for(src)}.dem"
        if not (dem.exists() and looks_like_cs2_demo(dem)):
            tmp = dem.with_suffix(".dem.part")
            with src.open("rb") as fin, tmp.open("wb") as fout:
                if lower.endswith(".zst"):
                    import zstandard  # only needed for FACEIT archives

                    zstandard.ZstdDecompressor().copy_stream(fin, fout)
                elif lower.endswith(".gz"):
                    with gzip.GzipFile(fileobj=fin) as gz:
                        shutil.copyfileobj(gz, fout)
                elif lower.endswith(".bz2"):
                    with bz2.BZ2File(fin) as bz:
                        shutil.copyfileobj(bz, fout)
                else:
                    raise ValueError(f"Don't know how to unpack {src.name}")
            tmp.replace(dem)

    if not looks_like_cs2_demo(dem):
        raise ValueError(
            f"{dem.name} is not a CS2 demo (it should start with {DEMO_MAGIC!r}). "
            "CS:GO demos and broken downloads are not supported."
        )
    return dem
