"""Show every round event in a demo, to debug round detection.

    python -m cs2review.debug_rounds path/to/match.dem

Use this when `python -m cs2review.check` reports a wrong score. It prints the
raw sequence of round_start / round_freeze_end / round_end events with the
game's round counter, and marks which round_end events were kept.
A knife round or a restart shows up as the counter going back to 0.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from demoparser2 import DemoParser

from .rounds import select_final_rounds
from .unpack import unpack_demo

EVENTS = ["begin_new_match", "round_announce_match_start", "round_start", "round_freeze_end", "round_end", "cs_win_panel_match"]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__)
        return 1
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    dem = unpack_demo(Path(args[0]), Path("demos"))
    parser = DemoParser(str(dem))
    print("server:", parser.parse_header().get("server_name"), "| map:", parser.parse_header().get("map_name"))

    frames = []
    for name, table in parser.parse_events(EVENTS, other=["total_rounds_played", "is_warmup_period"]):
        if isinstance(table, pd.DataFrame) and not table.empty:
            frames.append(table.assign(event=name))
    if not frames:
        print("No round events found in this demo.")
        return 1
    events = pd.concat(frames, ignore_index=True).sort_values(["tick", "event"]).reset_index(drop=True)

    ends = events[(events["event"] == "round_end") & ~events["is_warmup_period"].astype(bool)]
    kept = dict(select_final_rounds([(int(t), int(p)) for t, p in zip(ends["tick"], ends["total_rounds_played"])]))

    print(f"\n{'tick':>8}  {'event':<27}{'counter':>8}  {'warmup':<7}{'winner':<7}note")
    for e in events.itertuples():
        winner = {2: "T", 3: "CT"}.get(getattr(e, "winner", None), "") if e.event == "round_end" else ""
        note = ""
        if e.event == "round_end":
            note = f"-> round {kept[int(e.tick)]}" if int(e.tick) in kept else "ignored (warmup, knife round, restart or replay)"
        message = str(getattr(e, "message", "") or "").replace("#SFUI_Notice_", "") if e.event == "round_end" else ""
        print(
            f"{int(e.tick):>8}  {e.event:<27}{int(e.total_rounds_played):>8}  "
            f"{'yes' if bool(e.is_warmup_period) else '':<7}{winner:<7}{note} {message}"
        )
    print(f"\n{len(kept)} rounds kept out of {len(events[events['event'] == 'round_end'])} round_end events.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
