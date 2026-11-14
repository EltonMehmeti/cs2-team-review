# CS2 Team Review: working rules for agents

Read `PLAN.md` before changing anything. It holds the goals, the phases, the
exact rules for every metric, and the facts already verified about the demo
parser. This file is the short version of how to work here.

## What this project is

A post-match review tool for one five-player CS2 team on FACEIT. It parses
demo files into SQLite, computes team metrics, and uses an LLM to write
reports and suggest plays. Phase 1 (demo -> SQLite + self-check) and Phase 2
(team metrics and round flags) are done. Phase 1.5 waits for the owner's
FACEIT demos.

## Hard rules

- The tool only reads `.dem` files after a match. Never add anything that
  reads game memory, injects, overlays, sends inputs, or runs during a match.
- No scraping of hltv.org. No Steam or FACEIT passwords. Keys come from
  environment variables.
- Numbers in reports come from SQL or Python, never from the LLM counting.
- Every AI claim cites round numbers or a source URL.
- Keep `python -m cs2review.check --all` passing. It compares our numbers
  with the game's own scoreboard.

## Commands

```
pip install -r requirements.txt
python -m cs2review.ingest              # demos in inbox/ -> data/cs2review.sqlite
python -m cs2review.check --all         # self-check of every match
python -m cs2review.metrics --rebuild   # recompute Phase 2 metrics (ingest does it too)
python -m cs2review.metrics --buy-histogram   # tune buy-type thresholds
python -m cs2review.debug_rounds X.dem  # raw round events of one demo
python tests/test_rounds.py             # unit tests (no demo needed)
python tests/test_metrics.py            # Phase 2 unit tests (no demo needed)
python tests/test_integration.py        # end-to-end (needs tests/data/test_demo.dem)
```

Get the public test demo once:

```
git clone --depth 1 https://github.com/LaihoE/demoparser tmp_demoparser
# copy tmp_demoparser/src/parser/test_demo.dem to tests/data/test_demo.dem
```

## How to work

- One phase at a time, in the order of `PLAN.md`. Finish its "Done when"
  list, then update the status table at the top of `PLAN.md`.
- When unsure how the parser behaves, write a short script and run it on the
  test demo. Do not guess. Add what you learn to section 4 of `PLAN.md`.
- The database is a cache that can be rebuilt from the demos. Change tables
  freely, bump `SCHEMA_VERSION` in `db.py`, re-ingest. No migrations.
- Tests are plain functions with asserts that run with and without pytest.
  Unit tests build small DataFrames by hand and never need a demo.
- Write simple code and explain CS2 terms in comments: the owner is a junior
  engineer who is new to CS2 analytics.
- Demo files, the database and `config.toml` are git-ignored. Never commit
  them.
- Ask the owner for the inputs listed in section 9 of `PLAN.md` instead of
  inventing them (Steam IDs, map pool, zone files, API keys).

## Code layout

```
cs2review/config.py        settings from config.toml
cs2review/unpack.py        decompress and validate demo files
cs2review/rounds.py        choose the real rounds of a match
cs2review/parse.py         demo -> DataFrames (the core of Phase 1)
cs2review/places.py        area names for grenade landing points
cs2review/db.py            SQLAlchemy models, SCHEMA_VERSION, save/load helpers
cs2review/ingest.py        CLI
cs2review/check.py         CLI: scoreboard, self-check, KAST and opening duels
cs2review/metrics.py       Phase 2: team_rounds, player_round_stats, kill flags
cs2review/debug_rounds.py  CLI: raw round events
```
